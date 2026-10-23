import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from models import (
    CommunityGenerationContext,
    ContentPlanPeriod,
    ChatMessage,
    HistoricalPost,
    MarketingStrategy,
    PlatformAccount,
    Post,
    UserCommunity,
)
from generators.text_generator import generate_text

logger = logging.getLogger(__name__)

# Строгий структурированный вопрос (все 5 обязательных пунктов), который
# ИИ обязан задать, пока сообщество не настроено
# (CommunityGenerationContext.is_configured == False). Используется в
# generate_content_plan_from_chat как гейт перед генерацией плана; в чате
# вопросы выдаются небольшими диалоговыми блоками CONTEXT_SETUP_BLOCK_*.
CONTEXT_SETUP_QUESTION = (
    "Для настройки генерации ответьте: "
    "1) Цель, 2) Специфика, 3) ЦА, 4) Что уже работает, "
    "5) Предпочтения по картинкам (ИИ/Поиск/Загрузка)"
)

# Количество исторических постов, попадающих в блок <ANALYTICS>.
TOP_POSTS_FOR_ANALYTICS: int = 3

# Максимальная длина фрагмента текста поста в аналитическом блоке.
MAX_ANALYTICS_SNIPPET_CHARS: int = 90


def _split_context_questions(parts: List[str]) -> str:
    """Собирает вопросы анкеты небольшими диалоговыми блоками (по 2 вопроса).

    Вместо одного огромного списка из 5 вопросов пользователь получает
    короткое дружелюбное сообщение — так первый диалог выглядит как
    настоящий чат, а не как анкета.
    """
    header = (
        "Здравствуйте! Я ваш персональный контент-менеджер. "
        "Чтобы план получился точным, отвечьте, пожалуйста, на пару вопросов:"
    )
    lines = [header, ""]
    for i, part in enumerate(parts, start=1):
        lines.append(f"{i}) {part}")
    return "\n".join(lines)


# Блок A (вопросы 1-2), блок B (вопросы 3-4), блок C (вопрос 5) —
# суммарно это те же 5 обязательных пунктов CONTEXT_SETUP_QUESTIONS.
CONTEXT_SETUP_BLOCK_A = _split_context_questions(
    ["Какая цель сообщества? (продажи, вовлечение, узнаваемость)",
     "В чём специфика вашего сообщества? (тематика, форматы)"]
)
CONTEXT_SETUP_BLOCK_B = (
    "Отлично! Теперь ещё два вопроса:\n"
    "3) Кто ваша целевая аудитория? (возраст, интересы)\n"
    "4) Что у вас уже есть и что лучше всего работает?"
)
CONTEXT_SETUP_BLOCK_C = (
    "И последний вопрос:\n"
    "5) Предпочтения по картинкам в постах — ИИ / Поиск / Загрузка? "
    "(можно выбрать несколько)"
)

# === УМНЫЙ СТАРТ ДИАЛОГА (smart start) ===
# Сообщение для случая, когда анкета НЕ заполнена: вместо шаблонного списка
# из 3-5 вопросов пользователь получает одно компактное сообщение с просьбой
# заполнить форму настроек — на фронтенде она открывается автоматически
# (openContextModal()) для сообществ с is_configured == False.
# ВАЖНО (UX): в тексте явно объясняем пользователю, ЧЕМ качественнее
# заполнены поля, тем точнее результат — это повышает мотивацию подробно
# отвечать на онбординге и в форме настроек контент-менеджера.
CONTEXT_SETUP_FORM_MESSAGE = (
    "Здравствуйте! Я ваш персональный контент-менеджер. "
    "Чтобы я не задавал вам шаблонные вопросы, заполните, пожалуйста, "
    "форму настроек сообщества, которая открылась перед вами: цель, "
    "специфика, ЦА, что уже работает и предпочтения по картинкам.\n\n"
    "💡 Чем подробнее и качественнее вы заполните эти поля, тем точнее "
    "будет контент-план и тем лучше посты «попадут» в вашу аудиторию — "
    "эти данные напрямую подставляются в промпт при генерации.\n\n"
    "Не переживайте: настройки можно изменить в любой момент — кнопка "
    "«⚙️ Редактировать настройки» над чатом. Сохраните форму — и мы сразу "
    "перейдём к генерации плана."
)


def build_smart_start_message(ctx: CommunityGenerationContext) -> str:
    """Формирует «умное» приветствие при заполненной анкете сообщества.

    Резюмирует данные из CommunityGenerationContext (цель, ЦА) и предлагает
    скорректировать вводные либо сразу приступить к генерации — базовые
    вопросы при этом НЕ задаются.

    Args:
        ctx: настроенный контекст генерации (is_configured == True)

    Returns:
        str: текст приветственного сообщения от имени ИИ
    """
    goal = (ctx.goal or "").strip() or "не указана"
    audience = (ctx.target_audience or "").strip() or "не указана"
    return (
        f"Я проанализировал ваше сообщество. Вижу, что ваша цель: {goal}, "
        f"ЦА: {audience}. На основе этого я готов предложить план.\n\n"
        "💡 Чем подробнее заполнены поля настроек (цель, специфика, ЦА, что "
        "уже работает, картинки) — тем качественнее получится план. Если чего-то "
        "не хватает, дополните их кнопкой «⚙️ Редактировать настройки» над чатом "
        "(изменить можно в любой момент).\n\n"
        "Хотите скорректировать вводные или сразу приступим к генерации?"
    )


# Признаки шаблонных вопросов анкеты в ответе LLM (используется как guard:
# если модель всё-таки задала базовые вопросы, такой ответ не принимается).
_TEMPLATE_QUESTION_MARKERS = (
    "какая цель",
    "цель сообщества",
    "специфик",
    "целевая аудитория",
    "кто ваша",
    "что у вас уже есть",
    "что лучше всего работает",
    "что уже работает",
    "предпочтения по картинкам",
    "картинки в постах",
)


