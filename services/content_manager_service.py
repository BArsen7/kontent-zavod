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
    Post,
    UserCommunity,
)
from generators.text_generator import generate_text

logger = logging.getLogger(__name__)

# Строгий структурированный вопрос, который ИИ обязан задать первым,
# пока сообщество не настроено (CommunityGenerationContext.is_configured == False).
CONTEXT_SETUP_QUESTION = (
    "Для настройки генерации ответьте: "
    "1) Цель, 2) Специфика, 3) ЦА, 4) Что уже работает, "
    "5) Предпочтения по картинкам (ИИ/Поиск/Загрузка)"
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
    """Возвращает первое сообщество пользователя периода (для привязки контекста)."""
    return db.query(UserCommunity).filter(
        UserCommunity.user_id == period.user_id
    ).order_by(UserCommunity.id.asc()).first()


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

    return (
        "<CONTEXT>\n"
        f"Сообщество: {community.group_name or community.group_id}\n"
        f"Цель: {ctx.goal or 'не указана'}\n"
        f"Специфика сообщества: {ctx.community_specifics or 'не указана'}\n"
        f"Целевая аудитория: {ctx.target_audience or 'не указана'}\n"
        f"Что уже есть и что работает: {ctx.current_state_and_what_works or 'не указано'}\n"
        f"Предпочтения по источникам картинок: {prefs_str or 'не указаны'}\n"
        "</CONTEXT>"
    )


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
    ).order_by(ChatMessage.created_at.asc()).all()
    
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
    
    # Генерируем ответ через ИИ
    try:
        # FIX: провайдер выбирается по системной настройке ai_provider
        # (ollama | cloud | gigachat), а не жёстко Ollama.
        response = generate_text(
            prompt=context,
            system_prompt=CONTENT_MANAGER_SYSTEM_PROMPT,
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
) -> str:
    """
    Инициализирует чат с первыми вопросами от ИИ-контент-менеджера.
    
    Args:
        db: Сессия базы данных
        period_id: ID периода контент-плана
        
    Returns:
        str: Приветственное сообщение с вопросами
    """
    welcome_message = """Здравствуйте! Я ваш персональный контент-менеджер. 
Помогу вам создать эффективный контент-план для вашего сообщества.

Чтобы начать, расскажите немного о вашем проекте:

1. Какая тематика у вашего сообщества? (например, выпечка, рукоделие, путешествия)
2. Кто ваша целевая аудитория? (возраст, интересы)
3. Какие цели вы преследуете? (продажи, вовлечение, узнаваемость)

Ответьте на эти вопросы, и я предложу вам персональный контент-план!"""
    
    add_chat_message(db, period_id, "assistant", welcome_message)
    
    return welcome_message


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
