"""
Сервис «AI-консьержа» — онбординга нового пользователя.

Сценарий настройки:
1. Пользователь выбирает добавленное сообщество VK (UserCommunity) из списка.
2. Система автоматически собирает известные данные (parse_community_data):
   - описание группы и исторические посты -> AI-паспорт CommunityContext
     (services/context_strategy_service.build_community_context);
   - целевая аудитория, tone of voice, УТП, инсайты;
   - рубрики и контент-столпы из MarketingStrategy (если стратегия есть).
3. Консьерж задаёт вопросы ТОЛЬКО по недостающим полям (goal, specifics,
   current_state, image_preferences), предлагая кнопки-варианты ответов.
4. Ответы сохраняются в CommunityGenerationContext (is_configured=True),
   при отсутствии CommunityContext он создаётся из собранных данных,
   после чего следует редирект в контент-менеджер (?onboarded=1).

Если сообщество не выбрано или парсинг не дал данных, сохраняется
классический свободный текстовый диалог с извлечением JSON (fallback).

ВАЖНО: generate_text() и обращения к VK API синхронные — все
вызовы из FastAPI должны выполняться через asyncio.to_thread /
run_in_threadpool, чтобы не блокировать event loop.
"""
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from generators.text_generator import generate_text
from services.content_manager_service import get_or_create_generation_context

logger = logging.getLogger(__name__)

# Сколько последних реплик пользователя учитывать при детерминированном
# сопоставлении ответа с кнопками/ключами источников картинок.
RECENT_USER_TURNS_FOR_MATCHING = 3

# Fallback-парсинг завершённых диалогов может прийти позже, чем новый
# диалог начал сбор данных, — игнорируем устаревшие задачи старше 15 минут.
PARSE_TASK_TTL_SECONDS = 900

# Если за это время фоновый парсинг не завершился, фронтенд показывает
# сообщение и переходит на обычный текстовый диалог (см. onboarding.html).
PARSING_TIMEOUT_MS = 30000

# Приветствие консьержа (старый сценарий — свободный диалог без выбора
# сообщества; используется как fallback).
ONBOARDING_GREETING = (
    "Привет! Я твой ИИ-контент менеджер. Давай быстро настроим твое "
    "сообщество, чтобы я мог генерировать идеальные посты. Как называется "
    "твое сообщество и чем оно занимается?"
)

# Приветствие нового сценария: пользователь выбирает сообщество из списка,
# после чего данные парсятся автоматически (ШАГ 1).
ONBOARDING_GREETING_WITH_SELECTION = (
    "Привет! Я твой ИИ-контент менеджер 🤖 Выбери сообщество из списка "
    "выше — я автоматически проанализирую его данные ВКонтакте и свою базу "
    "знаний, а задам вопросы только о том, что не смог узнать сам."
)

# Сообщение при выборе сообщества (до старта фонового парсинга).
PARSING_MESSAGE = "⏳ Анализирую ваше сообщество ВКонтакте… Это займёт пару секунд."


# --- Каталог кнопок-подсказок по недостающим полям ------------------------

GOAL_BUTTONS: List[Dict[str, str]] = [
    {"label": "🎯 Привлекать новых клиентов", "value": "Привлечение новых клиентов через контент"},
    {"label": "💬 Увеличить вовлеченность", "value": "Увеличение вовлеченности и лояльности аудитории"},
    {"label": "📦 Продвигать товар/услугу", "value": "Прямые продажи конкретных товаров и услуг через соцсети"},
    {"label": "⭐ Повысить узнаваемость", "value": "Укрепление бренда и повышение узнаваемости"},
]

SPECIFICS_BUTTONS: List[Dict[str, str]] = [
    {"label": "🎨 Уникальный дизайн, ручная работа", "value": "Уникальный дизайн, ручная работа, возможность кастомизации"},
    {"label": "⚡ Быстрое выполнение заказов", "value": "Быстрое и срочное выполнение заказов"},
    {"label": "🌿 Эко-продукты, натуральные ингредиенты", "value": "Экологичность и натуральные материалы/ингредиенты"},
    {"label": "💰 Доступные цены, акции", "value": "Доступные цены, регулярные акции и скидки"},
]

CURRENT_STATE_BUTTONS: List[Dict[str, str]] = [
    {"label": "📸 Фото готовых работ/товаров", "value": "Фотографии готовых работ и товаров"},
    {"label": "🎥 Видео процесса создания", "value": "Видео процесса создания и закулисье"},
    {"label": "📝 Отзывы клиентов", "value": "Отзывы и кейсы довольных клиентов"},
    {"label": "📊 Полезные советы и экспертный контент", "value": "Полезные советы и экспертные статьи"},
    {"label": "❓ Пока ничего не публикую", "value": "Публикаций пока нет, начинаю с нуля"},
]

IMAGE_PREFS_BUTTONS: List[Dict[str, str]] = [
    {"label": "🤖 AI-генерация (уникальные)", "value": "Только AI-генерация уникальных изображений",
     "sources": ["ai"]},
    {"label": "🔍 Поиск в интернете", "value": "Подходящие картинки из интернета",
     "sources": ["web_search"]},
    {"label": "📤 Загружаю свои фото", "value": "Использую только свои загруженные фотографии",
     "sources": ["user_upload"]},
    {"label": "🎨 Комбинация AI + свои", "value": "Комбинация: AI-генерация плюс мои фотографии",
     "sources": ["ai", "user_upload"]},
]

# Кнопки для свободного диалога (сообщество ещё не добавлено / неизвестно).
NEW_COMMUNITY_TYPE_BUTTONS: List[Dict[str, str]] = [
    {"label": "🍰 Кондитерская / пекарня", "value": "Кондитерская: торты и десерты на заказ"},
    {"label": "🎨 Handmade / творческая мастерская", "value": "Мастерская handmade: украшения и изделия ручной работы"},
    {"label": "💻 IT-стартап / digital-услуги", "value": "IT-компания: цифровые продукты и услуги"},
    {"label": "☕ Кафе / ресторан", "value": "Кафе или ресторан: кухня и атмосфера"},
    {"label": "🛍️ Интернет-магазин", "value": "Интернет-магазин товаров"},
]

