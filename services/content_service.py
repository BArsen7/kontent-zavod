import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Any
from sqlalchemy.orm import Session

from models import ContentPlan, Post
from generators.text_generator import generate_text
from generators.image_generator import generate_kandinsky
from config import settings

logger = logging.getLogger(__name__)

# Системные промпты для разных типов постов
SYSTEM_PROMPTS = {
    "benefit": (
        "Ты — эксперт по уютной выпечке и созданию декоративных элементов из теста. "
        "Твоя задача — написать полезный, практичный пост для аудитории, которая любит печь. "
        "Тон: тёплый, дружеский, без пафоса. Избегай сложных терминов. "
        "Дай конкретный совет или лайфхак."
    ),
    "engagement": (
        "Ты — ведущая уютного блога о выпечке. "
        "Твоя задача — написать пост, который побудит подписчиков к общению в комментариях. "
        "Задай вопрос, предложи выбрать вариант, попроси поделиться опытом. "
        "Тон: очень тёплый, душевный, как разговор на кухне с подругой."
    ),
    "entertainment": (
        "Ты — автор развлекательного контента о мире выпечки. "
        "Напиши лёгкий, забавный пост. Это может быть смешная ситуация на кухне, "
        "курьёзный случай с тестом или просто милая история. "
        "Тон: игривый, лёгкий, с юмором."
    ),
    "sales": (
        "Ты — бережный продавец уникальных 3D-формочек для печенья. "
        "Напиши пост, который мягко предложит товар, не давя на покупателя. "
        "Сделай акцент на эмоциях, уюте, радости от создания красоты своими руками. "
        "Тон: доверительный, спокойный, без агрессивных призывов."
    )
}

USER_PROMPTS = {
    "benefit": [
        "Расскажи, как сделать так, чтобы фигурки из теста не теряли форму при выпечке.",
        "Поделись секретом идеального теста для пряников, которое не липнет к формочкам.",
        "Как правильно хранить 3D-формочки, чтобы они служили годами?",
        "Почему важно давать тесту отдохнуть перед раскаткой? Объясни просто.",
        "Топ-3 ошибки новичков при работе с рельефными формочками и как их избежать.",
        "Как добиться чёткого рельефа на печенье? Маленькие хитрости.",
        "Чем смазывать формочки, чтобы печенье легко вынималось?"
    ],
    "engagement": [
        "А какое ваше самое любимое зимнее печенье? Делитесь в комментариях!",
        "Что сложнее: замесить тесто или дождаться, пока оно остынет? :) Ставьте +, если знакомы.",
        "Любите ли вы добавлять специи в выпечку? Какая ваша любимая комбинация?",
        "Покажите в комментариях фото ваших шедевров! Нам очень интересно посмотреть.",
        "А вы дарите домашнее печенье друзьям или всё съедаете сами? Честно!",
        "Какой праздник вы любите печь больше всего? Новый год или что-то другое?"
    ],
    "entertainment": [
        "Случай на кухне: когда пыталась сделать снежинку, получилось... нечто космическое!",
        "История о том, как кот решил помочь мне с раскаткой теста. Было весело!",
        "Когда сказала мужу 'это последняя формочка', а сама заказала ещё пять...",
        "Ожидание vs Реальность: моё первое печенье из 3D-формочки. Смешно до слёз!",
        "Если бы печенье умело говорить, что бы оно сказало, когда его достают из печи?"
    ],
    "sales": [
        "Представь: утро, запах корицы, и ты достаёшь из духовки вот такую красоту. Наши формочки помогают создавать такие моменты.",
        "Ищете подарок для того, у кого всё есть? Попробуйте подарить возможность творить. Наши 3D-формочки — это магия в ваших руках.",
        "Уют начинается с мелочей. Чашка чая и домашнее печенье в форме звёздочек... Хотите такое же?",
        "Лимитированная коллекция формочек 'Зимняя сказка'. Успейте создать своё чудо до праздников.",
        "Не просто формочка, а инструмент для создания семейных традиций. Попробуйте и убедитесь сами."
    ]
}


