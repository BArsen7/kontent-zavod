import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from sqlalchemy.orm import Session

from models import ContentPlan, Post, SystemSetting
from generators.text_generator import generate_text
from generators.image_generator import generate_image
from config import settings

logger = logging.getLogger(__name__)


def _default_use_local(db: Session) -> bool:
    """True — локальный Ollama; False — облачный провайдер (cloud/gigachat)."""
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == "ai_provider").first()
        provider = (row.value or "").strip().lower() if row else "ollama"
        return provider not in ("cloud", "gigachat")
    except Exception as e:  # noqa: BLE001
        # FIX: при битой/отсутствующей таблице system_settings обязательно
        # откатываем сессию — иначе все последующие запросы в ней упадут,
        # и генерация пакета молча деградирует до локальной модели.
        logger.warning(
            f"Не удалось прочитать ai_provider ({type(e).__name__}: {e}) — "
            "fallback на локальную модель."
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return True



def _image_source_name(path: str) -> str:
    """Определяет источник изображения по имени файла для поля Post.image_source."""
    name = (path or "").lower()
    if ".jpg" in name:
        return "gigachat"
    return "kandinsky"


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


# Структуры контент-паков по типу периода (пропорции: польза/вовлечение/развлечение/продажа)
PACK_PERIODS: Dict[str, Dict[str, Any]] = {
    "week": {"days": 7, "post_types": ["benefit"] * 3 + ["engagement"] * 2 + ["entertainment"] * 1 + ["sales"] * 1},
    "two_weeks": {"days": 14, "post_types": ["benefit"] * 5 + ["engagement"] * 4 + ["entertainment"] * 3 + ["sales"] * 2},
    "month": {"days": 30, "post_types": ["benefit"] * 10 + ["engagement"] * 8 + ["entertainment"] * 7 + ["sales"] * 5},
}


def generate_weekly_pack(
    niche: str = "3d_cookies",
    db: Session = None,
    period_type: str = "week",
) -> List[Dict[str, Any]]:
    """
    Генерирует контент-пакет для заданной ниши (неделя / 2 недели / месяц).
    
    Args:
        niche: Строка с описанием ниши (пока используется хардкод для выпечки).
        db: Сессия базы данных SQLAlchemy.
        period_type: Тип периода - 'week' (7), 'two_weeks' (14) или 'month' (30 постов).
        
    Returns:
        Список словарей с информацией о созданных постах.
    """
    if db is None:
        logger.error("Сессия БД не передана в generate_weekly_pack")
        raise ValueError("Database session is required")

    # Валидация и нормализация типа периода (fallback -> week)
    if period_type not in PACK_PERIODS:
        logger.warning(f"Неизвестный period_type='{period_type}', используем 'week'")
        period_type = "week"

    days = PACK_PERIODS[period_type]["days"]
    num_posts = len(PACK_PERIODS[period_type]["post_types"])
    post_types = PACK_PERIODS[period_type]["post_types"]

    logger.info(
        f"Начинаем генерацию пакета ({period_type}: {num_posts} постов на {days} дн.) "
        f"для ниши: {niche}"
    )
    
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
            logger.info(f"Генерация поста {i+1}/{num_posts} тип: {post_type}")
            
            # Выбираем промпт
            system_prompt = SYSTEM_PROMPTS.get(post_type, SYSTEM_PROMPTS["benefit"])
            user_prompts_list = USER_PROMPTS.get(post_type, USER_PROMPTS["benefit"])
            # Берём случайный или по порядку (здесь по порядку с зацикливанием)
            user_prompt = user_prompts_list[i % len(user_prompts_list)]
            
            full_user_prompt = f"{user_prompt} Тематика: {niche}. Отвечай на русском языке."
            
            # Генерируем текст
            logger.info("Генерация текста...")
            # Провайдер выбирается по системной настройке ai_provider
            # (ollama | cloud | gigachat); fallback — локальная Ollama.
            text_content = generate_text(
                prompt=full_user_prompt,
                system_prompt=system_prompt,
                use_local=_default_use_local(db)
            )
            
            # Генерируем изображение
            logger.info("Генерация изображения...")
            image_prompt = f"Cozy baking scene, homemade cookies with detailed relief pattern, {post_type} theme, warm lighting, photorealistic, 4k"
            
            try:
                # Провайдер выбирается автоматически (SystemSetting image_provider):
                # GigaChat Premium (нативная генерация) или Kandinsky
                image_path = generate_image(prompt=image_prompt, save_dir="data/media")
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
                image_source=_image_source_name(image_path) if image_path else None,
                status="draft",
                publish_at=now + timedelta(days=(i * days) // num_posts), # Примерное время публикации
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