# Поле -> набор кнопок (порядок обхода вопросов онбординга).
QUESTION_FIELDS: List[str] = ["goal", "specifics", "current_state", "image_preferences"]

BUTTONS_BY_FIELD: Dict[str, List[Dict[str, str]]] = {
    "goal": GOAL_BUTTONS,
    "specifics": SPECIFICS_BUTTONS,
    "current_state": CURRENT_STATE_BUTTONS,
    "image_preferences": IMAGE_PREFS_BUTTONS,
}

# Значение кнопки «✏️ Свой вариант…» — подставляет подсказку в textarea,
# пользователь редактирует текст и отправляет вручную.
CUSTOM_BUTTON = {"label": "✏️ Свой вариант…", "value": "", "custom": True}

# Ключи ответов -> нормализованные источники картинок
# (совпадают с допустимыми значениями CommunityGenerationContext.image_source_preferences).
IMAGE_SOURCE_ALIASES: Dict[str, str] = {
    "ai": "ai", "ии": "ai", "нейросет": "ai", "генерац": "ai",
    "web": "web_search", "поиск": "web_search", "интернет": "web_search",
    "upload": "user_upload", "свой": "user_upload", "свои": "user_upload",
    "мои": "user_upload", "загруж": "user_upload", "фотографи": "user_upload",
    "комбин": "mix", "смеш": "mix", "микс": "mix", "comb": "mix",
}

# Детерминированные ключевые слова для распознавания ответов по кнопкам,
# если пользователь отредактировал подставленный текст.
FIELD_KEYWORDS: Dict[str, List[str]] = {
    "goal": [
        "клиент", "новым", "новый", "привлеч",
        "вовлеч", "лояльн", "аудитори",
        "продаж", "товар", "услуг",
        "узнаваем", "бренд",
    ],
    "specifics": [
        "дизайн", "ручная", "кастом", "уникальн",
        "быстр", "срочн",
        "эко", "натуральн",
        "цен", "скидк", "акци",
    ],
    "current_state": [
        "фото",
        "видео", "закулись",
        "отзыв", "кейс",
        "совет", "эксперт",
        "ничего", "нуля", "нет публи",
    ],
}


def _norm(s: Any) -> str:
    """Нормализует строку для нечувствительного к регистру поиска."""
    return str(s or "").lower().replace("ё", "е")


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


# --- Умные вопросы по недостающим полям (со смарт-подсказками) ------------


def _find_button_match(field: str, message: str) -> Optional[Dict[str, Any]]:
    """Ищет совпадение ответа пользователя с кнопками поля.

    Сначала точное совпадение value кнопки (клик без редактирования),
    затем нечувствительный к регистру поиск подстроки.
    """
    norm_msg = _norm(message)
    buttons = BUTTONS_BY_FIELD.get(field) or []
    for btn in buttons:
        if norm_msg == _norm(btn["value"]):
            return btn
    for btn in buttons:
        v = _norm(btn["value"])
        if v and (v in norm_msg or norm_msg in v):
            return btn
    return None


def _match_button_index(field: str, message: str) -> Optional[int]:
    """Определяет индекс кнопки по ответу: точное/подстрочное совпадение
    value или совпадение label; затем — порядковое число («2», «второй»)."""
    norm_msg = _norm(message)
    buttons = BUTTONS_BY_FIELD.get(field) or []
    for idx, btn in enumerate(buttons):
        v, l = _norm(btn["value"]), _norm(btn["label"])
        if norm_msg == v or norm_msg == l or (v and v in norm_msg) or (l and l in norm_msg):
            return idx
    m = re.search(r"\b(\d{1,2})\b", norm_msg)
    if m:
        n = int(m.group(1))
        if 1 <= n <= len(buttons):
            return n - 1
    words = {
        "перв": 0, "втор": 1, "трет": 2, "четвер": 3, "пят": 4, "шест": 5,
    }
    for w, idx in words.items():
        if w in norm_msg and idx < len(buttons):
            return idx
    return None


def parse_image_sources(text: str) -> List[str]:
    """Извлекает источники картинок из произвольного текста ответа.

    Распознаёт ключевые слова (ai/нейросеть, поиск/интернет, свои фото,
    комбинация) и возвращает список допустимых значений
    ["ai", "web_search", "user_upload"]. Пустой список — не распознано.
    """
    norm = _norm(text)
    found: List[str] = []
    for alias, key in IMAGE_SOURCE_ALIASES.items():
        if alias in norm:
            if key == "mix":
                for extra in ("ai", "user_upload"):
                    if extra not in found:
                        found.append(extra)
            elif key not in found:
                found.append(key)
    ordered = [s for s in ("ai", "web_search", "user_upload") if s in found]
    return ordered


def _answer_matches_field(field: str, answer: str) -> bool:
    """True, если ответ содержит ключевые слова вариантов ответов поля."""
    norm_answer = _norm(answer)
    for kw_group in FIELD_KEYWORDS.get(field, []):
        if kw_group in norm_answer:
            return True
    # Совпадение с текстом любой кнопки этого поля
    for btn in BUTTONS_BY_FIELD.get(field, []):
        v = _norm(btn["value"])
        if v and v in norm_answer:
            return True
    return False


