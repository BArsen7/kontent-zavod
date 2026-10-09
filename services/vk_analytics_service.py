"""
Сервис аналитики VK: импорт исторических постов и обновление метрик.

Функции модуля синхронные (как publishers/vk_publisher.py и
managers/community_manager.py): они вызываются из скриптов, FastAPI-роутов
(через threadpool) и фоновых задач APScheduler.

Работа с VK API ведётся через единый модуль vk_errors.py:
- create_vk_session — инициализация VkApi с api_version=5.199;
- with_retry — повтор запроса при rate-limit (код 6);
- describe_api_error — структурированное логирование ошибок.

Используемые методы VK API:
- wall.get        — история стены (owner_id отрицательный, count <= 100, offset);
- wall.getById    — актуальные метрики конкретных постов (posts="owner_id_post_id");
- groups.getById  — members_count и description сообщества (fields).

Формула Engagement Rate (ER):
    ER = (likes + comments + reposts) / views, если views > 0;
    ER = (likes + comments + reposts) / members_count, иначе.
Пост считается "топовым" (is_top_performer), если его ER входит в топ-15%
всех импортированных постов данного сообщества.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database import SessionLocal
from models import HistoricalPost, PlatformAccount, Post, PostStats
from vk_errors import (
    VK_API_VERSION,
    create_vk_session,
    owner_id_for_group,
    positive_group_id,
    with_retry,
)

logger = logging.getLogger(__name__)

# Максимальное число записей за один вызов wall.get (лимит VK API).
WALL_GET_MAX_COUNT: int = 100

# Квантиль порога "топовости" (только верхние 15% постов по ER).
TOP_PERFORMER_QUANTILE: float = 0.85

# Размер пачки для wall.getById (API принимает до 100 постов за запрос).
GET_BY_ID_BATCH_SIZE: int = 20


def _extract_items(response: Any) -> List[Dict[str, Any]]:
    """Нормализует ответ VK API к списку объектов постов.

    Поддерживает форматы: {'count': N, 'items': [...]} и голый список.
    """
    if isinstance(response, dict):
        return list(response.get("items") or [])
    if isinstance(response, list):
        return [x for x in response if isinstance(x, dict)]
    return []


def _metric_count(obj: Any) -> int:
    """Безопасно извлекает .count из метрического объекта VK (views/likes/...)."""
    if isinstance(obj, dict):
        try:
            return int(obj.get("count", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def calculate_engagement_rate(
    views: int, likes: int, comments: int, shares: int, members_count: int
) -> float:
    """Вычисляет ER по формуле проекта.

    Args:
        views: Просмотры поста (может быть 0).
        likes: Число лайков.
        comments: Число комментариев.
        shares: Число репостов.
        members_count: Число подписчиков группы (защита от деления на ноль).

    Returns:
        Значение Engagement Rate (float, >= 0).
    """
    reactions = max(int(likes) + int(comments) + int(shares), 0)
    v = max(int(views), 0)
    if v > 0:
        return reactions / v
    m = max(int(members_count), 0)
    if m > 0:
        return reactions / m
    return 0.0


def get_group_info(token: str, group_id: int, fields: str = "members_count,description") -> Dict[str, Any]:
    """Запрашивает groups.getById с указанными полями.

    Args:
        token: Сервисный ключ доступа сообщества.
        group_id: ID группы (нормализуется к положительному).
        fields: Список полей через запятую.

    Returns:
        Словарь с информацией о группе (пустой, если группа не найдена).
    """
    gid = positive_group_id(group_id)
    try:
        vk = create_vk_session(token).get_api()
        result = with_retry(
            vk.groups.getById,
            group_ids=str(gid),
            fields=fields,
            scope=f"groups.getById(g={gid})",
        )
    except Exception as e:  # noqa: BLE001 — аналитика не должна ронять вызывающий код
        logger.error(f"[vk_analytics] groups.getById для g={gid} не удался: {type(e).__name__}: {e}")
        return {}

    if isinstance(result, dict):
        groups_info = result.get("groups") or result.get("items") or []
    elif isinstance(result, list):
        groups_info = result
    else:
        groups_info = []
    return groups_info[0] if groups_info else {}


def _resolve_community(db: Session, community_id: int) -> Optional[PlatformAccount]:
    """Возвращает PlatformAccount (vk) по id или None с диагностическим логом."""
    account = db.get(PlatformAccount, community_id)
    if account is None:
        logger.error(f"[vk_analytics] PlatformAccount id={community_id} не найден")
        return None
    if account.platform != "vk":
        logger.error(
            f"[vk_analytics] Аккаунт id={community_id} имеет платформу "
            f"'{account.platform}', ожидается 'vk'"
        )
        return None
    return account


def _fetch_wall_posts(vk: Any, owner_id: int, limit: int) -> List[Dict[str, Any]]:
    """Пагинированно загружает записи со стены через wall.get (до `limit` шт.)."""
    items: List[Dict[str, Any]] = []
    offset = 0
    while len(items) < limit:
        batch = min(WALL_GET_MAX_COUNT, limit - len(items))
        response = with_retry(
            vk.wall.get,
            owner_id=owner_id,
            count=batch,
            offset=offset,
            v=VK_API_VERSION,
            scope=f"wall.get(owner={owner_id})",
        )
        page_items = _extract_items(response)
        if not page_items:
            break
        items.extend(page_items)
        offset += len(page_items)
        total = response.get("count") if isinstance(response, dict) else None
        if total is not None and offset >= int(total):
            break
    return items[:limit]


def _upsert_historical_posts(
    db: Session, community_id: int, items: List[Dict[str, Any]], members_count: int
) -> int:
    """Создаёт/обновляет записи HistoricalPost по ответам wall.get/wall.getById.

    Upsert по уникальному полю vk_post_id. Возвращает число обработанных постов.
    """
    processed = 0
    for item in items:
        post_id = item.get("id")
        if post_id is None:
            continue
        try:
            post_id = int(post_id)
        except (TypeError, ValueError):
            continue

        views = _metric_count(item.get("views"))
        likes = _metric_count(item.get("likes"))
        comments = _metric_count(item.get("comments"))
        shares = _metric_count(item.get("reposts"))
        er = calculate_engagement_rate(views, likes, comments, shares, members_count)

        date_ts = item.get("date")
        published_at = datetime.utcfromtimestamp(date_ts) if date_ts else None
        text = item.get("text") or ""

        record = db.query(HistoricalPost).filter(HistoricalPost.vk_post_id == post_id).first()
        if record is None:
            record = HistoricalPost(
                community_id=community_id,
                vk_post_id=post_id,
                text=text,
                published_at=published_at,
                views=views,
                likes=likes,
                shares=shares,
                comments=comments,
                engagement_rate=er,
            )
            db.add(record)
        else:
            record.text = text
            record.published_at = published_at or record.published_at
            record.views = views
            record.likes = likes
            record.shares = shares
            record.comments = comments
            record.engagement_rate = er
        processed += 1

    db.commit()
    return processed


def refresh_top_performers(db: Session, community_id: int) -> None:
    """Пересчитывает порог топ-15% ER и обновляет флаг is_top_performer.

    Порог = 85-й квантиль распределения ER среди постов сообщества
    (numpy.percentile(interpolation='linear'), реализация без зависимостей).
    """
    rates = [
        r[0]
        for r in db.query(HistoricalPost.engagement_rate)
        .filter(HistoricalPost.community_id == community_id)
        .all()
        if r[0] is not None
    ]
    posts = (
        db.query(HistoricalPost)
        .filter(HistoricalPost.community_id == community_id)
        .all()
    )
    if not posts:
        return

    if rates:
        ordered = sorted(rates)
        pos = TOP_PERFORMER_QUANTILE * (len(ordered) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(ordered) - 1)
        frac = pos - lo
        threshold = ordered[lo] * (1.0 - frac) + ordered[hi] * frac
    else:
        threshold = 0.0

    top_count = 0
    for p in posts:
        is_top = (p.engagement_rate or 0.0) >= threshold and (p.engagement_rate or 0.0) > 0
        if p.is_top_performer != is_top:
            p.is_top_performer = is_top
        if is_top:
            top_count += 1
    db.commit()
    logger.info(
        f"[vk_analytics] Топ-перформеры для community_id={community_id}: "
        f"{top_count} из {len(posts)} (порог ER={threshold:.4f})"
    )


def import_history(community_id: int, limit: int = 50, db: Optional[Session] = None) -> int:
    """Импортирует историю постов сообщества из VK (wall.get) с расчётом ER.

    Args:
        community_id: ID записи PlatformAccount (FK моделей аналитики).
        limit: Максимальное число постов для импорта.
        db: Необязательная внешняя сессия БД (создаётся своя, если не передана).

    Returns:
        Число созданных/обновлённых записей HistoricalPost (0 при ошибке).
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        account = _resolve_community(db, community_id)
        if account is None:
            return 0

        gid = positive_group_id(int(account.account_id))
        owner_id = owner_id_for_group(gid)

        # members_count нужен для ER при отсутствии просмотров у постов.
        group_info = get_group_info(account.access_token, gid, fields="members_count")
        members_count = int(group_info.get("members_count", 0) or 0)

        try:
            vk = create_vk_session(account.access_token).get_api()
            items = _fetch_wall_posts(vk, owner_id, limit)
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"[vk_analytics] Импорт истории для community_id={community_id} "
                f"(g={gid}) не удался: {type(e).__name__}: {e}"
            )
            return 0

        if not items:
            logger.warning(f"[vk_analytics] wall.get вернул пустой список для g={gid}")
            return 0

        processed = _upsert_historical_posts(db, community_id, items, members_count)
        refresh_top_performers(db, community_id)
        logger.info(
            f"[vk_analytics] Импортировано постов: {processed} "
            f"(community_id={community_id}, g={gid})"
        )
        return processed
    finally:
        if own_db:
            db.close()