def generate_weekly_pack(niche: str = "3d_cookies", db: Session = None) -> List[Dict[str, Any]]:
    """
    Генерирует контент-план на неделю (7 постов) для заданной ниши.
    Структура: 3 польза, 2 вовлечение, 1 развлечение, 1 продажа.
    
    Args:
        niche: Строка с описанием ниши (пока используется хардкод для выпечки).
        db: Сессия базы данных SQLAlchemy.
        
    Returns:
        Список словарей с информацией о созданных постах.
    """
    if db is None:
        logger.error("Сессия БД не передана в generate_weekly_pack")
        raise ValueError("Database session is required")

    logger.info(f"Начинаем генерацию недельного пакета для ниши: {niche}")

    # Определяем структуру недели
    post_types = ["benefit"] * 3 + ["engagement"] * 2 + ["entertainment"] * 1 + ["sales"] * 1
    
    # Создаём или получаем контент-план на текущую неделю
    now = datetime.now()
    current_week = now.isocalendar()[1]
    current_year = now.year
    
    content_plan = db.query(ContentPlan).filter(
        ContentPlan.week_number == current_week,
        ContentPlan.year == current_year
    ).first()
    
    if not content_plan:
        content_plan = ContentPlan(
            week_number=current_week,
            year=current_year,
            created_at=now
        )
        db.add(content_plan)
        db.commit()
        db.refresh(content_plan)
        logger.info(f"Создан новый контент-план на неделю {current_week}, год {current_year}")
    else:
        logger.info(f"Используем существующий контент-план ID: {content_plan.id}")

    created_posts = []

    for i, post_type in enumerate(post_types):
        try:
            logger.info(f"Генерация поста {i+1}/7 тип: {post_type}")
            
            # Выбираем промпт
            system_prompt = SYSTEM_PROMPTS.get(post_type, SYSTEM_PROMPTS["benefit"])
            user_prompts_list = USER_PROMPTS.get(post_type, USER_PROMPTS["benefit"])
            # Берём случайный или по порядку (здесь по порядку с зацикливанием)
            user_prompt = user_prompts_list[i % len(user_prompts_list)]
            
            full_user_prompt = f"{user_prompt} Тематика: {niche}. Отвечай на русском языке."
            
            # Генерируем текст
            logger.info("Генерация текста...")
            text_content = generate_text(
                prompt=full_user_prompt,
                system_prompt=system_prompt,
                use_local=True  # По умолчанию используем локальную Ollama
            )
            
            # Генерируем изображение
            logger.info("Генерация изображения...")
            image_prompt = f"Cozy baking scene, homemade cookies with detailed relief pattern, {post_type} theme, warm lighting, photorealistic, 4k"
            
            try:
                image_path = generate_kandinsky(
                    prompt=image_prompt,
                    api_key=settings.gigachat_key,
                    secret_key=settings.gigachat_secret,
                    save_dir="data/media"
                )
                logger.info(f"Изображение сохранено: {image_path}")
            except Exception as img_err:
                logger.error(f"Ошибка генерации изображения: {img_err}. Продолжаем без картинки.")
                image_path = None
            
            # Создаём запись в БД
            new_post = Post(
                content_plan_id=content_plan.id,
                post_type=post_type,
                topic=user_prompt[:50], # Короткая тема
                text_draft=text_content,
                text_final=text_content, # Изначально черновик = финал
                image_url=image_path, # Локальный путь
                image_source="kandinsky" if image_path else None,
                status="draft",
                publish_at=now + timedelta(days=i//2), # Примерное время публикации
                published_at=None
            )
            
            db.add(new_post)
            db.commit()
            db.refresh(new_post)
            
            logger.info(f"Пост ID {new_post.id} успешно создан и сохранён в БД")
            
            created_posts.append({
                "id": new_post.id,
                "type": post_type,
                "topic": new_post.topic,
                "status": new_post.status,
                "has_image": image_path is not None
            })
            
        except Exception as e:
            logger.error(f"Критическая ошибка при генерации поста {i+1}: {e}")
            # Не прерываем весь цикл, пытаемся сделать остальные
            continue

    logger.info(f"Генерация завершена. Создано постов: {len(created_posts)}")
    return created_posts