def _has_meaningful_answer(state: Dict[str, Any], field: str) -> bool:
    """Есть ли осмысленный ответ по полю: детерминированный черновик из
    умного диалога (record_answer) либо внятная реплика в свободном диалоге
    (не клик по кнопке «Свой вариант…» и не одно слово-паразит)."""
    draft_key = {
        "goal": "goal",
        "specifics": "specifics",
        "current_state": "current_state",
        "image_preferences": "image_source_preferences",
    }.get(field)
    if draft_key is not None:
        value = (state.get("answers_draft") or {}).get(draft_key)
        if isinstance(value, (list, tuple)):
            if any(str(v).strip() for v in value):
                return True
        elif str(value or "").strip():
            return True
    messages = state.get("messages", [])
    # Ответ по полю считается только реплика, сказанная ПОСЛЕ того, как бот
    # задал вопрос именно этому полю (meta.field == field). Иначе короткие
    # ответы на предыдущие вопросы («Фото…», «Отзывы…») ложно матчатся
    # ключевыми словами следующих полей и шаги пропускаются.
    last_q_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        meta = messages[i].get("meta") or {}
        if messages[i].get("role") == "assistant" and meta.get("field") == field:
            last_q_idx = i
            break
    if last_q_idx == -1:
        return False
    candidates = messages[last_q_idx + 1:]
    for msg in candidates:
        if msg.get("role") != "user":
            continue
        text = (msg.get("content") or "").strip()
        if len(text) < 3:
            continue
        low = _norm(text)
        if low in ("свой вариант", "свой вариант…", "свой", "другое", "своё"):
            continue
        if field == "image_preferences":
            if parse_image_sources(text):
                return True
        elif _answer_matches_field(field, text):
            return True
    return False


def missing_onboarding_fields(parsed_data: Optional[Dict[str, Any]], state: Dict[str, Any]) -> List[str]:
    """Вычисляет список полей, о которых нужно спросить пользователя.

    Поле считается заполненным, если оно уже есть в CommunityGenerationContext,
    получено из VK/базы знаний (parsed_data) или внятный ответ прозвучал
    в текущем диалоге.
    """
    parsed = parsed_data or {}
    existing = state.get("existing_context") or {}
    missing: List[str] = []
    for field in QUESTION_FIELDS:
        if str(existing.get(field) or "").strip():
            continue
        if field == "target_audience":
            continue  # спрашиваем только через goal/current_state, не отдельным полем
        if field == "goal" and str(parsed.get("usp") or "").strip():
            pass  # УТП помогает сформулировать цель, но цель всё равно уточняем
        if _has_meaningful_answer(state, field):
            continue
        missing.append(field)
    return missing


def build_parsed_summary(parsed_data: Dict[str, Any]) -> str:
    """Короткое человекочитаемое резюме автоматически собранных данных
    (что показать пользователю перед вопросами)."""
    lines: List[str] = []
    audience = (parsed_data.get("target_audience") or "").strip()
    tone = (parsed_data.get("tone_of_voice") or "").strip()
    usp = (parsed_data.get("usp") or "").strip()
    insights = (parsed_data.get("insights_summary") or "").strip()
    rubrics = parsed_data.get("rubrics") or []
    pillars = parsed_data.get("content_pillars") or []
    description = (parsed_data.get("community_description") or "").strip()

    if description:
        first_sentence = re.split(r"(?<=[.!?])\s", description)[0][:200]
        lines.append(f"Чем занимается сообщество: {first_sentence}")
    if audience:
        lines.append(f"Целевая аудитория: {audience[:250]}")
    if usp:
        lines.append(f"Сильная сторона (УТП): {usp[:200]}")
    if tone:
        lines.append(f"Стиль общения: {tone[:120]}")
    if insights:
        lines.append(f"Что показывает аналитика постов: {insights[:280]}")
    if rubrics:
        names = [
            str(r.get("name")) for r in rubrics
            if isinstance(r, dict) and r.get("name")
        ]
        if names:
            lines.append(f"Рубрики из стратегии: {', '.join(names[:5])}")
    if pillars:
        lines.append(f"Контент-столпы: {', '.join(str(p) for p in pillars[:4])}")
    return "\n".join(lines)