def update_metrics_for_published_posts(db: Optional[Session] = None) -> int:
    """Обновляет метрики постов, опубликованных планировщиком (wall.getById).

    Берёт посты со статусом 'published' и для каждого сообщества (по
    config_json аккаунта либо по настройкам .env) запрашивает актуальные
    likes/comments/views/reposts и сохраняет их в PostStats (последняя
    запись) и HistoricalPost (для последующего AI-анализа контекста).

    Args:
        db: Необязательная внешняя сессия БД.

    Returns:
        Число обновлённых постов.
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        published_posts = (
            db.query(Post)
            .filter(Post.status == "published", Post.published_at.isnot(None))
            .order_by(Post.published_at.desc())
            .all()
        )
        if not published_posts:
            logger.debug("[vk_analytics] Опубликованных постов нет — обновлять метрики нечего")
            return 0

        accounts = db.query(PlatformAccount).filter(PlatformAccount.platform == "vk").all()

        updated_total = 0
        for account in accounts:
            gid = positive_group_id(int(account.account_id))
            owner_id = owner_id_for_group(gid)
            group_info = get_group_info(account.access_token, gid, fields="members_count")
            members_count = int(group_info.get("members_count", 0) or 0)

            # Пачки "owner_id_post_id" формируем только для постов этого сообщества.
            post_ids = [pid for pid in _post_ids_for_account(db, published_posts, account) if pid]
            if not post_ids:
                continue

            try:
                vk = create_vk_session(account.access_token).get_api()
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[vk_analytics] Не создана VK-сессия для g={gid}: {type(e).__name__}: {e}"
                )
                continue

            for chunk_start in range(0, len(post_ids), GET_BY_ID_BATCH_SIZE):
                chunk = post_ids[chunk_start: chunk_start + GET_BY_ID_BATCH_SIZE]
                posts_param = ",".join(f"{owner_id}_{pid}" for pid in chunk)
                try:
                    response = with_retry(
                        vk.wall.getById,
                        posts=posts_param,
                        v=VK_API_VERSION,
                        scope=f"wall.getById(g={gid})",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        f"[vk_analytics] wall.getById для g={gid} не удался: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue

                items = _extract_items(response)
                if not items:
                    continue

                # Обновляем метрики импортированной истории (если посты там есть).
                _upsert_historical_posts(db, account.id, items, members_count)

                for item in items:
                    if _apply_stats_to_post(db, published_posts, item):
                        updated_total += 1

        refresh_top_performers_all(db)
        logger.info(f"[vk_analytics] Обновлено метрик постов: {updated_total}")
        return updated_total
    finally:
        if own_db:
            db.close()


def _post_ids_for_account(db: Session, posts: List[Post], account: PlatformAccount) -> List[int]:
    """Извлекает VK post_id опубликованных постов, относящихся к аккаунту.

    Порядок поиска привязки:
    1. period.community_info содержит id/link сообщества (account_id);
    2. единственный vk-аккаунт в системе (конфигурация "одно сообщество").
    """
    account_ids = {str(account.account_id), str(positive_group_id(int(account.account_id)))}
    result: List[int] = []
    single_account = False
    all_vk = db.query(PlatformAccount).filter(PlatformAccount.platform == "vk").count()
    if all_vk == 1:
        single_account = True

    for post in posts:
        vk_post_id: Optional[int] = None
        if post.content_plan_period is not None:
            info = post.content_plan_period.community_info or ""
            if any(a and a in info for a in account_ids) or single_account:
                vk_post_id = _parse_vk_post_id(info, account_ids)
        elif single_account:
            vk_post_id = None  # период неизвестен — пропускаем (нет source vk_post_id)
        if vk_post_id is not None:
            result.append(vk_post_id)
    return result


def _parse_vk_post_id(text: str, account_ids: set) -> Optional[int]:
    """Пытается извлечь vk post_id из строки community_info.

    Поддерживает форматы: "wall-123_456", "-123_456", "…/w{owner}_{id}",
    "https://vk.com/wall-{owner}_{post_id}".
    """
    import re

    match = re.search(r"wall-?\d+_(\d+)", text)
    if not match:
        match = re.search(r"-\d+_(\d+)", text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _apply_stats_to_post(db: Session, posts: List[Post], item: Dict[str, Any]) -> bool:
    """Сохраняет метрики одного ответа wall.getById в PostStats ближайшего поста.

    Сопоставление поста выполняется по тексту (text_final/text_draft совпадает
    с item['text'] полностью или является его префиксом) — это надёжнее, чем
    парсинг произвольного community_info.
    """
    text = item.get("text") or ""
    target: Optional[Post] = None
    for post in posts:
        body = post.text_final or post.text_draft or ""
        if body and text and (body == text or text.startswith(body[:200])):
            target = post
            break
    if target is None:
        return False

    stats = (
        db.query(PostStats)
        .filter(PostStats.post_id == target.id, PostStats.platform == "vk")
        .order_by(PostStats.collected_at.desc())
        .first()
    )
    views = _metric_count(item.get("views"))
    likes = _metric_count(item.get("likes"))
    comments = _metric_count(item.get("comments"))

    if stats is None:
        stats = PostStats(post_id=target.id, platform="vk", views=views, likes=likes, comments=comments)
        db.add(stats)
    else:
        stats.views = views
        stats.likes = likes
        stats.comments = comments
        stats.collected_at = datetime.utcnow()
    db.commit()
    return True


def refresh_top_performers_all(db: Session) -> None:
    """Пересчитывает топы по всем сообществам, имеющим исторические посты."""
    community_ids = [
        row[0]
        for row in db.query(HistoricalPost.community_id).distinct().all()
    ]
    for cid in community_ids:
        refresh_top_performers(db, cid)