def _looks_like_template_questions(text: str) -> bool:
    """True, если текст похож на шаблонные вопросы анкеты (повтор того, что ИИ уже знает)."""
    lowered = (text or "").lower()
    hits = sum(1 for marker in _TEMPLATE_QUESTION_MARKERS if marker in lowered)
    return hits >= 2


def _configured_context_note(ctx: Optional[CommunityGenerationContext]) -> str:
    """Инструкция для system_prompt: анкета заполнена, известные данные не спрашивать."""
    if ctx is None:
        return ""
    return (
        "\n\nАНКЕТА СООБЩЕСТВА УЖЕ ЗАПОЛНЕНА (см. CONTEXT/ANALYTICS): "
        f"цель — «{(ctx.goal or '').strip() or 'не указана'}», "
        f"ЦА — «{(ctx.target_audience or '').strip() or 'не указана'}». "
        "НЕ задавай пользователю вопросы о цели, специфике, ЦА, том, что уже "
        "работает, и предпочтениях по картинкам — ты уже знаешь эти данные. "
        "Сразу предлагай план или уточняй только новые детали."
    )


def get_or_create_generation_context(
    db: Session,
    user_community_id: int
) -> CommunityGenerationContext:
    """Возвращает (или создаёт с is_configured=False) контекст генерации сообщества.

    Args:
        db: Сессия базы данных
        user_community_id: ID записи UserCommunity

    Returns:
        CommunityGenerationContext: запись настроек генерации
    """
    ctx = db.query(CommunityGenerationContext).filter(
        CommunityGenerationContext.user_community_id == user_community_id
    ).first()

    if ctx is None:
        ctx = CommunityGenerationContext(
            user_community_id=user_community_id,
            is_configured=False,
        )
        db.add(ctx)
        db.commit()
        db.refresh(ctx)

    return ctx


def _period_user_community(db: Session, period: ContentPlanPeriod) -> Optional[UserCommunity]:
    """Возвращает сообщество, к которому привязан период (для умного старта чата).

    FIX: раньше всегда возвращалось ПЕРВОЕ сообщество пользователя — при
    нескольких сообществах приветствие строилось по контексту НЕ того
    сообщества, для которого создавался план (контекст выбранного
    сообщества не подхватывался, чат выглядел «не настроенным», а в
    «Мои планы» результат был виден только после перезагрузки из-за
    рассинхронизации состояния). Теперь приоритет:
    1. community_info.period_community_id — id сообщества, явно выбранного
       пользователем в момент создания периода;
    2. единственное сообщество пользователя;
    3. первое сообщество (обратная совместимость).
    """
    import json as _json

    raw = (period.community_info or "").strip() if period is not None else ""
    if raw:
        # 1) Явная привязка периода к сообществу (JSON {"period_community_id": N})
        try:
            data = _json.loads(raw)
            if isinstance(data, dict):
                cid = data.get("period_community_id")
                if cid:
                    comm = db.query(UserCommunity).filter(
                        UserCommunity.id == int(cid),
                        UserCommunity.user_id == period.user_id,
                    ).first()
                    if comm is not None:
                        return comm
        except (ValueError, TypeError):
            pass
        # 2) Совпадение по group_id / названию внутри текстового описания
        comms = db.query(UserCommunity).filter(
            UserCommunity.user_id == period.user_id
        ).order_by(UserCommunity.id.asc()).all()
        for c in comms:
            name = (c.group_name or "").strip()
            if name and name in raw:
                return c
            if str(c.group_id) in raw:
                return c
        if len(comms) == 1:
            return comms[0]
        if comms:
            return comms[0]
    return db.query(UserCommunity).filter(
        UserCommunity.user_id == period.user_id
    ).order_by(UserCommunity.id.asc()).first()


def get_configured_generation_context(
    db: Session, user_community_id: int
) -> Optional[CommunityGenerationContext]:
    """Возвращает CommunityGenerationContext, только если он реально настроен.

    Проверяет наличие записи с is_configured == True и непустыми ключевыми
    полями (хотя бы цель или ЦА). Иначе — None.
    """
    ctx = db.query(CommunityGenerationContext).filter(
        CommunityGenerationContext.user_community_id == user_community_id
    ).first()
    if ctx is None or not ctx.is_configured:
        return None
    if not ((ctx.goal or "").strip() or (ctx.target_audience or "").strip()):
        return None
    return ctx


def _top_historical_posts(db: Session, group_id: int) -> List[HistoricalPost]:
    """Топ-N исторических постов VK-сообщества по вовлечённости (ER).

    HistoricalPost.community_id ссылается на PlatformAccount.id, а связь
    с конкретным сообществом пользователя идёт через account_id (VK group_id).
    Сначала берём посты с флагом is_top_performer, при их отсутствии —
    просто топ по engagement_rate.
    """
    account_ids = [
        aid
        for (aid,) in db.query(PlatformAccount.id).filter(
            PlatformAccount.account_id == group_id
        ).all()
    ]
    if not account_ids:
        return []

    query = db.query(HistoricalPost).filter(HistoricalPost.community_id.in_(account_ids))
    top_flagged = (
        query.filter(HistoricalPost.is_top_performer.is_(True))
        .order_by(HistoricalPost.engagement_rate.desc())
        .limit(TOP_POSTS_FOR_ANALYTICS)
        .all()
    )
    if top_flagged:
        return top_flagged
    return query.order_by(HistoricalPost.engagement_rate.desc()).limit(TOP_POSTS_FOR_ANALYTICS).all()


