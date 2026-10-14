"""Сервис тарификации: проверка лимитов и гибридная генерация изображений.

Модуль связывает модель TariffPlan с бизнес-логикой:

1. ``check_tariff_generation_limits`` — валидация запроса на генерацию
   контент-плана/пакета постов BEFORE обращения к LLM:
   - дальность периода (week/two_weeks/month) не больше max_plan_horizon_days;
   - уже созданные посты за текущий период + ожидаемое количество новых
     не превышают max_posts_per_week / max_posts_per_month.

2. ``assign_images_to_post`` — гибридное назначение картинок посту
   (user_upload → ai → web_search) в соответствии с политикой тарифа,
   с сохранением результатов в таблицу PostImage (order_index по порядку).

3. ``collect_user_uploaded_images`` — собирает загруженные пользователем
   картинки поста (PostImage.image_source == "user_upload") для повторного
   использования при автоназначении.

Все функции синхронные (SQLAlchemy Session); вызывающий код обязан пускать
их либо из threadpool-роутов FastAPI (``def``), либо через
``await asyncio.to_thread(...)`` из async-роутов — event loop не блокируется.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Post, PostImage, TariffPlan, User, UserCommunity

logger = logging.getLogger(__name__)

# Типы периодов -> (количество дней, количество постов в пакете/плане)
PERIOD_INFO: Dict[str, Dict[str, int]] = {
    "week": {"days": 7, "posts": 7},
    "two_weeks": {"days": 14, "posts": 14},
    "month": {"days": 30, "posts": 30},
}


def get_user_tariff(db: Session, user: User) -> Optional[TariffPlan]:
    """Возвращает TariffPlan пользователя (None, если тариф не назначен)."""
    if not user or not getattr(user, "tariff_plan_id", None):
        return None
    return db.query(TariffPlan).filter(TariffPlan.id == user.tariff_plan_id).first()


def parse_allowed_sources(plan: Optional[TariffPlan]) -> List[str]:
    """Безопасно парсит JSON-строку allowed_image_sources тарифа."""
    if plan is None or not plan.allowed_image_sources:
        return []
    try:
        data = json.loads(plan.allowed_image_sources)
    except (ValueError, TypeError):
        logger.warning("Tariff %s: некорректный JSON в allowed_image_sources", plan.id)
        return []
    return [str(s) for s in data] if isinstance(data, list) else []


def count_recent_posts(db: Session, user_id: int, days: int) -> int:
    """Считает посты пользователя (все его периоды) за последние ``days`` дней.

    Учитываются посты с publish_at в окне [now - days, now + days] —
    то есть уже созданные/запланированные на текущий период публикации.
    """
    from models import ContentPlanPeriod  # локальный импорт против циклов

    now = datetime.now()
    since = now - timedelta(days=days)
    until = now + timedelta(days=days)

    return (
        db.query(func.count(Post.id))
        .join(ContentPlanPeriod, Post.content_plan_period_id == ContentPlanPeriod.id)
        .filter(
            ContentPlanPeriod.user_id == user_id,
            Post.status.in_(["draft", "scheduled", "published"]),
            # publish_at NULL считаем «скоро» — включаем в подсчёт консервативно
            (
                (Post.publish_at >= since) & (Post.publish_at <= until)
            )
            | (Post.publish_at.is_(None)),
        )
        .scalar()
        or 0
    )


def check_tariff_generation_limits(
    db: Session,
    user: User,
    period_type: str,
    expected_posts: Optional[int] = None,
) -> Dict[str, Any]:
    """Проверяет лимиты тарифа перед генерацией. Возвращает диагностику.

    Args:
        db: Сессия БД.
        user: Пользователь-инициатор генерации.
        period_type: 'week' | 'two_weeks' | 'month'.
        expected_posts: сколько постов будет создано (по умолчанию — размер
            пакета для периода: 7/14/30).

    Raises:
        HTTPException(403): если запрошенный период длиннее max_plan_horizon_days
            или создание expected_posts постов превысит max_posts_per_week /
            max_posts_per_month (с учётом уже существующих постов за период).

    Returns:
        dict с фактами проверки (для логов/ответа): horizon_days, posts_limit,
        existing_posts, expected_posts.
    """
    from fastapi import HTTPException  # локальный импорт: сервис не привязан к web-слою

    info = PERIOD_INFO.get(period_type)
    if info is None:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный тип периода '{period_type}'. Доступно: week, two_weeks, month",
        )

    plan = get_user_tariff(db, user)
    if plan is None:
        # Тариф не назначен — ограничения не применяются (например, админ/демо).
        logger.debug("У пользователя id=%s нет тарифа — лимиты не проверяются", user.id)
        return {
            "has_tariff": False,
            "horizon_days": info["days"],
            "posts_limit": None,
            "existing_posts": 0,
            "expected_posts": expected_posts if expected_posts is not None else info["posts"],
        }

    days = info["days"]
    new_posts = expected_posts if expected_posts is not None else info["posts"]

    # (б) Дальность контент-плана
    if days > (plan.max_plan_horizon_days or 0):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Превышен лимит тарифного плана '{plan.name}': максимальная "
                f"дальность контент-плана — {plan.max_plan_horizon_days} дн., "
                f"запрошен период '{period_type}' ({days} дн.)."
            ),
        )

    # (в) Количество постов за текущий период
    limit_key = "max_posts_per_month" if days >= 30 else "max_posts_per_week"
    posts_limit = int(getattr(plan, limit_key, 0) or 0)
    existing = count_recent_posts(db, user.id, days)
    if existing + new_posts > posts_limit:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Превышен лимит тарифного плана '{plan.name}': можно не более "
                f"{posts_limit} постов за {('месяц' if days >= 30 else 'неделю')}, "
                f"сейчас создано {existing}. Запрошенная генерация добавит ещё "
                f"{new_posts} постов."
            ),
        )

    logger.info(
        "Тарифная проверка пройдена: user=%s, plan=%s, period=%s, existing=%d, new=%d, limit=%d",
        user.id, plan.name, period_type, existing, new_posts, posts_limit,
    )
    return {
        "has_tariff": True,
        "tariff_name": plan.name,
        "horizon_days": plan.max_plan_horizon_days,
        "posts_limit": posts_limit,
        "existing_posts": existing,
        "expected_posts": new_posts,
    }


def _to_web_media_path(image_path: Optional[str]) -> Optional[str]:
    """Локальный путь data/media/x.jpg -> веб-путь /media/x.jpg (без дублирования логики)."""
    if not image_path:
        return image_path
    p = str(image_path).replace("\\", "/")
    if p.startswith(("http://", "https://", "/")):
        return p
    marker = "data/media/"
    idx = p.find(marker)
    if idx != -1:
        return "/media/" + p[idx + len(marker):]
    return "/media/" + p.lstrip("./")


def collect_user_uploaded_images(post: Post) -> List[Dict[str, str]]:
    """Возвращает уже загруженные пользователем картинки поста (user_upload)."""
    return [
        {"url": img.image_url, "source": "user_upload"}
        for img in (post.images or [])
        if (img.image_source or "") == "user_upload" and img.image_url
    ]


def assign_images_to_post(
    db: Session,
    post: Post,
    tariff_plan: Optional[TariffPlan],
    topic: Optional[str] = None,
    replace_existing: bool = True,
) -> List[Dict[str, str]]:
    """Гибридно назначает посту изображения согласно политике тарифа.

    Логика (пункт 2 задания):
      а) allows_images == False (или тарифа нет) — пост остаётся без картинок
         (при replace_existing старые автокартинки очищаются);
      б) целевое количество — до max_images_per_post;
      в) источники обходятся в приоритетном порядке:
         1. user_upload — загруженные пользователем картинки всегда первые;
         2. ai — генерация через GigaChat/Kandinsky (generators.image_generator);
         3. web_search — services.image_search_service.search_images_sync.
      г) результаты сохраняются в PostImage с корректным order_index;
         post.image_url/image_source синхронизируются с первой картинкой.

    Функция СИНХРОННАЯ (сетевые вызовы генерации/поиска идут в потоке):
    вызывать только из threadpool-роута или через asyncio.to_thread.

    Returns:
        Список [{"url", "source"}] назначенных изображений.
    """
    allows = bool(tariff_plan is not None and tariff_plan.allows_images)
    max_imgs = int(tariff_plan.max_images_per_post or 0) if allows else 0
    sources = parse_allowed_sources(tariff_plan) if allows else []

    # Приоритет обхода источников: пользовательские -> ИИ -> веб-поиск
    ordered_sources = [s for s in ("user_upload", "ai", "web_search") if s in sources]

    chosen: List[Dict[str, str]] = []

    # 1. Загруженные пользователем картинки — первыми
    if "user_upload" in ordered_sources:
        chosen.extend(collect_user_uploaded_images(post)[:max_imgs])

    # Если картинок всё ещё нет — генерируем/ищем по разрешённым источникам
    if not chosen and max_imgs > 0:
        subject = (topic or post.topic or "").strip() or "пост для соцсети"
        prompt = (
            f"Фотография для поста ВКонтакте: {subject}. "
            "Яркая, реалистичная, без текста на изображении."
        )
        search_query = subject[:80]

        for source in ordered_sources:
            if len(chosen) >= max_imgs:
                break
            if source == "ai":
                try:
                    from generators.image_generator import generate_image

                    path = generate_image(prompt=prompt, save_dir="data/media")
                    url = _to_web_media_path(path)
                    if url:
                        chosen.append({"url": url, "source": "ai"})
                except Exception as e:  # noqa: BLE001 — деградируем к следующему источнику
                    logger.warning("assign_images_to_post: ИИ-генерация не удалась: %s", e)
            elif source == "web_search":
                try:
                    from services.image_search_service import search_images_sync

                    urls = search_images_sync(search_query, max_imgs - len(chosen))
                    chosen.extend({"url": u, "source": "web_search"} for u in urls)
                except Exception as e:  # noqa: BLE001
                    logger.warning("assign_images_to_post: веб-поиск не удался: %s", e)

    chosen = chosen[:max_imgs] if max_imgs else []

    if replace_existing or chosen:
        # Полностью заменяем набор PostImage (старые удаляются каскадом)
        post.images = []
        for idx, item in enumerate(chosen):
            post.images.append(
                PostImage(
                    image_url=item["url"],
                    image_source=item["source"],
                    order_index=idx,
                )
            )
        # Обратная совместимость: image_url = первая картинка
        if post.images:
            first = post.images[0]
            post.image_url = first.image_url
            post.image_source = first.image_source
        elif replace_existing:
            post.image_url = None
            post.image_source = None
        db.commit()
        db.refresh(post)

    logger.info(
        "assign_images_to_post: пост id=%s получил %d картинок (тариф: %s)",
        post.id, len(post.images), getattr(tariff_plan, "name", None),
    )
    return [{"url": i["url"], "source": i["source"]} for i in chosen]


def resolve_community_for_period(db: Session, period) -> Optional[UserCommunity]:
    """Определяет UserCommunity периода по community_info ("Сообщество ID: N").

    Используется для подстановки предпочтений источников картинок сообщества
    (CommunityGenerationContext.image_source_preferences) поверх общих
    разрешений тарифа.
    """
    info = (getattr(period, "community_info", "") or "") if period else ""
    marker = "Сообщество ID:"
    if marker not in info:
        return None
    try:
        cid = int(info.split(marker, 1)[1].strip().split()[0].strip(",."))
    except (ValueError, IndexError):
        return None
    return db.query(UserCommunity).filter(UserCommunity.id == cid).first()
