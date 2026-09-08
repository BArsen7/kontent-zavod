"""
CLI-скрипт для инициализации базы данных.

Использование:
    python scripts/setup_db.py
    python scripts/setup_db.py --with-test-data

Аргументы:
    --with-test-data  Добавить тестовые данные после создания таблиц
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Добавляем корень проекта в PATH для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from database import init_db, SessionLocal, engine, Base
from models import PlatformAccount, ContentPlan, Post, PostStats

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
        description="Инициализация базы данных и создание таблиц",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--with-test-data",
        action="store_true",
        help="Добавить тестовые данные после создания таблиц"
    )
    
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Удалить существующие таблицы перед созданием (ОСТОРОЖНО: все данные будут потеряны)"
    )
    
    return parser.parse_args()


def create_test_data(db: SessionLocal):
    """Создание тестовых данных для демонстрации."""
    logger.info("Создание тестовых данных...")
    
    try:
        # Создание тестового аккаунта VK
        vk_account = PlatformAccount(
            platform="vk",
            account_id=str(settings.vk_group_id) if settings.vk_group_id else "test_group",
            access_token=settings.vk_token if settings.vk_token else "test_token",
            config_json={"auto_publish": True}
        )
        db.add(vk_account)
        logger.info("Создан тестовый аккаунт VK")
        
        # Создание тестового контент-плана на текущую неделю
        now = datetime.now()
        current_week = now.isocalendar()[1]
        current_year = now.year
        
        content_plan = ContentPlan(
            week_number=current_week,
            year=current_year,
            created_at=now
        )
        db.add(content_plan)
        db.commit()
        db.refresh(content_plan)
        logger.info(f"Создан тестовый контент-план на неделю {current_week}")
        
        # Создание тестовых постов разных типов
        test_posts = [
            {
                "post_type": "benefit",
                "topic": "Как правильно хранить 3D-формочки",
                "text_draft": "Привет, друзья! 🍪 Сегодня расскажу, как правильно хранить наши волшебные формочки...",
                "text_final": "Привет, друзья! 🍪 Сегодня расскажу, как правильно хранить наши волшебные формочки...",
                "status": "draft",
                "publish_at": now + timedelta(hours=2)
            },
            {
                "post_type": "engagement",
                "topic": "А какое ваше любимое печенье?",
                "text_draft": "Ребята, а давайте поделимся в комментариях — какое печенье вы любите больше всего?",
                "text_final": "Ребята, а давайте поделимся в комментариях — какое печенье вы любите больше всего?",
                "status": "approved",
                "publish_at": now - timedelta(hours=1)  # Уже должно быть опубликовано
            },
            {
                "post_type": "entertainment",
                "topic": "Смешной случай на кухне",
                "text_draft": "Вчера у меня случился забавный момент на кухне 😄 Пекла печенье...",
                "text_final": "Вчера у меня случился забавный момент на кухне 😄 Пекла печенье...",
                "status": "published",
                "publish_at": now - timedelta(days=1),
                "published_at": now - timedelta(days=1)
            },
            {
                "post_type": "sales",
                "topic": "Новая коллекция формочек 'Зимняя сказка'",
                "text_draft": "Друзья, у нас прекрасная новость! 🎄 Встречайте новую коллекцию...",
                "text_final": "Друзья, у нас прекрасная новость! 🎄 Встречайте новую коллекцию...",
                "status": "draft",
                "publish_at": now + timedelta(days=3)
            }
        ]
        
        for post_data in test_posts:
            post = Post(
                content_plan_id=content_plan.id,
                **post_data
            )
            db.add(post)
        
        db.commit()
        logger.info(f"Создано {len(test_posts)} тестовых постов")
        
        # Создание тестовой статистики для опубликованного поста
        published_post = db.query(Post).filter(Post.status == "published").first()
        if published_post:
            stats = PostStats(
                post_id=published_post.id,
                platform="vk",
                views=150,
                likes=23,
                comments=5,
                collected_at=now
            )
            db.add(stats)
            db.commit()
            logger.info(f"Создана тестовая статистика для поста ID={published_post.id}")
        
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при создании тестовых данных: {e}")
        db.rollback()
        return False


def main():
    """Основная функция скрипта."""
    args = parse_args()
    
    logger.info("Запуск инициализации базы данных")
    
    try:
        # Опциональное удаление существующих таблиц
        if args.drop_existing:
            logger.warning("Удаление существующих таблиц...")
            print("\n⚠️  ВНИМАНИЕ: Все существующие данные будут удалены!")
            response = input("Вы уверены? Введите 'yes' для подтверждения: ")
            if response.lower() != 'yes':
                print("Отменено пользователем.")
                sys.exit(0)
            
            Base.metadata.drop_all(bind=engine)
            logger.info("Все таблицы удалены")
        
        # Инициализация БД (создание таблиц)
        init_db()
        logger.info("Таблицы успешно созданы")
        print("\n✓ База данных успешно инициализирована")
        print(f"  Путь к файлу: {settings.db_path}")
        
        # Добавление тестовых данных
        if args.with_test_data:
            print("\n📝 Создание тестовых данных...")
            db = SessionLocal()
            try:
                if create_test_data(db):
                    print("✓ Тестовые данные успешно созданы")
                    
                    # Вывод сводки
                    print("\n" + "=" * 60)
                    print("СВОДКА ПО ТЕСТОВЫМ ДАННЫМ")
                    print("=" * 60)
                    
                    posts_count = db.query(Post).count()
                    draft_count = db.query(Post).filter(Post.status == "draft").count()
                    approved_count = db.query(Post).filter(Post.status == "approved").count()
                    published_count = db.query(Post).filter(Post.status == "published").count()
                    
                    print(f"Всего постов: {posts_count}")
                    print(f"  • draft: {draft_count}")
                    print(f"  • approved: {approved_count}")
                    print(f"  • published: {published_count}")
                    print("=" * 60)
                else:
                    print("❌ Ошибка при создании тестовых данных")
                    sys.exit(1)
            finally:
                db.close()
        
        print("\n✅ Готово!")
        logger.info("Инициализация базы данных завершена успешно")
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        print(f"\n❌ Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