def _post_topic_snippet(post: HistoricalPost) -> str:
    """Короткая «тема» поста — первые строки текста для блока ANALYTICS."""
    text = (post.text or "").strip().replace("\n", " ")
    if not text:
        return f"пост #{post.vk_post_id}"
    if len(text) > MAX_ANALYTICS_SNIPPET_CHARS:
        text = text[:MAX_ANALYTICS_SNIPPET_CHARS].rstrip() + "…"
    return text


def build_analytics_block(db: Session, period: ContentPlanPeriod) -> str:
    """Формирует блок <ANALYTICS>...</ANALYTICS> из анкеты и исторической статистики.

    Содержит: лучшие посты (темы + ER) из HistoricalPost, ЦА и цель из
    CommunityGenerationContext, а также рубрики из MarketingStrategy, если
    стратегия сообщества уже генерировалась.

    Возвращает пустую строку, если и настроенной анкеты, и исторических
    данных нет — тогда контент-менеджер должен задавать вопросы.
    """
    community = _period_user_community(db, period)
    if community is None:
        return ""

    ctx = get_configured_generation_context(db, community.id)
    top_posts = _top_historical_posts(db, community.group_id)

    if ctx is None and not top_posts:
        return ""

    lines = ["<ANALYTICS>"]

    if top_posts:
        posts_desc = "; ".join(
            f"«{_post_topic_snippet(p)}» (ER={p.engagement_rate:.2f}%)" for p in top_posts
        )
        lines.append(f"Лучшие посты: {posts_desc}")
    else:
        lines.append("Лучшие посты: историческая статистика отсутствует")

    if ctx is not None:
        lines.append(f"ЦА: {ctx.target_audience or 'не указана'}")
        lines.append(f"Цель: {ctx.goal or 'не указана'}")
        if (ctx.community_specifics or "").strip():
            lines.append(f"Специфика: {ctx.community_specifics.strip()}")
        if (ctx.current_state_and_what_works or "").strip():
            lines.append(f"Что уже работает: {ctx.current_state_and_what_works.strip()}")

        strategy = (
            db.query(MarketingStrategy)
            .join(PlatformAccount, MarketingStrategy.community_id == PlatformAccount.id)
            .filter(PlatformAccount.account_id == community.group_id)
            .first()
        )
        if strategy is not None:
            rubric_names = [
                str(r.get("name")) for r in (strategy.rubrics or [])
                if isinstance(r, dict) and r.get("name")
            ]
            if rubric_names:
                lines.append(f"Рубрики стратегии: {', '.join(rubric_names[:5])}")
            if (strategy.posting_schedule or "").strip():
                lines.append(f"График публикаций: {strategy.posting_schedule.strip()}")

    lines.append("</ANALYTICS>")
    return "\n".join(lines)


def build_context_block(db: Session, period: ContentPlanPeriod) -> str:
    """Формирует блок <CONTEXT>...</CONTEXT> из CommunityGenerationContext.

    Если для сообщества пользователя есть настроенный контекст
    (is_configured == True), его данные внедряются в системный промпт LLM.
    Иначе возвращается пустая строка.
    """
    community = _period_user_community(db, period)
    if community is None:
        return ""

    ctx = db.query(CommunityGenerationContext).filter(
        CommunityGenerationContext.user_community_id == community.id
    ).first()

    if ctx is None or not ctx.is_configured:
        return ""

    # image_source_preferences — JSON-строка; парсим безопасно для отображения
    try:
        prefs = json.loads(ctx.image_source_preferences or "[]")
        prefs_str = ", ".join(str(p) for p in prefs) if isinstance(prefs, list) else str(prefs)
    except (ValueError, TypeError):
        prefs_str = ctx.image_source_preferences or ""

    parts = [
        "<CONTEXT>",
        f"Сообщество: {community.group_name or community.group_id}",
        f"Цель: {ctx.goal or 'не указана'}",
        f"Специфика сообщества: {ctx.community_specifics or 'не указана'}",
        f"Целевая аудитория: {ctx.target_audience or 'не указана'}",
        f"Что уже есть и что работает: {ctx.current_state_and_what_works or 'не указано'}",
        f"Предпочтения по источникам картинок: {prefs_str or 'не указаны'}",
    ]

    # Дополнительно к анкете подмешиваем аналитику лучших постов, чтобы план
    # опирался на реальные ER-паттерны сообщества.
    analytics = build_analytics_block(db, period)
    if analytics:
        parts.append(analytics)

    parts.append("</CONTEXT>")
    return "\n".join(parts)