def generate_question_with_buttons(
    missing_field: str,
    parsed_data: Optional[Dict[str, Any]] = None,
    question_index: int = 1,
    total_questions: int = 4,
    community_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Формирует вопрос по недостающему полю + кнопки-варианты ответа.

    Returns:
        {
          "question": текст вопроса (без markdown),
          "buttons": [{"label": ..., "value": ..., "sources": [...]}, ...],
          "field": имя заполняемого поля,
          "progress": {"index": N, "total": M},
        }
    Синхронная, чистая функция (LLM не вызывает).
    """
    parsed = parsed_data or {}
    name = community_name or parsed.get("community_name") or "вашего сообщества"
    audience = (parsed.get("target_audience") or "").strip()
    usp = (parsed.get("usp") or "").strip()

    def _ctx_intro() -> str:
        bits = []
        if audience:
            bits.append(f"аудитория — {audience[:120]}")
        if usp:
            bits.append(f"сильная сторона — {usp[:100]}")
        if bits:
            return "Я уже вижу, что " + ", а ".join(bits) + ". "
        return ""

    if missing_field == "goal":
        question = (
            f"Отлично! Я проанализировал сообщество «{name}». {_ctx_intro()}"
            "Давайте уточним главную цель ведения сообщества:"
        )
        buttons = GOAL_BUTTONS
    elif missing_field == "specifics":
        question = (
            f"Понял! Теперь расскажите о специфике «{name}» — что делает вас "
            "особенными для подписчиков?"
        )
        buttons = SPECIFICS_BUTTONS
    elif missing_field == "current_state":
        question = (
            f"Отлично! Что из вашего текущего контента в «{name}» работает "
            "лучше всего?"
        )
        buttons = CURRENT_STATE_BUTTONS
    elif missing_field == "image_preferences":
        question = (
            "Последний вопрос — какие изображения предпочитаете использовать "
            "в постах?"
        )
        buttons = IMAGE_PREFS_BUTTONS
    else:
        question = (
            f"Расскажите чуть больше о сообществе «{name}», чтобы я лучше "
            "подбирал контент."
        )
        buttons = NEW_COMMUNITY_TYPE_BUTTONS
        missing_field = "specifics"

    payload_buttons = [dict(b) for b in buttons] + [dict(CUSTOM_BUTTON)]
    return {
        "question": question,
        "buttons": payload_buttons,
        "field": missing_field,
        "progress": {"index": question_index, "total": max(total_questions, question_index)},
    }


def next_smart_step(
    state: Dict[str, Any],
    parsed_data: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Следующий недополненный вопрос умного сценария (или None, если всё
    собрано / сценарий неактивен)."""
    if not state.get("smart_mode"):
        return None
    missing = missing_onboarding_fields(parsed_data, state)
    if not missing:
        return None
    field = missing[0]
    asked = state.get("asked_fields") or {}
    q_no = len(asked) + 1
    total = q_no + len(missing) - 1
    step = generate_question_with_buttons(
        field, parsed_data, q_no, total,
        community_name=(parsed_data or {}).get("community_name"),
    )
    return step


def record_answer(state: Dict[str, Any], message: str) -> None:
    """Обновляет черновик ответов состояния по текущему вопросу: сопоставляет
    ответ с кнопками (детерминированно), иначе сохраняет текст как есть."""
    current_field = state.get("current_question_field")
    if not current_field:
        return
    draft = state.setdefault("answers_draft", {})
    if current_field == "image_preferences":
        idx = _match_button_index(current_field, message)
        sources: Optional[List[str]] = None
        if idx is not None:
            btn = IMAGE_PREFS_BUTTONS[idx]
            sources = list(btn.get("sources") or [])
        if not sources:
            sources = parse_image_sources(message)
        if sources:
            draft["image_source_preferences"] = sources
        else:
            draft["image_preferences_text"] = message.strip()[:500]
    else:
        btn = _find_button_match(current_field, message)
        value = btn["value"] if btn else message.strip()
        target_key = {
            "goal": "goal",
            "specifics": "specifics",
            "current_state": "current_state",
        }.get(current_field)
        if target_key and value:
            draft[target_key] = value[:1000]
    state.setdefault("asked_fields", {})[current_field] = True


def reassign_answer_to_matching_field(state: Dict[str, Any], message: str) -> None:
    """Если ответ пользователя матчится с кнопками другого ещё не отвеченного
    поля — переключаем текущий вопрос на это поле.

    Ситуация: кнопки текущего вопроса остаются видимыми после отправки
    ответа; пользователь мог нажать кнопку следующего вопроса и отправить
    её значение раньше времени. Без переключения такой ответ был бы
    записан в неверное поле.
    """
    asked = state.get("asked_fields") or {}
    current = state.get("current_question_field")
    if not current:
        return
    norm_msg = _norm(message)
    if not norm_msg:
        return
    # 1) Точное совпадение со значением/подписью кнопки другого поля —
    # пользователь явно нажал кнопку следующего вопроса.
    for field in QUESTION_FIELDS:
        if field == current or asked.get(field):
            continue
        buttons = BUTTONS_BY_FIELD.get(field) or []
        matched = any(
            norm_msg == _norm(btn["value"])
            or (btn.get("label") and norm_msg == _norm(btn["label"]))
            for btn in buttons
        )
        if matched:
            state["current_question_field"] = field
            return
    # 2) Подстрочное совпадение — но только если реплика НЕ матчится с
    # кнопками текущего поля (иначе это отредактированный ответ на текущий
    # вопрос, и переключать поле нельзя).
    if _find_button_match(current, message) is not None:
        return
    for field in QUESTION_FIELDS:
        if field == current or asked.get(field):
            continue
        buttons = BUTTONS_BY_FIELD.get(field) or []
        matched = any(
            (_norm(btn["value"]) and _norm(btn["value"]) in norm_msg)
            or (btn.get("label") and _norm(btn["label"]) in norm_msg)
            for btn in buttons
        )
        if field == "image_preferences" and not matched:
            matched = bool(parse_image_sources(message))
        if matched:
            state["current_question_field"] = field
            return


# --- Фоновый парсинг данных сообщества -------------------------------------


class ParseTaskStore:
    """In-memory хранилище фоновых задач парсинга (ключ — task_id).

    Диалог онбординга короткоживущий; при перезапуске процесса задачи
    теряются, фронтенд по таймауту переключается на обычный диалог.
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, user_id: int, community_pk: int) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex
        task = {
            "task_id": task_id,
            "user_id": user_id,
            "community_pk": community_pk,
            "status": "parsing",   # parsing | done | error
            "data": None,
            "error": None,
            "started_at": datetime.utcnow(),
        }
        with self._lock:
            self._prune_locked()
            self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._tasks.get(task_id)

    def latest_for_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            user_tasks = [t for t in self._tasks.values() if t["user_id"] == user_id]
        if not user_tasks:
            return None
        return max(user_tasks, key=lambda t: t["started_at"])

    def finish(self, task_id: str, data: Optional[Dict[str, Any]], error: Optional[str] = None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task["status"] = "error" if error else "done"
            task["data"] = data
            task["error"] = error

    def _prune_locked(self) -> None:
        stale = [
            tid for tid, t in self._tasks.items()
            if (datetime.utcnow() - t["started_at"]).total_seconds() > PARSE_TASK_TTL_SECONDS
        ]
        for tid in stale:
            self._tasks.pop(tid, None)


parse_task_store = ParseTaskStore()


def ensure_platform_account(db: Session, user_community: Any) -> Optional[int]:
    """Возвращает PlatformAccount.id для UserCommunity (создаёт при отсутствии).

    Модели аналитики (HistoricalPost / CommunityContext / MarketingStrategy)
    привязаны к PlatformAccount, поэтому перед парсингом убеждаемся, что
    запись существует, и обновляем в ней токен сообщества.
    """
    from models import PlatformAccount

    group_id = abs(int(getattr(user_community, "group_id", 0) or 0))
    if group_id == 0:
        return None
    account = (
        db.query(PlatformAccount)
        .filter(
            PlatformAccount.platform == "vk",
            PlatformAccount.account_id == str(group_id),
        )
        .first()
    )
    if account is None:
        account = PlatformAccount(
            platform="vk",
            account_id=str(group_id),
            access_token=getattr(user_community, "group_token", "") or "",
            config_json={"description": getattr(user_community, "group_name", "") or ""},
        )
        db.add(account)
        db.commit()
        db.refresh(account)
    else:
        token = getattr(user_community, "group_token", "") or ""
        if token and account.access_token != token:
            account.access_token = token
            db.commit()
    return account.id


def collect_known_data(db: Session, user_community: Any, account_id: int) -> Dict[str, Any]:
    """Собирает известные о сообществе данные БЕД вызовов LLM/VK (быстро).

    Источники: UserCommunity, существующий CommunityContext,
    MarketingStrategy, описание группы из PlatformAccount.config_json.
    """
    from models import CommunityContext, MarketingStrategy, PlatformAccount

    data: Dict[str, Any] = {
        "community_name": getattr(user_community, "group_name", None),
        "community_description": None,
        "target_audience": None,
        "tone_of_voice": None,
        "usp": None,
        "insights_summary": None,
        "rubrics": [],
        "content_pillars": [],
        "posting_schedule": None,
        "has_history": False,
        "context_rebuilt": False,
        "source": "db",
    }

    account = db.get(PlatformAccount, account_id)
    cfg = getattr(account, "config_json", None)
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    if isinstance(cfg, dict):
        desc = str(cfg.get("description") or "").strip()
        if desc and desc.lower() != "описание отсутствует":
            data["community_description"] = desc

    ctx_row = (
        db.query(CommunityContext)
        .filter(CommunityContext.community_id == account_id)
        .order_by(CommunityContext.updated_at.desc())
        .first()
    )
    if ctx_row is not None:
        data["target_audience"] = (ctx_row.target_audience or "").strip() or None
        data["tone_of_voice"] = (ctx_row.tone_of_voice or "").strip() or None
        data["usp"] = (ctx_row.usp or "").strip() or None
        data["insights_summary"] = (ctx_row.insights_summary or "").strip() or None

    strategy = (
        db.query(MarketingStrategy)
        .filter(MarketingStrategy.community_id == account_id)
        .first()
    )
    if strategy is not None:
        data["rubrics"] = list(strategy.rubrics or [])
        data["content_pillars"] = list(strategy.content_pillars or [])
        data["posting_schedule"] = (strategy.posting_schedule or "").strip() or None

    from models import HistoricalPost

    data["has_history"] = (
        db.query(HistoricalPost.id)
        .filter(HistoricalPost.community_id == account_id)
        .first() is not None
    )
    return data


def parse_community_data(db: Session, user_community: Any, use_local: bool = True) -> Dict[str, Any]:
    """Собирает данные о сообществе: VK API + AI-паспорт + база знаний.

    Шаги (синхронная функция — из async-роута вызывать через to_thread):
    1. Обеспечиваем PlatformAccount для UserCommunity.
    2. Быстрый сбор известных данных из БД (кэширование: существующий
       CommunityContext не пересобирается).
    3. Если паспорта нет — build_community_context() (groups.getById +
       исторические посты -> LLM). При пустой истории сначала пробуем
       импортировать wall.get из VK.
    4. Если есть MarketingStrategy — добавляем рубрики/контент-столпы.

    Ошибки VK/LLM не пробрасываются: возвращаем то, что удалось собрать
    (fallback на обычный диалог обеспечивает консьерж).
    """
    data: Dict[str, Any] = {
        "community_name": getattr(user_community, "group_name", None),
        "community_description": None,
        "target_audience": None,
        "tone_of_voice": None,
        "usp": None,
        "insights_summary": None,
        "rubrics": [],
        "content_pillars": [],
        "posting_schedule": None,
        "has_history": False,
        "context_rebuilt": False,
        "source": "none",
    }

    account_id = ensure_platform_account(db, user_community)
    if account_id is None:
        data["error"] = "Сообщество ещё не привязано к VK (нет group_id)"
        return data

    try:
        known = collect_known_data(db, user_community, account_id)
        data.update({k: v for k, v in known.items() if v})
    except Exception as e:  # noqa: BLE001
        logger.error(f"[onboarding.parse] Ошибка сбора известных данных: {type(e).__name__}: {e}")

    # A. Из VK API: описание группы (groups.getById fields="description").
    try:
        from services.vk_analytics_service import get_group_info

        gid = abs(int(getattr(user_community, "group_id", 0) or 0))
        info = get_group_info(
            getattr(user_community, "group_token", "") or "", gid,
            fields="description,members_count",
        )
        description = (info.get("description") or info.get("wiki") or "").strip()
        if description:
            data["community_description"] = description
            data["members_count"] = int(info.get("members_count") or 0) or None
        vk_name = (info.get("name") or "").strip()
        if vk_name:
            data["community_name"] = vk_name
        data["source"] = "vk"
    except Exception as e:  # noqa: BLE001 — VK недоступен не должен ронять онбординг
        logger.error(f"[onboarding.parse] groups.getById не удался: {type(e).__name__}: {e}")
        data["vk_error"] = True

    # Исторические посты: при отсутствии — пробуем импорт из wall.get.
    if not data.get("has_history"):
        try:
            from services.vk_analytics_service import import_history

            imported = import_history(account_id, limit=50, db=db)
            data["imported_posts"] = imported
            data["has_history"] = bool(imported)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[onboarding.parse] import_history не удался: {type(e).__name__}: {e}")

    # AI-паспорт сообщества: кэшируем — пересобираем только если его нет.
    if not (data.get("target_audience") or data.get("usp") or data.get("tone_of_voice")):
        if data.get("has_history"):
            try:
                from services.context_strategy_service import build_community_context

                passport = build_community_context(account_id, db=db)
                if passport is not None:
                    data["target_audience"] = (passport.target_audience or "").strip() or data.get("target_audience")
                    data["tone_of_voice"] = (passport.tone_of_voice or "").strip() or data.get("tone_of_voice")
                    data["usp"] = (passport.usp or "").strip() or data.get("usp")
                    data["insights_summary"] = (passport.insights_summary or "").strip() or data.get("insights_summary")
                    data["context_rebuilt"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"[onboarding.parse] build_community_context не удался: {type(e).__name__}: {e}")

    # B. Внутренняя база знаний: MarketingStrategy (рубрики, столпы, график).
    if not data.get("rubrics"):
        try:
            known = collect_known_data(db, user_community, account_id)
            if known.get("rubrics"):
                data["rubrics"] = known["rubrics"]
                data["content_pillars"] = known.get("content_pillars") or []
                data["posting_schedule"] = known.get("posting_schedule")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[onboarding.parse] Чтение MarketingStrategy не удалась: {type(e).__name__}: {e}")

    return data


def run_parse_task(task_id: str, user_id: int, community_pk: int, use_local: bool) -> None:
    """Тело фонового задачи парсинга (вызывается в worker-потоке).

    Создаёт собственную сессию БД (SessionLocal) — нельзя использовать
    сессию запроса из другого потока.
    """
    from database import SessionLocal

    db = SessionLocal()
    try:
        from models import UserCommunity

        uc = (
            db.query(UserCommunity)
            .filter(UserCommunity.id == community_pk, UserCommunity.user_id == user_id)
            .first()
        )
        if uc is None:
            parse_task_store.finish(task_id, None, error="Сообщество не найдено")
            return
        data = parse_community_data(db, uc, use_local=use_local)
        parse_task_store.finish(task_id, data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[onboarding.parse] Фоновая задача {task_id} упала: {type(e).__name__}: {e}")
        parse_task_store.finish(task_id, None, error=str(e)[:300])
    finally:
        db.close()


def apply_parsed_to_state(state: Dict[str, Any], parsed: Optional[Dict[str, Any]]) -> None:
    """Кладёт результат парсинга в состояние диалога и включает умный режим."""
    state["parsed_data"] = parsed or {}
    state["smart_mode"] = True
    state["asked_fields"] = {}
    state["answers_draft"] = {}


def apply_task_result_to_state(state: Dict[str, Any], task: Dict[str, Any]) -> None:
    task_data = task.get("data")
    if task.get("status") == "done":
        apply_parsed_to_state(state, task_data)
    else:
        # Ошибка парсинга — fallback: свободный диалог без авто-данных.
        apply_parsed_to_state(state, {"community_name": (task_data or {}).get("community_name")})
        state["parse_error"] = task.get("error") or "Не удалось проанализировать сообщество"


def concierge_reply(dialog: List[Dict[str, str]], use_local: bool = True,
                    parsed_data: Optional[Dict[str, Any]] = None) -> str:
    """Один шаг диалога консьержа: возвращает следующий вопрос/реплику LLM.

    Args:
        dialog: список сообщений [{"role": "user"/"assistant", "content": ...}]
                (история целиком, последняя реплика — пользователя).
        use_local: фолбэк-переключатель провайдера LLM.
        parsed_data: автоматически собранные данные сообщества. Если переданы —
                LLM сообщает о них и НЕ задаёт вопросы по уже известным полям
                (запрашивает только недостающее).

    Синхронная функция! Из async-роута вызывать только через to_thread.
    """
    transcript = "\n".join(
        f"{'Клиент' if m['role'] == 'user' else 'Консьерж'}: {m['content']}"
        for m in dialog
    )
    known_block = ""
    avoid_hint = ""
    if parsed_data:
        summary = build_parsed_summary(parsed_data)
        if summary:
            known_block = f"\n\nУже известные данные (НЕ спрашивай о них):\n{summary}\n"
            filled = [
                k for k in ("target_audience", "tone_of_voice", "usp", "insights_summary")
                if str(parsed_data.get(k) or "").strip()
            ]
            if filled:
                avoid_hint = f"\nЭти поля уже заполнены: {', '.join(filled)}."
    prompt = (
        f"{CONCIERGE_SYSTEM_PROMPT}\n\n"
        f"Текущий диалог:\n{transcript}\n{known_block}{avoid_hint}\n\n"
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


def extract_onboarding_data(dialog: List[Dict[str, str]], use_local: bool = True,
                            parsed_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Извлекает структурированные данные из диалога отдельным вызовом LLM.

    Промпт (пункт 5 задачи): «Извлеки из диалога JSON с полями: goal,
    specifics, audience. Если чего-то нет, оставь null».

    Args:
        dialog: история диалога консьержа.
        use_local: провайдер LLM.
        parsed_data: автоматически собранные данные сообщества — добавляются
            в промпт как известные и подмешиваются в результат, если LLM
            ничего не извлекла по соответствующему полю.

    Returns:
        dict с ключами community_name/goal/specifics/audience/current_state/
        image_source_preferences (значения — строки/списки или None) либо
        None, если LLM вернула невалидный JSON.

    Синхронная функция! Из async-роута вызывать только через to_thread.
    """
    transcript = "\n".join(
        f"{'Клиент' if m['role'] == 'user' else 'Консьерж'}: {m['content']}"
        for m in dialog
    )
    prompt = EXTRACT_PROMPT_TEMPLATE.format(dialog=transcript)
    if parsed_data:
        summary = build_parsed_summary(parsed_data)
        if summary:
            prompt += (
                "\n\nАвтоматически собранные данные о сообществе "
                "(используй их, если в диалоге поля не упомянуты):\n" + summary
            )
    try:
        raw = generate_text(prompt=prompt, system_prompt=EXTRACT_JSON_SYSTEM_PROMPT, use_local=use_local)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[onboarding] Ошибка generate_text (извлечение): {type(e).__name__}: {e}")
        raw = None
    data = extract_json_object(raw) if raw is not None else None
    if data is None and not parsed_data:
        logger.warning("[onboarding] LLM вернула невалидный JSON при извлечении данных")
        return None
    result: Dict[str, Any] = {}
    for key in ("community_name", "goal", "specifics", "audience"):
        value = (data or {}).get(key)
        result[key] = str(value).strip() if value not in (None, "", "null") else None
    # Текущее состояние: из LLM либо распознаём по последним репликам диалога.
    cs_value = (data or {}).get("current_state")
    result["current_state"] = (
        str(cs_value).strip() if cs_value not in (None, "", "null") else None
    )
    if not result["current_state"]:
        recent = [m["content"] for m in dialog if m["role"] == "user"][-RECENT_USER_TURNS_FOR_MATCHING:]
        for text in reversed(recent):
            if _answer_matches_field("current_state", text):
                result["current_state"] = text.strip()[:1000]
                break
    # Предпочтения по картинкам: приоритет — детерминированный разбор ответа
    # на вопрос с кнопками (он точнее свободной интерпретации LLM).
    sources: List[str] = []
    recent_msgs = [m["content"] for m in dialog if m["role"] == "user"][-RECENT_USER_TURNS_FOR_MATCHING:]
    for text in reversed(recent_msgs):
        sources = parse_image_sources(text)
        if sources:
            break
    if not sources:
        img_value = (data or {}).get("image_source_preferences")
        if isinstance(img_value, list):
            allowed = {"ai", "web_search", "user_upload"}
            sources = [str(s).strip() for s in img_value if str(s).strip() in allowed]
    result["image_source_preferences"] = sources or None
    # Дополняем пустые поля автоданными (парсинг VK / база знаний).
    if parsed_data:
        if not result.get("audience"):
            result["audience"] = (parsed_data.get("target_audience") or "").strip() or None
        if not result.get("community_name"):
            result["community_name"] = (parsed_data.get("community_name") or "").strip() or None
        if not result.get("specifics"):
            usp = (parsed_data.get("usp") or "").strip()
            if usp:
                result["specifics"] = usp
    return result


def ensure_community_context(db: Session, account_id: int, parsed: Dict[str, Any]) -> None:
    """Создаёт CommunityContext (AI-паспорт), если его нет, из собранных
    в онбординге данных (ШАГ 5.2). Существующий паспорт не перетирает."""
    from models import CommunityContext

    existing = (
        db.query(CommunityContext)
        .filter(CommunityContext.community_id == account_id)
        .first()
    )
    if existing is not None:
        return
    audience = (parsed.get("target_audience") or "").strip()
    description = (parsed.get("community_description") or "").strip()
    usp = (parsed.get("usp") or "").strip()
    tone = (parsed.get("tone_of_voice") or "").strip()
    insights = (parsed.get("insights_summary") or "").strip()
    if not any((audience, description, usp, tone, insights)):
        return  # создавать пустой паспорт смысла нет
    row = CommunityContext(
        community_id=account_id,
        target_audience=audience or description or None,
        tone_of_voice=tone[:255] or None,
        usp=usp or None,
        insights_summary=insights or None,
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    logger.info(f"[onboarding] Создан CommunityContext (account_id={account_id}) из данных онбординга")


def save_onboarding_context(
    db: Session,
    user_community_id: int,
    extracted: Dict[str, Any],
    parsed_data: Optional[Dict[str, Any]] = None,
) -> Any:
    """Сохраняет извлечённые данные в CommunityGenerationContext.

    Заполняет goal / community_specifics / target_audience / current_state /
    image_source_preferences и ставит is_configured=True. Существующие
    непустые значения не перетираются на None. Если переданы auto-данные
    парсинга (parsed_data) и для сообщества ещё нет AI-паспорта — создаёт
    CommunityContext из них.

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

    prefs = extracted.get("image_source_preferences")
    if isinstance(prefs, str):
        try:
            prefs = json.loads(prefs)
        except ValueError:
            prefs = [s.strip() for s in prefs.split(",") if s.strip()]
    if isinstance(prefs, (list, tuple)):
        allowed = {"ai", "web_search", "user_upload"}
        clean = [str(s).strip() for s in prefs if str(s).strip() in allowed]
        if clean and not ctx.image_source_preferences:
            ctx.image_source_preferences = json.dumps(clean, ensure_ascii=False)

    ctx.is_configured = True
    db.commit()
    db.refresh(ctx)

    # ШАГ 5.2: если CommunityContext отсутствовал — создать из распаршенных данных.
    if parsed_data is not None:
        try:
            from models import UserCommunity

            uc = db.get(UserCommunity, user_community_id)
            if uc is not None:
                account_id = ensure_platform_account(db, uc)
                if account_id is not None:
                    ensure_community_context(db, account_id, parsed_data)
        except Exception as e:  # noqa: BLE001 — паспорт не должен ронять сохранение контекста
            logger.error(f"[onboarding] Не удалось создать CommunityContext: {type(e).__name__}: {e}")

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


# --- Оркестрация умного диалога (используется эндпоинтом /api/onboarding/chat) ---


def _question_payload(step: Dict[str, Any]) -> Dict[str, Any]:
    """Приводит шаг-вопрос к JSON-ответу API."""
    return {
        "status": "question",
        "reply": step["question"],
        "buttons": step["buttons"],
        "field": step["field"],
        "progress": step.get("progress"),
    }


def finalize_smart_onboarding(
    db: Session,
    state: Dict[str, Any],
    use_local: bool = True,
) -> Dict[str, Any]:
    """Сохраняет собранные ответы и возвращает статус configured.

    Сначала — детерминированный черновик ответов на вопросы с кнопками;
    недостающие поля добираются LLM-извлечением из всего диалога (с учётом
    автоматически собранных данных).
    """
    parsed = state.get("parsed_data") or {}
    draft = dict(state.get("answers_draft") or {})
    extracted: Dict[str, Any] = {
        "community_name": parsed.get("community_name"),
        "goal": draft.get("goal"),
        "specifics": draft.get("specifics"),
        "audience": parsed.get("target_audience"),
        "current_state": draft.get("current_state"),
        "image_source_preferences": draft.get("image_source_preferences"),
    }

    needs_llm = not all(
        extracted.get(k) for k in ("goal", "specifics", "audience", "current_state")
    ) or not extracted.get("image_source_preferences")
    if needs_llm:
        llm_extracted = extract_onboarding_data(
            state.get("messages", []), use_local=use_local, parsed_data=parsed
        ) or {}
        for key in ("goal", "specifics", "audience", "current_state"):
            if not extracted.get(key) and llm_extracted.get(key):
                extracted[key] = llm_extracted[key]
        if not extracted.get("image_source_preferences") and llm_extracted.get("image_source_preferences"):
            extracted["image_source_preferences"] = llm_extracted["image_source_preferences"]
        if not extracted.get("community_name") and llm_extracted.get("community_name"):
            extracted["community_name"] = llm_extracted["community_name"]

    # Если про источник картинок договорились текстом без ключевых слов —
    # сохраняем формулировку в специфику, чтобы она попала в промпты генерации.
    prefs_text = draft.get("image_preferences_text")
    if prefs_text and not extracted.get("image_source_preferences"):
        extracted["image_source_preferences"] = ["ai", "web_search", "user_upload"]
        if extracted.get("specifics"):
            extracted["specifics"] += f". Изображения: {prefs_text}"
        else:
            extracted["specifics"] = f"Изображения: {prefs_text}"

    ctx = save_onboarding_context(
        db, state["community_id"], extracted, parsed_data=parsed
    )
    state["done"] = True
    return {
        "status": "configured",
        "reply": (
            "Отлично, спасибо! Настроил генерацию под ваше сообщество 🎉 "
            "Переходим к контент-менеджеру…"
        ),
        "context": {
            "community_name": extracted.get("community_name"),
            "goal": ctx.goal,
            "community_specifics": ctx.community_specifics,
            "target_audience": ctx.target_audience,
            "current_state_and_what_works": ctx.current_state_and_what_works,
            "image_source_preferences": ctx.image_source_preferences,
            "is_configured": ctx.is_configured,
        },
        "redirect_url": "/content-manager?onboarded=1",
    }


def run_onboarding_turn(
    db: Session,
    state: Dict[str, Any],
    message: str,
    use_local: bool = True,
) -> Dict[str, Any]:
    """Один шаг диалога консьержа с поддержкой нового сценария.

    Поведение:
    - smart_mode (после выбора сообщества и парсинга): детерминированные
      вопросы ТОЛЬКО по недостающим полям + кнопки; завершение — сохранение
      CommunityGenerationContext и редирект (?onboarded=1);
    - обычный режим: прежний LLM-диалог (fallback), при достаточном числе
      реплик — извлечение JSON и сохранение.

    Синхронная функция! Из async-роута вызывать только через to_thread.

    Returns:
        {"status": "question"|"configured", "reply": str, ...}
    """
    messages = state.setdefault("messages", [])
    community_pk = state.get("community_id")

    # --- Умный режим: вопросы по недостающим полям с кнопками ---
    if state.get("smart_mode"):
        pending_task = state.get("task_id")
        if pending_task:
            task = parse_task_store.get(pending_task)
            if task is None or task.get("status") == "parsing":
                return {
                    "status": "parsing",
                    "reply": PARSING_MESSAGE,
                    "parsing_task_id": pending_task,
                }
            state["task_id"] = None
            apply_task_result_to_state(state, task)

        # Пользователь мог нажать другую кнопку и отправить отредактированный
        # ответ, не дожидаясь нового вопроса: если реплика матчится с кнопками
        # другого ещё не отвеченного поля — переключаемся на это поле.
        if message:
            reassign_answer_to_matching_field(state, message)

        if message:
            record_answer(state, message)
            messages.append({"role": "user", "content": message})

        missing = missing_onboarding_fields(state.get("parsed_data"), state)
        if not missing:
            return finalize_smart_onboarding(db, state, use_local=use_local)

        current_field = state.get("current_question_field")
        if (
            not message
            and current_field
            and current_field in missing
            and state.get("last_question_payload")
        ):
            # Повторный запрос текущего вопроса (например, после отправки
            # ответа, который не был засчитан): возвращаем тот же вопрос с
            # теми же кнопками — без «съедания» шага прогресса.
            return dict(state["last_question_payload"])

        asked = state.get("asked_fields") or {}
        field = missing[0]
        q_no = len(asked) + 1
        total = q_no + len(missing) - 1
        step = generate_question_with_buttons(
            field,
            state.get("parsed_data"),
            q_no,
            total,
            community_name=(state.get("parsed_data") or {}).get("community_name"),
        )
        state["current_question_field"] = step["field"]
        meta = {"field": step["field"], "buttons": step["buttons"]}
        messages.append({"role": "assistant", "content": step["question"], "meta": meta})
        payload = _question_payload(step)
        state["last_question_payload"] = payload
        return payload

    # --- Обычный режим (fallback-диалог) ---
    if not messages:
        greeting = (
            ONBOARDING_GREETING_WITH_SELECTION if community_pk else ONBOARDING_GREETING
        )
        messages.append({"role": "assistant", "content": greeting})
        return {
            "status": "question",
            "reply": greeting,
            "buttons": list(NEW_COMMUNITY_TYPE_BUTTONS),
            "field": None,
        }

    if message:
        messages.append({"role": "user", "content": message})

    user_turns = sum(1 for m in messages if m["role"] == "user")
    reply = concierge_reply(messages, use_local=use_local, parsed_data=state.get("parsed_data"))

    if "DONE" in reply.upper() or user_turns >= MIN_USER_TURNS_FOR_EXTRACTION:
        extracted = extract_onboarding_data(
            messages, use_local=use_local, parsed_data=state.get("parsed_data")
        )
        if extracted and community_pk is not None:
            ctx = save_onboarding_context(
                db, community_pk, extracted, parsed_data=state.get("parsed_data")
            )
            state["done"] = True
            return {
                "status": "configured",
                "reply": (
                    "Отлично, спасибо! Настроил генерацию под ваше сообщество 🎉 "
                    "Переходим к контент-менеджеру…"
                ),
                "context": {
                    "community_name": extracted.get("community_name"),
                    "goal": ctx.goal,
                    "community_specifics": ctx.community_specifics,
                    "target_audience": ctx.target_audience,
                    "current_state_and_what_works": ctx.current_state_and_what_works,
                    "image_source_preferences": ctx.image_source_preferences,
                    "is_configured": ctx.is_configured,
                },
                "redirect_url": "/content-manager?onboarded=1",
            }
        if extracted and community_pk is None:
            # Сообщество ещё не добавлено — данные извлечены, но сохранять
            # некуда: просим добавить сообщество в панели.
            state["done"] = True
            return {
                "status": "question",
                "reply": (
                    "Спасибо! Я всё записал. Добавьте, пожалуйста, сообщество "
                    "в панели управления, и я применю настройки."
                ),
            }

    messages.append({"role": "assistant", "content": reply})
    return {"status": "question", "reply": reply}
