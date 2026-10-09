"""
Сервис AI-анализа контекста сообщества и генерации маркетинговой стратегии.

Использует существующий generate_text() из generators/text_generator.py
(провайдер выбирается по SystemSetting ai_provider: ollama / cloud / gigachat).

Данные:
- CommunityContext — «паспорт» сообщества (ЦА, tone of voice, УТП, инсайты),
  строится по описанию группы (groups.getById fields="description") и по
  историческим постам HistoricalPost (5 топовых по ER + 3 худших по ER);
- MarketingStrategy — контент-стратегия (rubrics, content_pillars,
  posting_schedule), строится из паспорта сообщества.

Оба сервиса требуют СТРОГО валидный JSON от LLM; ответ очищается от
возможных markdown-обёрток и парсится через json.loads с fallback-извлечением
первого {...} блока.
"""
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database import SessionLocal
from generators.text_generator import generate_text
from models import CommunityContext, HistoricalPost, MarketingStrategy, PlatformAccount
from services.vk_analytics_service import get_group_info

logger = logging.getLogger(__name__)

# Число примеров постов, передаваемых в промпт анализа.
TOP_POSTS_FOR_CONTEXT: int = 5
BOTTOM_POSTS_FOR_CONTEXT: int = 3

# Максимальная длина одного поста в промпте (защита от переполнения контекста LLM).
MAX_POST_CHARS_IN_PROMPT: int = 700

# Системный промпт: LLM обязана вернуть чистый JSON без markdown-обёрток.
JSON_SYSTEM_PROMPT = (
    "Ты — аналитик социальных сетей. Отвечай ТОЛЬКО валидным JSON без "
    "markdown-обёрток (```), пояснений и дополнительного текста."
)

CONTEXT_PROMPT_TEMPLATE = """Ты — опытный SMM-аналитик. Проанализируй данные сообщества ВКонтакте.
Описание сообщества: {group_description}
Примеры успешных постов (высокий ER): {top_posts_text}
Примеры неудачных постов (низкий ER): {bottom_posts_text}

Верни СТРОГО валидный JSON без markdown-оберток со следующей структурой:
{{
  "target_audience": "Краткое описание ЦА, их боли и интересы (макс. 3 предложения)",
  "tone_of_voice": "Стиль общения (например: дружелюбный, экспертный, дерзкий)",
  "usp": "Уникальное торговое предложение или главная фишка",
  "insights_summary": "3 конкретных паттерна: какие темы, форматы или хуки сработали лучше всего, а какие нет."
}}"""

STRATEGY_PROMPT_TEMPLATE = """Ты — стратег по контент-маркетингу. На основе паспорта сообщества разработай стратегию.
Паспорт сообщества: {context_json}

Верни СТРОГО валидный JSON со следующей структурой:
{{
  "content_pillars": ["Смысловой блок 1", "Смысловой блок 2", "Смысловой блок 3"],
  "rubrics": [
    {{"name": "Название рубрики", "description": "О чем писать", "frequency": "например, 2 раза в неделю"}}
  ],
  "posting_schedule": "Рекомендации по лучшим дням и времени для публикации, основанные на поведении ЦА"
}}"""