def _default_use_local(db: Session) -> bool:
    """Читает настройку ai_provider из SystemSetting.

    True — локальный Ollama; False — облачный провайдер (cloud/gigachat).
    При недоступности БД — безопасный fallback: локальная модель.
    """
    try:
        from models import SystemSetting

        row = db.query(SystemSetting).filter(SystemSetting.key == "ai_provider").first()
        provider = (row.value or "").strip().lower() if row else "ollama"
        return provider not in ("cloud", "gigachat")
    except Exception as e:  # noqa: BLE001
        # FIX: таблица system_settings может отсутствовать в старой БД
        # («no such table») — сессию нужно откачать, иначе все последующие
        # запросы в этой сессии будут падать, а генерация деградирует до Ollama.
        logger.warning(
            f"Не удалось прочитать ai_provider ({type(e).__name__}: {e}) — "
            "fallback на локальную модель. Проверьте миграции при старте приложения."
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return True


# Системный промпт для контент-менеджера
CONTENT_MANAGER_SYSTEM_PROMPT = """Ты — персональный контент-менеджер и маркетолог для ведения социальных сетей. 
Твоя задача — помочь пользователю создать эффективный контент-план для его сообщества.

Твои обязанности:
1. Узнать информацию о сообществе (тематика, целевая аудитория, цели)
2. Задать уточняющие вопросы о предпочтениях в контенте
3. На основе ответов сгенерировать персонализированный контент-план
4. Предложить посты разных типов: полезные, вовлекающие, развлекательные, продающие

Тон общения: дружеский, профессиональный, поддерживающий. Задавай вопросы по одному, не перегружай пользователя.

Форматирование ответов (ВАЖНО):
- Отвечай обычным текстом: для переносов строк используй \\n, для списков — «- » с новой строки.
- КАТЕГОРИЧЕСКИ запрещён любой HTML: не используй <br>, <b>, <i>, <ul>, <li> и другие теги.

Формат контент-плана:
- Период: неделя, 2 недели, месяц
- Количество постов: зависит от периода (7, 14, 30)
- Типы постов чередуются: польза (30-40%), вовлечение (25-30%), развлечение (15-20%), продажа (15-20%)

Когда пользователь подтверждает план, создай посты с темами и кратким описанием."""


def get_or_create_content_plan_period(
    db: Session,
    user_id: int,
    period_type: str = "week",
    community_info: Optional[str] = None
) -> ContentPlanPeriod:
    """
    Создаёт новый период контент-плана или возвращает активный.
    
    Args:
        db: Сессия базы данных
        user_id: ID пользователя
        period_type: Тип периода (week, two_weeks, month)
        community_info: Информация о сообществе
        
    Returns:
        ContentPlanPeriod: Период контент-плана
    """
    # Проверяем есть ли активный период
    active_period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.user_id == user_id,
        ContentPlanPeriod.status == "active"
    ).first()
    
    if active_period:
        # Проверяем не закончился ли период
        now = datetime.now()
        if active_period.end_date < now:
            # Помечаем как завершённый
            active_period.status = "completed"
            active_period.updated_at = now
            db.commit()
            logger.info(f"Период {active_period.id} завершён, создаём новый")
        else:
            logger.info(f"Возвращаем активный период {active_period.id}")
            return active_period
    
    # Создаём новый период
    now = datetime.now()
    if period_type == "week":
        days = 7
    elif period_type == "two_weeks":
        days = 14
    elif period_type == "month":
        days = 30
    else:
        days = 7
    
    new_period = ContentPlanPeriod(
        user_id=user_id,
        period_type=period_type,
        start_date=now,
        end_date=now + timedelta(days=days),
        status="draft",
        community_info=community_info or ""
    )
    
    db.add(new_period)
    db.commit()
    db.refresh(new_period)
    
    logger.info(f"Создан новый период контент-плана {new_period.id} на {days} дней")
    return new_period


def add_chat_message(
    db: Session,
    period_id: int,
    role: str,
    content: str
) -> ChatMessage:
    """
    Добавляет сообщение в чат контент-плана.
    
    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана
        role: Роль (user, assistant, system)
        content: Текст сообщения
        
    Returns:
        ChatMessage: Созданное сообщение
    """
    message = ChatMessage(
        content_plan_period_id=period_id,
        role=role,
        content=content
    )
    
    db.add(message)
    db.commit()
    db.refresh(message)
    
    return message


def get_chat_history(db: Session, period_id: int) -> List[ChatMessage]:
    """
    Получает историю чата для периода контент-плана.
    
    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана
        
    Returns:
        List[ChatMessage]: Список сообщений
    """
    messages = db.query(ChatMessage).filter(
        ChatMessage.content_plan_period_id == period_id
    ).order_by(ChatMessage.id.asc()).all()
    
    return messages


def get_user_community_info(db: Session, user_id: int) -> str:
    """
    Получает информацию о сообществах пользователя.
    
    Args:
        db: Сессия базы данных
        user_id: ID пользователя
        
    Returns:
        str: Информация о сообществах
    """
    communities = db.query(UserCommunity).filter(
        UserCommunity.user_id == user_id
    ).all()
    
    if not communities:
        return "Информация о сообществах отсутствует"
    
    info_parts = []
    for comm in communities:
        info_parts.append(f"- {comm.group_name or f'Сообщество {comm.group_id}'} (ID: {comm.group_id})")
    
    return "\n".join(info_parts)


_HTML_TAG_RE = re.compile(r"<br\s*/?>|</?(?:p|div|span|ul|ol|li|b|strong|i|em|u|s|a|h[1-6]|blockquote|pre|code)\b[^>]*>", re.IGNORECASE)


