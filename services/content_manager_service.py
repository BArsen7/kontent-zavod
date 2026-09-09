import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from models import ContentPlanPeriod, ChatMessage, Post, User, UserCommunity
from generators.text_generator import generate_text

logger = logging.getLogger(__name__)

# Системный промпт для контент-менеджера
CONTENT_MANAGER_SYSTEM_PROMPT = """Ты — персональный контент-менеджер и маркетолог для ведения социальных сетей. 
Твоя задача — помочь пользователю создать эффективный контент-план для его сообщества.

Твои обязанности:
1. Узнать информацию о сообществе (тематика, целевая аудитория, цели)
2. Задать уточняющие вопросы о предпочтениях в контенте
3. На основе ответов сгенерировать персонализированный контент-план
4. Предложить посты разных типов: полезные, вовлекающие, развлекательные, продающие

Тон общения: дружеский, профессиональный, поддерживающий. Задавай вопросы по одному, не перегружай пользователя.

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
        response = generate_text(
            prompt=context,
            system_prompt=CONTENT_MANAGER_SYSTEM_PROMPT,
            use_local=True
        )
        
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
        
        ai_response = generate_text(
            prompt=prompt,
            system_prompt="Ты эксперт по контент-маркетингу. Верни ТОЛЬКО JSON без дополнительного текста.",
            use_local=True
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
    
    # Получаем первое сообщество пользователя для привязки
    first_community = db.query(UserCommunity).filter(
        UserCommunity.user_id == user_id
    ).first()
    
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
