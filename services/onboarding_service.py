"""
Сервис «AI-консьержа» — онбординга нового пользователя.

Заменяет скучную форму регистрации на короткий диалог: консьерж узнаёт
название и суть сообщества, а затем извлекает структурированные данные
(goal / specifics / audience) из текста диалога отдельным вызовом LLM с
JSON-промптом и сохраняет их в CommunityGenerationContext
(is_configured=True). После этого пользователь перенаправляется
в основной интерфейс контент-менеджера с уже готовым контекстом.

Вспомогательные функции (state-хранилище диалога, парсер JSON, промпты)
используются и для варианта интеграции прямо в /content-manager.

ВАЖНО: generate_text() синхронный (HTTP к Ollama/GigaChat/cloud) — все
вызовы из FastAPI должны выполняться через asyncio.to_thread /
run_in_threadpool, чтобы не блокировать event loop.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from generators.text_generator import generate_text
from services.content_manager_service import get_or_create_generation_context

logger = logging.getLogger(__name__)

# Приветствие консьержа (пункт 3 задачи).
ONBOARDING_GREETING = (
    "Привет! Я твой ИИ-контент менеджер. Давай быстро настроим твое "
    "сообщество, чтобы я мог генерировать идеальные посты. Как называется "
    "твое сообщество и чем оно занимается?"
)

# Системный промпт консьержа: коротко, по-русски, НЕ выдумывать данные.
CONCIERGE_SYSTEM_PROMPT = (
    "Ты — дружелюбный ИИ-консьерж сервиса автопостинга «Autopilot Content». "
    "Твоя цель за 2–4 коротких вопроса узнать: название сообщества, чем оно "
    "занимается, его главную цель ведения соцсетей, целевую аудиторию и что "
    "уже публикуется/работает хорошо. Задавай не более одного-двух вопросов "
    "за сообщение, отвечай кратко по-русски, без markdown. Не выдумывай "
    "данные пользователя. Когда ответов достаточно, напиши ровно слово "
    "DONE и больше ничего."
)

# Промпт извлечения (пункт 5 задачи) — строго JSON.
EXTRACT_JSON_SYSTEM_PROMPT = (
    "Ты — аналитик. Отвечай ТОЛЬКО валидным JSON без markdown-обёрток "
    "(```) и пояснений."
)

EXTRACT_PROMPT_TEMPLATE = (
    "Извлеки из диалога ниже JSON с полями:\n"
    '{{\n  "community_name": "...",\n  "goal": "...",\n'
    '  "specifics": "...",\n  "audience": "..."\n}}\n'
    "Если каких-то данных нет, оставь в поле null.\n\n"
    "Диалог:\n{dialog}"
)


def extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Парсит JSON-объект из ответа LLM, терпимо к markdown-обёрткам.

    Возвращает dict либо None, если распознать JSON не удалось.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: первый сбалансированный { ... } блок.
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
                        try:
                            parsed = json.loads(cleaned[start: idx + 1])
                            if isinstance(parsed, dict):
                                return parsed
                        except (json.JSONDecodeError, ValueError):
                            break
        start = cleaned.find("{", start + 1)
    return None


def new_onboarding_state(user_id: int) -> Dict[str, Any]:
    """Создаёт пустое состояние диалога консьержа."""
    return {"user_id": user_id, "messages": [], "done": False}


class OnboardingStateStore:
    """In-memory хранилище состояний диалога консьержа (ключ — user_id).

    Диалог онбординга короткоживущий, поэтому БД не используется; при
    перезапуске процесса пользователь просто начинает заново.
    """

    def __init__(self) -> None:
        self._states: Dict[int, Dict[str, Any]] = {}

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self._states.get(user_id)

    def ensure(self, user_id: int) -> Dict[str, Any]:
        state = self._states.get(user_id)
        if state is None:
            state = new_onboarding_state(user_id)
            self._states[user_id] = state
        return state

    def reset(self, user_id: int) -> None:
        self._states.pop(user_id, None)

    def append(self, user_id: int, role: str, content: str) -> None:
        state = self.ensure(user_id)
        state["messages"].append({"role": role, "content": content})


# Глобальный стор (используется эндпоинтом /api/onboarding/chat в app.py).
onboarding_store = OnboardingStateStore()

# Минимальное число реплик пользователя, после которого имеет смысл
# пытаться извлечь данные (название+суть, цель, ЦА).
MIN_USER_TURNS_FOR_EXTRACTION = 2


def concierge_reply(dialog: List[Dict[str, str]], use_local: bool = True) -> str:
    """Один шаг диалога консьержа: возвращает следующий вопрос/реплику LLM.

    Args:
        dialog: список сообщений [{"role": "user"/"assistant", "content": ...}]
                (история целиком, последняя реплика — пользователя).
        use_local: фолбэк-переключатель провайдера LLM.

    Синхронная функция! Из async-роута вызывать только через to_thread.
    """
    transcript = "\n".join(
        f"{'Клиент' if m['role'] == 'user' else 'Консьерж'}: {m['content']}"
        for m in dialog
    )
    prompt = (
        f"{CONCIERGE_SYSTEM_PROMPT}\n\n"
        f"Текущий диалог:\n{transcript}\n\n"
        "Напиши следующий ответ ассистента (короткий вопрос или DONE)."
    )
    try:
        reply = generate_text(prompt=prompt, system_prompt=CONCIERGE_SYSTEM_PROMPT, use_local=use_local)
    except Exception as e:  # noqa: BLE001 — LLM не должен ронять онбординг
        logger.error(f"[onboarding] Ошибка generate_text (консьерж): {type(e).__name__}: {e}")
        # Fallback без LLM: задаём следующий вопрос из скрипта.
        user_turns = sum(1 for m in dialog if m["role"] == "user")
        fallback_questions = [
            "Отлично! А какая главная цель ведения сообщества — привлекать клиентов, "
            "повышать узнаваемость бренда или общаться с аудиторией?",
            "Кто ваша целевая аудитория — возраст, интересы, боли?",
            "Что уже публикуется и что из этого работает лучше всего?",
        ]
        if user_turns <= len(fallback_questions):
            return fallback_questions[min(user_turns, len(fallback_questions) - 1)]
        return "DONE"
    reply = (reply or "").strip()
    return reply or ONBOARDING_GREETING


def extract_onboarding_data(dialog: List[Dict[str, str]], use_local: bool = True) -> Optional[Dict[str, Any]]:
    """Извлекает структурированные данные из диалога отдельным вызовом LLM.

    Промпт (пункт 5 задачи): «Извлеки из диалога JSON с полями: goal,
    specifics, audience. Если чего-то нет, оставь null».

    Returns:
        dict с ключами community_name/goal/specifics/audience (значения —
        строки или None) либо None, если LLM вернула невалидный JSON.

    Синхронная функция! Из async-роута вызывать только через to_thread.
    """
    transcript = "\n".join(
        f"{'Клиент' if m['role'] == 'user' else 'Консьерж'}: {m['content']}"
        for m in dialog
    )
    prompt = EXTRACT_PROMPT_TEMPLATE.format(dialog=transcript)
    try:
        raw = generate_text(prompt=prompt, system_prompt=EXTRACT_JSON_SYSTEM_PROMPT, use_local=use_local)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[onboarding] Ошибка generate_text (извлечение): {type(e).__name__}: {e}")
        return None
    data = extract_json_object(raw)
    if data is None:
        logger.warning("[onboarding] LLM вернула невалидный JSON при извлечении данных")
        return None
    result: Dict[str, Any] = {}
    for key in ("community_name", "goal", "specifics", "audience"):
        value = data.get(key)
        result[key] = str(value).strip() if value not in (None, "", "null") else None
    return result


def save_onboarding_context(
    db: Session,
    user_community_id: int,
    extracted: Dict[str, Any],
) -> Any:
    """Сохраняет извлечённые данные в CommunityGenerationContext.

    Заполняет goal / community_specifics / target_audience (плюс current_state,
    если LLM что-то извлёла) и ставит is_configured=True. Существующие
    непустые значения не перетираются на None.

    Returns:
        Обновлённый CommunityGenerationContext.
    """
    ctx = get_or_create_generation_context(db, user_community_id)

    def _apply(field: str, value: Optional[str]) -> None:
        if value and not getattr(ctx, field, None):
            setattr(ctx, field, value)

    _apply("goal", extracted.get("goal"))
    _apply("community_specifics", extracted.get("specifics"))
    _apply("target_audience", extracted.get("audience"))
    _apply("current_state_and_what_works", extracted.get("current_state"))

    ctx.is_configured = True
    db.commit()
    db.refresh(ctx)
    logger.info(
        f"[onboarding] Контекст сообщества id={user_community_id} настроен через AI-консьержа"
    )
    return ctx


def needs_onboarding(db: Session, user_id: int) -> bool:
    """True, если у пользователя нет ни одного сообщества ЛИБО хотя бы у
    одного из его сообществ не заполнен CommunityGenerationContext
    (is_configured=False). Используется для показа онбординга вместо
    обычного интерфейса /content-manager."""
    from models import CommunityGenerationContext as CGC
    from models import UserCommunity as UC

    ids = [row.id for row in db.query(UC.id).filter(UC.user_id == user_id).all()]
    if not ids:
        return True
    configured_ids = {
        r[0]
        for r in db.query(CGC.user_community_id)
        .filter(CGC.user_community_id.in_(ids), CGC.is_configured.is_(True))
        .all()
    }
    return any(cid not in configured_ids for cid in ids)