# === АДАПТИВНЫЕ КНОПКИ БЫСТРЫХ ВАРИАНТОВ ОТВЕТА ===
# Набор кнопок подбирается ПОД ТЕКСТ каждого сообщения ИИ на фронтенде
# (content_manager.html detectChatButtons -> /api/content-manager/chat-options).
# Каждое сообщение accompanied кнопками на протяжении всего диалога: если
# узкоспециализированный набор не подошёл, показывается универсальный fallback.
CHAT_OPTION_BUTTONS: Dict[str, List[Dict[str, str]]] = {
    # Стартовое приветствие: скорректировать вводные или генерировать план
    "adjust": [
        {"label": "✏️ Скорректировать вводные", "value": "Хочу скорректировать вводные: "},
        {"label": "✨ Приступить к генерации плана", "value": "Всё верно, приступим к генерации плана"},
        {"label": "📋 Показать, что ты знаешь о сообществе", "value": "Покажи, какие данные о моём сообществе ты уже учёл"},
    ],
    # Вопрос про текущий контент / что уже публикуется
    "current_state": [
        {"label": "📸 Фотографии готовых работ", "value": "Фотографии готовых работ"},
        {"label": "📝 Отзывы клиентов", "value": "Отзывы клиентов"},
        {"label": "💡 Советы и полезный контент", "value": "Советы и полезный контент"},
        {"label": "❓ Пока ничего не публикую", "value": "Пока ничего не публикую"},
    ],
    # Вопрос про картинки/изображения
    "image_preferences": [
        {"label": "🤖 Генерировать AI-картинки", "value": "Генерировать AI-картинки"},
        {"label": "🔍 Искать картинки в интернете", "value": "Искать картинки в интернете"},
        {"label": "📤 Буду загружать свои фото", "value": "Буду загружать свои фото"},
        {"label": "🎨 Комбинация AI и своих фото", "value": "Комбинация AI-картинок и своих фото"},
    ],
    # Сообщение об открытой форме настроек (анкета не заполнена)
    "setup_form": [
        {"label": "⚙️ Открыть форму настроек", "value": "__open_settings__"},
        {"label": "❓ Пропустить и начать с вопросов", "value": "Давай без формы — задай мне вопросы прямо в чате"},
    ],
    # План сгенерирован (посты созданы)
    "plan_ready": [
        {"label": "✅ Оставить как есть", "value": "План отличный, оставляем как есть"},
        {"label": "🔄 Пересобрать план", "value": "Пересобери план с учётом наших договорённостей"},
        {"label": "✏️ Внести правки", "value": "Хочу внести правки в план: "},
    ],
    # Универсальный fallback — кнопки всегда видны на протяжении диалога
    "default": [
        {"label": "✨ Сгенерировать контент-план", "value": "Приступим к генерации контент-плана"},
        {"label": "✏️ Скорректировать вводные", "value": "Хочу скорректировать вводные: "},
        {"label": "⚙️ Изменить настройки сообщества", "value": "__open_settings__"},
        {"label": "❓ Что ты умеешь?", "value": "Расскажи, чем ты можешь помочь"},
    ],
}


def detect_chat_buttons(text: str) -> List[Dict[str, str]]:
    """Подбирает кнопки быстрых вариантов под текст сообщения ИИ.

    Содержание и количество кнопок адаптируются под смысл реплики:
    приветствие/вопрос о корректировке -> «правку или генерацию»,
    вопрос о текущем контенте -> варианты контента, вопрос о картинках ->
    источники изображений, упоминание формы настроек -> «открыть форму»,
    готовый план -> «принять/пересобрать». Если ни один паттерн не распознан —
    возвращается универсальный набор (кнопки не исчезают никогда).

    Args:
        text: Текст сообщения ассистента.

    Returns:
        Список кнопок [{"label": ..., "value": ...}] (всегда непустой).
    """
    t = (text or "").lower()
    # Приоритет 1: готовые посты/план (упоминание сгенерированного плана)
    if ("план" in t and any(w in t for w in ("готов", "создан", "сгенерирован", "вот ваш", "получил"))):
        return CHAT_OPTION_BUTTONS["plan_ready"]
    # Приоритет 2: просьба заполнить форму настроек
    if "форм" in t and ("настрой" in t or "заполн" in t):
        return CHAT_OPTION_BUTTONS["setup_form"]
    # Приоритет 3: стартовое приветствие / выбор «правка или генерация»
    if (("скорректиров" in t or "вводные" in t or "приступ" in t) and
            ("генераци" in t or "план" in t)) or "проанализировал" in t:
        return CHAT_OPTION_BUTTONS["adjust"]
    # Приоритет 4: вопрос про текущий контент
    if ("что вы публикуете" in t or "что уже" in t or "текущ" in t and "контент" in t or
            "работает лучше" in t or "что из контента" in t):
        return CHAT_OPTION_BUTTONS["current_state"]
    # Приоритет 5: вопрос про картинки/изображения
    img_mention = any(w in t for w in ("картинк", "изображен", "фотограф", "визуал"))
    img_ask = any(w in t for w in ("предпочит", "какие", "использовать", "нужны ли", "подходит"))
    if img_mention and img_ask:
        return CHAT_OPTION_BUTTONS["image_preferences"]
    # Fallback: универсальные кнопки — остаются видимыми весь диалог
    return CHAT_OPTION_BUTTONS["default"]


def _strip_html_from_response(text: str) -> str:
    """Нормализует ответ LLM в plain text для отображения в чате.

    FIX (баг с <br>): контент-менеджер рендерится во фронтенде как escaped
    plain text (переводы \n заменяются на <br> уже на клиенте). Если модель
    возвращает HTML (<br>, <b>, <ul><li> и т.п.), пользователь видит теги
    текстом. Удаляем типовые HTML-теги, приводя ответ к обычному тексту.

    Args:
        text: Сырой ответ модели.

    Returns:
        Текст без HTML-разметки (без изменений, если тегов нет).
    """
    if not text or "<" not in text:
        return text
    cleaned = _HTML_TAG_RE.sub("\n", text)
    # Схлопываем несколько подряд идущих пустых строк в одну.
    cleaned = re.sub(r"(\n\s*){2,}", "\n\n", cleaned)
    return cleaned.strip()


