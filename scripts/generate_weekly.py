"""
CLI-скрипт для генерации контент-плана на неделю.

Использование:
    python scripts/generate_weekly.py
    python scripts/generate_weekly.py --niche "3d_cookie_cutters"
    python scripts/generate_weekly.py --niche "пряники" --model "gigachat"

Аргументы:
    --niche   Ниша/тематика контента (по умолчанию "3d_cookie_cutters")
    --model   Модель для генерации: ollama/gigachat (по умолчанию "ollama")
"""

import argparse
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в PATH для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from database import init_db, SessionLocal
from services.content_service import generate_weekly_pack

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Парсинг аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Генерация контент-плана на неделю (7 постов)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--niche",
        type=str,
        default="3d_cookie_cutters",
        help="Ниша/тематика контента (по умолчанию: 3d_cookie_cutters)"
    )
    
    parser.add_argument(
        "--model",
        type=str,
        choices=["ollama", "gigachat"],
        default="ollama",
        help="Модель для генерации текста (по умолчанию: ollama). В текущей версии используется только ollama."
    )
    
    return parser.parse_args()


def main():
    """Основная функция скрипта."""
    args = parse_args()
    
    logger.info(f"Запуск генерации недельного пакета: ниша='{args.niche}', модель='{args.model}'")
    
    # Инициализация БД
    try:
        init_db()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        sys.exit(1)
    
    db = SessionLocal()
    
    try:
        # Генерация недельного пакета
        logger.info("Запуск генерации недельного пакета...")
        created_posts = generate_weekly_pack(
            niche=args.niche,
            db=db
        )
        
        if not created_posts:
            logger.warning("Не удалось создать ни одного поста")
            print("\n⚠️  Не удалось создать ни одного поста. Проверьте логи.")
            sys.exit(1)
        
        # Вывод результата
        print("\n" + "=" * 60)
        print("РЕЗУЛЬТАТ ГЕНЕРАЦИИ НЕДЕЛЬНОГО ПАКА")
        print("=" * 60)
        print(f"Всего создано постов: {len(created_posts)}")
        print(f"Ниша: {args.niche}")
        print("-" * 60)
        print(f"{'ID':<6} {'Тип':<15} {'Тема':<35} {'Статус':<10} {'Изобр.':<6}")
        print("-" * 60)
        
        for post in created_posts:
            has_image = "✓" if post.get("has_image", False) else "✗"
            topic = post.get("topic", "N/A")[:33] + "..." if len(post.get("topic", "")) > 35 else post.get("topic", "N/A")
            print(f"{post.get('id', 'N/A'):<6} {post.get('type', 'N/A'):<15} {topic:<35} {post.get('status', 'N/A'):<10} {has_image:<6}")
        
        print("-" * 60)
        
        # Статистика по типам
        type_counts = {}
        for post in created_posts:
            post_type = post.get("type", "unknown")
            type_counts[post_type] = type_counts.get(post_type, 0) + 1
        
        print("\nСтатистика по типам постов:")
        for post_type, count in sorted(type_counts.items()):
            print(f"  • {post_type}: {count}")
        
        print("=" * 60)
        
        logger.info(f"Генерация завершена. Создано {len(created_posts)} постов.")
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
