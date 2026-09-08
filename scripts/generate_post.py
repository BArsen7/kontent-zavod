"""
CLI-скрипт для генерации одиночного поста.

Использование:
    python scripts/generate_post.py --topic "Как мыть 3D-формочки" --type "польза"
    python scripts/generate_post.py --topic "Новогодние печеньки" --type "продажа" --model "gigachat"

Аргументы:
    --topic   Тема поста (обязательный)
    --type    Тип поста: польза/вовлечение/развлечение/продажа (по умолчанию "польза")
    --model   Модель для генерации: ollama/gigachat (по умолчанию "ollama")
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Добавляем корень проекта в PATH для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from database import init_db, SessionLocal
from models import Post
from generators.text_generator import generate_text
from generators.image_generator import generate_kandinsky

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Системные промпты для разных типов постов
SYSTEM_PROMPTS = {
    "польза": (
        "Ты — эксперт по уютной выпечке и созданию декоративных элементов из теста. "
        "Твоя задача — написать полезный, практичный пост для аудитории, которая любит печь. "
        "Тон: тёплый, дружеский, без пафоса. Избегай сложных терминов. "
        "Дай конкретный совет или лайфхак."
    ),
    "вовлечение": (
        "Ты — ведущая уютного блога о выпечке. "
        "Твоя задача — написать пост, который побудит подписчиков к общению в комментариях. "
        "Задай вопрос, предложи выбрать вариант, попроси поделиться опытом. "
        "Тон: очень тёплый, душевный, как разговор на кухне с подругой."
    ),
    "развлечение": (
        "Ты — автор развлекательного контента о мире выпечки. "
        "Напиши лёгкий, забавный пост. Это может быть смешная ситуация на кухне, "
        "курьёзный случай с тестом или просто милая история. "
        "Тон: игривый, лёгкий, с юмором."
    ),
    "продажа": (
        "Ты — бережный продавец уникальных 3D-формочек для печенья. "
        "Напиши пост, который мягко предложит товар, не давя на покупателя. "
        "Сделай акцент на эмоциях, уюте, радости от создания красоты своими руками. "
        "Тон: доверительный, спокойный, без агрессивных призывов."
    )
}

# Промпты для генерации изображений
IMAGE_PROMPTS = {
    "польза": "Cozy baking scene, homemade cookies with detailed relief pattern, educational theme, warm lighting, photorealistic, 4k",
    "вовлечение": "Cozy kitchen scene, person holding cookies, engaging atmosphere, warm lighting, photorealistic, 4k",
    "развлечение": "Funny baking scene, cookies in whimsical shapes, playful atmosphere, bright lighting, photorealistic, 4k",
    "продажа": "Beautiful 3D cookie cutters display, elegant packaging, cozy background, professional product photo, 4k"
}


def parse_args() -> argparse.Namespace:
    """Парсинг аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Генерация одиночного поста с текстом и изображением",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--topic",
        type=str,
        required=True,
        help="Тема поста (обязательный аргумент)"
    )
    
    parser.add_argument(
        "--type",
        type=str,
        choices=["польза", "вовлечение", "развлечение", "продажа"],
        default="польза",
        help="Тип поста (по умолчанию: польза)"
    )
    
    parser.add_argument(
        "--model",
        type=str,
        choices=["ollama", "gigachat"],
        default="ollama",
        help="Модель для генерации текста (по умолчанию: ollama)"
    )
    
    return parser.parse_args()


def main():
    """Основная функция скрипта."""
    args = parse_args()
    
    logger.info(f"Запуск генерации поста: тема='{args.topic}', тип='{args.type}', модель='{args.model}'")
    
    # Инициализация БД
    try:
        init_db()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        sys.exit(1)
    
    db = SessionLocal()
    
    try:
        # Формирование промптов
        system_prompt = SYSTEM_PROMPTS.get(args.type, SYSTEM_PROMPTS["польза"])
        user_prompt = f"{args.topic}. Тематика: 3D-формочки для печенья и уютная выпечка. Отвечай на русском языке."
        
        # Генерация текста
        logger.info("Генерация текста...")
        use_local = (args.model == "ollama")
        try:
            text_content = generate_text(
                prompt=user_prompt,
                system_prompt=system_prompt,
                use_local=use_local
            )
            logger.info(f"Текст успешно сгенерирован ({len(text_content)} символов)")
        except Exception as e:
            logger.error(f"Ошибка генерации текста: {e}")
            sys.exit(1)
        
        # Генерация изображения
        logger.info("Генерация изображения...")
        image_prompt = IMAGE_PROMPTS.get(args.type, IMAGE_PROMPTS["польза"])
        image_path = None
        
        try:
            image_path = generate_kandinsky(
                prompt=image_prompt,
                api_key=settings.gigachat_key,
                secret_key=settings.gigachat_secret,
                save_dir="data/media"
            )
            logger.info(f"Изображение сохранено: {image_path}")
        except Exception as e:
            logger.warning(f"Ошибка генерации изображения: {e}. Продолжаем без картинки.")
        
        # Создание записи в БД
        new_post = Post(
            content_plan_id=0,  # Для одиночных постов не используется
            post_type=args.type,
            topic=args.topic[:255],  # Ограничение длины темы
            text_draft=text_content,
            text_final=text_content,
            image_url=image_path,
            image_source="kandinsky" if image_path else None,
            status="draft",
            publish_at=datetime.now(),
            published_at=None
        )
        
        db.add(new_post)
        db.commit()
        db.refresh(new_post)
        
        logger.info(f"Пост ID={new_post.id} успешно создан и сохранён в БД со статусом 'draft'")
        
        # Вывод результата
        print("\n" + "=" * 60)
        print("РЕЗУЛЬТАТ ГЕНЕРАЦИИ ПОСТА")
        print("=" * 60)
        print(f"ID поста: {new_post.id}")
        print(f"Тема: {args.topic}")
        print(f"Тип: {args.type}")
        print(f"Статус: draft")
        print(f"\nТекст поста:\n{'-' * 40}\n{text_content}\n{'-' * 40}")
        print(f"\nПуть к изображению: {image_path if image_path else 'Не сгенерировано'}")
        print("=" * 60)
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