def generate_ai_response(
    db: Session,
    period_id: int,
    user_message: str,
    user_id: int
) -> str:
    """
    Генерирует ответ ИИ-контент-менеджера на сообщение пользователя.
    
    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана
        user_message: Сообщение пользователя
        user_id: ID пользователя
        
    Returns:
        str: Ответ ИИ
    """
    # Получаем период
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id
    ).first()
    
    if not period:
        return "Ошибка: период контент-плана не найден"
    
    # Получаем историю чата
    chat_history = get_chat_history(db, period_id)
    
    # Формируем контекст для ИИ
    context = f"""Текущий статус:
- Период планирования: {period.period_type}
- Даты: {period.start_date.strftime('%d.%m.%Y')} - {period.end_date.strftime('%d.%m.%Y')}
- Статус: {period.status}
- Информация о сообществе: {period.community_info or 'Не указана'}

История диалога:"""

    for msg in chat_history[-10:]:  # Последние 10 сообщений
        role_ru = "Пользователь" if msg.role == "user" else "Ассистент"
        context += f"\n{role_ru}: {msg.content}"

    context += f"\n\nПользователь: {user_message}\n\nАссистент:"

    # FIX (игнорирование анкеты и аналитики + умный старт): если сообщество
    # настроено (CommunityGenerationContext.is_configured == True) — данные
    # внедряются в системный промпт как блоки <CONTEXT>/<ANALYTICS>, и ИИ НЕ
    # задаёт вопросы о том, что уже знает. Проверка контекста выполняется при
    # каждом ответе, включая первый.
    system_prompt = CONTENT_MANAGER_SYSTEM_PROMPT
    community = _period_user_community(db, period)
    ctx = (
        get_configured_generation_context(db, community.id)
        if community is not None
        else None
    )
    top_posts = _top_historical_posts(db, community.group_id) if community is not None else []
    analytics_block = build_analytics_block(db, period) if (ctx is not None or top_posts) else ""
    if ctx is not None:
        # Контекст ЕСТЬ: добавляем аналитику (если есть) и явный запрет
        # переспрашивать известные данные.
        if analytics_block:
            system_prompt = f"{system_prompt}\n\n{analytics_block}"
        system_prompt += _configured_context_note(ctx)
    elif analytics_block:
        # Только история постов без заполненной анкеты — используем её,
        # но диалог настройки не отменяем.
        system_prompt = (
            f"{system_prompt}\n\n{analytics_block}\n\n"
            "У тебя есть история постов сообщества (см. ANALYTICS), но анкета "
            "ещё не заполнена — уточняй недостающие вводные."
        )
    elif community is not None:
        # Анкеты нет — вместо шаблонных вопросов просим заполнить форму
        # настроек (на фронтенде она открывается автоматически).
        system_prompt = (
            f"{system_prompt}\n\nАнкета сообщества ещё не заполнена. "
            "НЕ рассылай шаблонные вопросы по пунктам анкеты — попроси "
            "пользователя заполнить форму настроек сообщества (она открыта "
            "в интерфейсе) и сохранить её. После сохранения все данные "
            "поступят тебе автоматически."
        )

    # Генерируем ответ через ИИ
    try:
        # FIX: провайдер выбирается по системной настройке ai_provider
        # (ollama | cloud | gigachat), а не жёстко Ollama.
        response = generate_text(
            prompt=context,
            system_prompt=system_prompt,
            use_local=_default_use_local(db),
        )

        # FIX (баг с <br> в чате): модель иногда возвращает HTML-разметку
        # (<br>, <b>, списки <ul><li>) вместо обычного текста. Фронтенд экранирует
        # '<' при выводе, поэтому теги видны пользователю как текст (например,
        # «...темы:<br>- Рецепты<br>- Мастер-классы»). Нормализуем ответ в plain text.
        response = _strip_html_from_response(response)
        
        # Сохраняем сообщение пользователя и ответ ИИ
        add_chat_message(db, period_id, "user", user_message)
        add_chat_message(db, period_id, "assistant", response)
        
        return response
    except Exception as e:
        logger.error(f"Ошибка генерации ответа ИИ: {e}")
        error_msg = "Извините, произошла ошибка при генерации ответа. Попробуйте ещё раз."
        add_chat_message(db, period_id, "user", user_message)
        add_chat_message(db, period_id, "assistant", error_msg)
        return error_msg


def initialize_chat_with_questions(
    db: Session,
    period_id: int
) -> Optional[str]:
    """
    Инициализирует чат ПЕРВОГО СОЗДАНИЯ периодом — ровно одним сообщением
    от ИИ-контент-менеджера.

    FIX (дублирование приветствий): раньше это функция вызывалась КАЖДЫЙ раз
    при нажатии кнопок «Неделя / 2 недели / Месяц», даже когда период уже
    существовал (get_or_create_content_plan_period возвращает активный
    период). Каждое такое нажатие добавляло в чат НОВОЕ приветствие (часто
    перефразированное LLM), из-за чего в чате копилось несколько похожих
    сообщений подряд. Теперь:
      - если в чате периода УЖЕ есть сообщения — функция ничего не делает
        и возвращает None (фронтенд при этом подтягивает существующую
        историю);
      - приветствие пишется в чат строго один раз на период.

    Логика «умного старта»:
    1. Период берётся из БД по period_id, сообщество пользователя — через
       _period_user_community(db, period).
    2. Проверяется CommunityGenerationContext сообщества с is_configured == True.
    3. Если контекст ЕСТЬ — формируется детерминированное приветствие,
       резюмирующее данные анкеты. Базовые вопросы из пунктов анкеты
       НЕ задаются. Если есть историческая аналитика постов, к резюме
       добавляется строка о лучших темах.
    4. Если контекста НЕТ — выдаётся ОДНО компактное сообщение с просьбой
       заполнить форму настроек (на фронтенде модалка формы открывается
       автоматически для незаполненных сообществ).

    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана

    Returns:
        Optional[str]: созданное приветственное сообщение либо None, если
            чат уже инициализирован (сообщения в нём есть)
    """
    # Шаг 0: идемпотентность — если в чате уже есть сообщения, повторное
    # приветствие НЕ создаём (защита от дублей при повторных кликах по
    # кнопкам периода и при возврате существующего активного периода).
    existing = get_chat_history(db, period_id)
    if existing:
        logger.info(
            f"[initialize_chat] Чат периода {period_id} уже инициализирован "
            f"(сообщений: {len(existing)}) — приветствие не дублируем"
        )
        return None

    # Шаг 1: период из БД.
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id
    ).first()

    # Шаг 2: сообщество пользователя.
    community = _period_user_community(db, period) if period is not None else None

    # Шаг 3: настроенный контекст генерации (is_configured == True).
    ctx = (
        get_configured_generation_context(db, community.id)
        if community is not None
        else None
    )

    # === ВЕТКА 1: контекст ЕСТЬ — умный старт без базовых вопросов ===
    if ctx is not None:
        # Только детерминированная резюма анкеты (без обращения к LLM):
        # фиксированный текст гарантирует отсутствие вариативных дублей
        # приветствий и мгновенный отклик без задержек на генерацию.
        welcome_message = build_smart_start_message(ctx)

        # Опционально: дополняем резюму реальной аналитикой постов.
        top_posts = _top_historical_posts(db, community.group_id)
        if top_posts:
            best_topic = _post_topic_snippet(top_posts[0])
            welcome_message += (
                f"\nПо истории постов лучше всего заходят: «{best_topic}»."
            )

        add_chat_message(db, period_id, "assistant", welcome_message)
        return welcome_message

    # === ВЕТКА 2: контекста НЕТ — одно компактное сообщение о форме настроек ===
    # Шаблонные вопросы из пунктов анкеты больше не рассылаются: на фронтенде
    # для незаполненного сообщества автоматически открывается модальная форма
    # настроек (openContextModal()), и просим пользователя заполнить именно её.
    add_chat_message(db, period_id, "assistant", CONTEXT_SETUP_FORM_MESSAGE)
    return CONTEXT_SETUP_FORM_MESSAGE