def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    """Извлекает JSON-объект из ответа LLM.

    Терпимо к markdown-обёрткам ```json ... ``` и постороннему тексту вокруг:
    сначала пробуем json.loads целиком, затем ищем первый сбалансированный
    блок {...}.

    Returns:
        Словарь либо None, если JSON не распознан.
    """
    if not raw:
        return None

    cleaned = raw.strip()
    # Срезаем markdown-обёртки, если модель их всё-таки добавила.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Поиск первого сбалансированного { ... } блока.
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(cleaned)):
            ch = cleaned[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        fragment = cleaned[start: idx + 1]
                        try:
                            parsed = json.loads(fragment)
                            if isinstance(parsed, dict):
                                return parsed
                        except (json.JSONDecodeError, ValueError):
                            break
        start = cleaned.find("{", start + 1)

    logger.warning("Не удалось распарсить JSON из ответа LLM")
    return None


def _format_post_sample(post: HistoricalPost, index: int) -> str:
    """Форматирует один исторический пост для блока примеров в промпте."""
    text = (post.text or "").strip().replace("\n", " ")
    if len(text) > MAX_POST_CHARS_IN_PROMPT:
        text = text[:MAX_POST_CHARS_IN_PROMPT] + "…"
    return (
        f"[Пример {index}] ER={post.engagement_rate:.4f}, "
        f"лайки={post.likes}, комменты={post.comments}, репосты={post.shares}, "
        f"просмотры={post.views}\nТекст: {text}"
    )


def _collect_post_samples(db: Session, community_id: int) -> tuple[str, str]:
    """Возвращает текстовые блоки топ- и антипримеров постов сообщества."""
    top_posts = (
        db.query(HistoricalPost)
        .filter(
            HistoricalPost.community_id == community_id,
            HistoricalPost.is_top_performer.is_(True),
        )
        .order_by(HistoricalPost.engagement_rate.desc())
        .limit(TOP_POSTS_FOR_CONTEXT)
        .all()
    )
    bottom_posts = (
        db.query(HistoricalPost)
        .filter(HistoricalPost.community_id == community_id)
        .order_by(HistoricalPost.engagement_rate.asc())
        .limit(BOTTOM_POSTS_FOR_CONTEXT)
        .all()
    )

    top_text = "\n\n".join(_format_post_sample(p, i + 1) for i, p in enumerate(top_posts))
    bottom_text = "\n\n".join(_format_post_sample(p, i + 1) for i, p in enumerate(bottom_posts))
    return top_text or "(нет данных)", bottom_text or "(нет данных)"


def build_community_context(community_id: int, db: Optional[Session] = None) -> Optional[CommunityContext]:
    """Формирует и сохраняет AI-паспорт сообщества (CommunityContext).

    Шаги:
    1. Описание группы: берём из groups.getById(fields="description"),
       fallback — config_json аккаунта / произвольный текст периода.
    2. 5 топовых и 3 худших по ER поста из HistoricalPost.
    3. Промпт -> LLM -> JSON -> upsert CommunityContext.

    Args:
        community_id: ID записи PlatformAccount.
        db: Необязательная внешняя сессия БД.

    Returns:
        Сохранённый CommunityContext либо None (аккаунт не найден, нет
        исторических данных или LLM вернула невалидный JSON).
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        account = db.get(PlatformAccount, community_id)
        if account is None or account.platform != "vk":
            logger.error(f"[context_strategy] VK-аккаунт id={community_id} не найден")
            return None

        group_id = abs(int(account.account_id))
        group_info = get_group_info(account.access_token, group_id, fields="description,members_count")
        group_description = (
            group_info.get("description")
            or group_info.get("wiki")
            or ""
        ).strip()
        if not group_description:
            # Fallback: описание могло быть сохранено при регистрации сообщества.
            cfg = account.config_json if isinstance(account.config_json, dict) else {}
            group_description = str(cfg.get("description") or cfg.get("group_name") or "Описание отсутствует")

        posts_total = (
            db.query(HistoricalPost)
            .filter(HistoricalPost.community_id == community_id)
            .count()
        )
        if posts_total == 0:
            logger.warning(
                f"[context_strategy] Нет исторических постов для community_id={community_id}. "
                "Сначала выполните импорт: services.vk_analytics_service.import_history()"
            )
            return None

        top_posts_text, bottom_posts_text = _collect_post_samples(db, community_id)

        prompt = CONTEXT_PROMPT_TEMPLATE.format(
            group_description=group_description,
            top_posts_text=top_posts_text,
            bottom_posts_text=bottom_posts_text,
        )

        from services.content_service import _default_use_local

        try:
            raw = generate_text(prompt=prompt, system_prompt=JSON_SYSTEM_PROMPT, use_local=_default_use_local(db))
        except Exception as e:  # noqa: BLE001 — генерация не должна ронять планировщик/роут
            logger.error(f"[context_strategy] Ошибка generate_text для контекста: {type(e).__name__}: {e}")
            return None

        data = _parse_llm_json(raw)
        if data is None:
            return None

        context = db.query(CommunityContext).filter(CommunityContext.community_id == community_id).first()
        if context is None:
            context = CommunityContext(community_id=community_id)
            db.add(context)

        context.target_audience = str(data.get("target_audience", "") or "")
        context.tone_of_voice = str(data.get("tone_of_voice", "") or "")[:255]
        context.usp = str(data.get("usp", "") or "")
        context.insights_summary = str(data.get("insights_summary", "") or "")
        context.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(context)

        logger.info(f"[context_strategy] Паспорт сообщества сохранён (community_id={community_id})")
        return context
    finally:
        if own_db:
            db.close()


def generate_marketing_strategy(community_id: int, db: Optional[Session] = None) -> Optional[MarketingStrategy]:
    """Генерирует и сохраняет контент-стратегию сообщества (MarketingStrategy).

    Требует существующий CommunityContext (см. build_community_context).

    Args:
        community_id: ID записи PlatformAccount.
        db: Необязательная внешняя сессия БД.

    Returns:
        Сохранённый MarketingStrategy либо None при отсутствии контекста
        или невалидном JSON-ответе LLM.
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        context = (
            db.query(CommunityContext)
            .filter(CommunityContext.community_id == community_id)
            .order_by(CommunityContext.updated_at.desc())
            .first()
        )
        if context is None:
            logger.warning(
                f"[context_strategy] CommunityContext для community_id={community_id} отсутствует — "
                "сначала вызовите build_community_context()"
            )
            return None

        context_payload = {
            "target_audience": context.target_audience,
            "tone_of_voice": context.tone_of_voice,
            "usp": context.usp,
            "insights_summary": context.insights_summary,
        }
        prompt = STRATEGY_PROMPT_TEMPLATE.format(
            context_json=json.dumps(context_payload, ensure_ascii=False)
        )

        from services.content_service import _default_use_local

        try:
            raw = generate_text(prompt=prompt, system_prompt=JSON_SYSTEM_PROMPT, use_local=_default_use_local(db))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[context_strategy] Ошибка generate_text для стратегии: {type(e).__name__}: {e}")
            return None

        data = _parse_llm_json(raw)
        if data is None:
            return None

        strategy = (
            db.query(MarketingStrategy)
            .filter(MarketingStrategy.community_id == community_id)
            .first()
        )
        if strategy is None:
            strategy = MarketingStrategy(community_id=community_id)
            db.add(strategy)

        pillars = data.get("content_pillars")
        rubrics = data.get("rubrics")
        strategy.content_pillars = pillars if isinstance(pillars, list) else []
        strategy.rubrics = rubrics if isinstance(rubrics, list) else []
        strategy.posting_schedule = str(data.get("posting_schedule", "") or "")[:500]
        strategy.generated_at = datetime.utcnow()
        db.commit()
        db.refresh(strategy)

        logger.info(f"[context_strategy] Стратегия сообщества сохранена (community_id={community_id})")
        return strategy
    finally:
        if own_db:
            db.close()