def generate_content_plan_from_chat(
    db: Session,
    period_id: int,
    user_id: int
) -> List[Dict[str, Any]]:
    """
    Генерирует контент-план на основе истории чата.
    
    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана
        user_id: ID пользователя
        
    Returns:
        List[Dict]: Список созданных постов
    """
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id
    ).first()
    
    if not period:
        raise ValueError("Период контент-плана не найден")

    # FIX (контекст генерации): проверяем CommunityGenerationContext сообщества.
    community = _period_user_community(db, period)
    gen_ctx: Optional[CommunityGenerationContext] = None
    if community is not None:
        gen_ctx = db.query(CommunityGenerationContext).filter(
            CommunityGenerationContext.user_community_id == community.id
        ).first()

    # Если сообщество ещё не настроено (первый диалог) — первый ответ ИИ
    # должен быть строго структурированным вопросом о настройке, а не планом.
    if gen_ctx is not None and not gen_ctx.is_configured:
        add_chat_message(db, period_id, "assistant", CONTEXT_SETUP_QUESTION)
        logger.info(
            f"Период {period_id}: контекст сообщества {community.id} не настроен — "
            "возвращён структурированный вопрос настройки"
        )
        return [{"setup_required": True, "question": CONTEXT_SETUP_QUESTION}]
    
    # Получаем историю чата для контекста
    chat_history = get_chat_history(db, period_id)
    chat_context = "\n".join([f"{msg.role}: {msg.content}" for msg in chat_history])
    
    # Определяем количество постов基于 периода
    if period.period_type == "week":
        num_posts = 7
        post_types = ["benefit"] * 3 + ["engagement"] * 2 + ["entertainment"] * 1 + ["sales"] * 1
    elif period.period_type == "two_weeks":
        num_posts = 14
        post_types = ["benefit"] * 5 + ["engagement"] * 4 + ["entertainment"] * 3 + ["sales"] * 2
    elif period.period_type == "month":
        num_posts = 30
        post_types = ["benefit"] * 10 + ["engagement"] * 8 + ["entertainment"] * 7 + ["sales"] * 5
    else:
        num_posts = 7
        post_types = ["benefit"] * 3 + ["engagement"] * 2 + ["entertainment"] * 1 + ["sales"] * 1
    
    # Генерируем темы для постов через ИИ
    prompt = f"""На основе следующего диалога с пользователем, предложи {num_posts} тем для постов.
    
Диалог:
{chat_context}

Предложи конкретные темы для каждого типа поста:
- Польза (benefit): практические советы, лайфхаки
- Вовлечение (engagement): вопросы, опросы
- Развлечение (entertainment): забавные истории, мемы
- Продажа (sales): мягкие предложения товара/услуги

Верни ответ в формате JSON массива объектов:
[
    {{"type": "benefit", "topic": "Тема поста", "description": "Краткое описание"}},
    ...
]"""
    
    try:
        from generators.text_generator import generate_text
        import json
        
        # FIX (внедрение контекста): данные CommunityGenerationContext
        # (is_configured == True) добавляются в системный промпт LLM
        # как блок <CONTEXT>...</CONTEXT>.
        plan_system_prompt = (
            "Ты эксперт по контент-маркетингу. Верни ТОЛЬКО JSON без дополнительного текста."
        )
        context_block = build_context_block(db, period)
        if context_block:
            plan_system_prompt = f"{plan_system_prompt}\n\n{context_block}"

        ai_response = generate_text(
            prompt=prompt,
            system_prompt=plan_system_prompt,
            use_local=_default_use_local(db),
        )
        
        # Парсим JSON ответ
        # Очищаем ответ от возможных лишних символов
        clean_response = ai_response.strip()
        if clean_response.startswith("```json"):
            clean_response = clean_response[7:]
        if clean_response.endswith("```"):
            clean_response = clean_response[:-3]
        clean_response = clean_response.strip()
        
        post_topics = json.loads(clean_response)
        
    except Exception as e:
        logger.error(f"Ошибка парсинга JSON от ИИ: {e}")
        # Используем дефолтные темы если не удалось получить от ИИ
        post_topics = []
        for i, ptype in enumerate(post_types[:num_posts]):
            post_topics.append({
                "type": ptype,
                "topic": f"Тема {i+1} ({ptype})",
                "description": f"Описание для поста типа {ptype}"
            })
    
    # Гибридная картинка (тарификация): план генерируется синхронно из
    # threadpool-роута FastAPI, поэтому сетевые вызовы поиска здесь допустимы.
    # Если у пользователя нет тарифа или тариф не разрешает картинки —
    # посты остаются без изображений (обратная совместимость сохранена).
    tariff_plan = None
    try:
        from models import User
        from services.tariff_service import get_user_tariff

        plan_user = db.query(User).filter(User.id == period.user_id).first()
        if plan_user is not None:
            tariff_plan = get_user_tariff(db, plan_user)
    except Exception as e:  # noqa: BLE001 — диагностика тарифа не должна ломать план
        logger.warning("Не удалось определить тариф пользователя: %s", e)

    web_search_enabled = False
    max_imgs = 0
    allowed_sources = set()
    if tariff_plan is not None and getattr(tariff_plan, "allows_images", False):
        max_imgs = int(tariff_plan.max_images_per_post or 0)
        from services.tariff_service import parse_allowed_sources

        allowed_sources = set(parse_allowed_sources(tariff_plan))
        if gen_ctx is not None and gen_ctx.image_source_preferences:
            try:
                prefs = {str(s) for s in json.loads(gen_ctx.image_source_preferences)}
                if prefs:
                    allowed_sources &= prefs  # предпочтения сообщества сужают разрешения
            except (ValueError, TypeError):
                pass
        web_search_enabled = bool(max_imgs > 0 and "web_search" in allowed_sources)

    search_query = ""
    if community is not None:
        search_query = (community.group_name or "").strip()
    if not search_query:
        search_query = (period.community_info or "").strip() or "контент для сообщества"

    # Создаём посты в БД
    created_posts = []
    now = datetime.now()

    for i, topic_data in enumerate(post_topics[:num_posts]):
        post = Post(
            content_plan_id=1,  # Default content plan, может быть обновлено позже
            content_plan_period_id=period_id,
            post_type=topic_data.get("type", "benefit"),
            topic=topic_data.get("topic", "Без темы")[:255],
            text_draft=f"# Черновик поста\n\n{topic_data.get('description', '')}\n\n(Требуется доработка)",
            text_final=None,
            image_url=None,
            image_source=None,
            status="draft",
            publish_at=now + timedelta(hours=i*3)  # Распределяем посты по времени
        )

        db.add(post)
        db.flush()  # получаем post.id до commit

        # Гибридное назначение картинок согласно тарифу (user_upload -> ai ->
        # web_search). Для черновиков плана доступны web_search и user_upload;
        # ИИ-генерация остаётся за явным эндпоинтом /api/generate.
        if web_search_enabled:
            try:
                from services.tariff_service import assign_images_to_post

                assign_images_to_post(
                    db,
                    post,
                    tariff_plan,
                    topic=post.topic or search_query,
                    replace_existing=False,
                )
            except Exception as e:  # noqa: BLE001 — без картинки лучше, чем без плана
                logger.warning(
                    "Пост %s: гибридное назначение картинок не удалось: %s",
                    post.id, e,
                )

        created_posts.append({
            "id": post.id,
            "type": post.post_type,
            "topic": post.topic
        })

    # Обновляем статус периода
    period.status = "active"
    period.updated_at = now
    db.commit()
    
    logger.info(f"Создано {len(created_posts)} постов для периода {period_id}")
    
    return created_posts


def update_existing_plan(
    db: Session,
    period_id: int,
    modifications: Dict[str, Any]
) -> ContentPlanPeriod:
    """
    Обновляет существующий контент-план (кроме опубликованных постов).
    
    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана
        modifications: Изменения для применения
        
    Returns:
        ContentPlanPeriod: Обновлённый период
    """
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id
    ).first()
    
    if not period:
        raise ValueError("Период контент-плана не найден")
    
    # Обновляем информацию о периоде
    if "period_type" in modifications:
        period.period_type = modifications["period_type"]
    
    if "community_info" in modifications:
        period.community_info = modifications["community_info"]
    
    # Обновляем только черновики и одобренные посты (не опубликованные)
    editable_posts = db.query(Post).filter(
        Post.content_plan_period_id == period_id,
        Post.status.in_(["draft", "approved"])
    ).all()
    
    if "post_topics" in modifications:
        for i, post in enumerate(editable_posts):
            if i < len(modifications["post_topics"]):
                topic_data = modifications["post_topics"][i]
                post.topic = topic_data.get("topic", post.topic)
                post.post_type = topic_data.get("type", post.post_type)
    
    period.updated_at = datetime.now()
    db.commit()
    db.refresh(period)
    
    return period


def check_and_regenerate_expiring_plan(
    db: Session,
    user_id: int
) -> Optional[ContentPlanPeriod]:
    """
    Проверяет, не подходит ли к концу текущий план, и создаёт новый если нужно.
    
    Args:
        db: Сессия базы данных
        user_id: ID пользователя
        
    Returns:
        Optional[ContentPlanPeriod]: Новый период если создан, иначе None
    """
    now = datetime.now()
    
    # Ищем активный период
    active_period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.user_id == user_id,
        ContentPlanPeriod.status == "active"
    ).first()
    
    if not active_period:
        return None
    
    # Если до конца периода осталось меньше 3 дней
    days_left = (active_period.end_date - now).days
    
    if days_left <= 3:
        logger.info(f"Период {active_period.id} заканчивается через {days_left} дней, создаём новый")
        
        # Помечаем текущий как завершённый
        active_period.status = "completed"
        active_period.updated_at = now
        db.commit()
        
        # Создаём новый период с теми же параметрами
        new_period = get_or_create_content_plan_period(
            db=db,
            user_id=user_id,
            period_type=active_period.period_type,
            community_info=active_period.community_info
        )
        
        return new_period
    
    return None
