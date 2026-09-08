"""
CLI-скрипт для ручной публикации всех одобренных постов.

Использование:
    python scripts/publish_pending.py

Скрипт находит все посты со статусом 'approved' в БД и публикует их в VK.
После успешной публикации статус поста меняется на 'published'.
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

# Добавляем корень проекта в PATH для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from database import init_db, SessionLocal
from models import Post
from publishers.vk_publisher import VKPublisher

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
        description="Публикация всех одобренных постов в VK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничить количество публикуемых постов (по умолчанию: все)"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Режим проверки: показать, что будет опубликовано, без реальной публикации"
    )
    
    return parser.parse_args()


def main():
    """Основная функция скрипта."""
    args = parse_args()
    
    logger.info("Запуск публикации одобренных постов")
    
    # Инициализация БД
    try:
        init_db()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        sys.exit(1)
    
    db = SessionLocal()
    
    try:
        # Поиск всех одобренных постов
        pending_posts = db.query(Post).filter(
            Post.status == "approved"
        ).order_by(Post.publish_at.asc()).all()
        
        if not pending_posts:
            logger.info("Нет постов со статусом 'approved' для публикации")
            print("\n✓ Нет постов со статусом 'approved' для публикации.")
            return
        
        # Ограничение количества постов
        if args.limit:
            pending_posts = pending_posts[:args.limit]
            logger.info(f"Ограничено до {args.limit} постов")
        
        total_posts = len(pending_posts)
        logger.info(f"Найдено {total_posts} постов для публикации")
        
        # Инициализация VK Publisher
        if not settings.vk_token or not settings.vk_group_id:
            logger.error("VK токен или ID группы не настроены в .env файле")
            print("\n❌ Ошибка: VK токен или ID группы не настроены в .env файле")
            sys.exit(1)
        
        vk_publisher = VKPublisher(
            token=settings.vk_token,
            group_id=settings.vk_group_id
        )
        
        # Статистика
        success_count = 0
        error_count = 0
        skipped_count = 0
        
        # Вывод заголовка
        print("\n" + "=" * 60)
        if args.dry_run:
            print("РЕЖИМ ПРОВЕРКИ (dry-run) - публикация не будет выполнена")
        else:
            print("ПУБЛИКАЦИЯ ОДОБРЕННЫХ ПОСТОВ")
        print("=" * 60)
        print(f"Всего постов для публикации: {total_posts}")
        print("-" * 60)
        
        # Публикация каждого поста
        for i, post in enumerate(pending_posts, 1):
            print(f"\n[{i}/{total_posts}] Пост ID={post.id}")
            print(f"  Тема: {post.topic}")
            print(f"  Тип: {post.post_type}")
            print(f"  Текст (первые 50 симв.): {post.text_final[:50] if post.text_final else 'N/A'}...")
            
            if args.dry_run:
                print(f"  → [DRY-RUN] Будет опубликован")
                skipped_count += 1
                continue
            
            # Подготовка данных для публикации
            post_data = {
                "text": post.text_final or "",
                "image_path": post.image_url if post.image_url else None
            }
            
            # Проверка наличия текста
            if not post_data["text"]:
                logger.warning(f"Пост ID={post.id} не имеет текста, пропускаем")
                print(f"  ⚠️  Пропущен: нет текста")
                skipped_count += 1
                continue
            
            # Публикация
            try:
                result = vk_publisher.publish(post_data)
                
                if result.get("success"):
                    post_id = result.get("post_id")
                    post_url = result.get("url", f"https://vk.com/wall-{settings.vk_group_id}_{post_id}")
                    
                    # Обновление статуса в БД
                    post.status = "published"
                    post.published_at = datetime.now()
                    db.commit()
                    
                    logger.info(f"Пост ID={post.id} успешно опубликован. URL: {post_url}")
                    print(f"  ✓ Успешно опубликован! URL: {post_url}")
                    success_count += 1
                else:
                    error_msg = result.get("error", "Неизвестная ошибка")
                    logger.error(f"Пост ID={post.id} не опубликован: {error_msg}")
                    print(f"  ❌ Ошибка: {error_msg}")
                    error_count += 1
                    
            except Exception as e:
                logger.error(f"Критическая ошибка при публикации поста ID={post.id}: {e}")
                print(f"  ❌ Критическая ошибка: {e}")
                error_count += 1
                db.rollback()
        
        # Итоговая статистика
        print("\n" + "=" * 60)
        print("ИТОГИ")
        print("=" * 60)
        print(f"Всего постов: {total_posts}")
        print(f"✓ Успешно: {success_count}")
        print(f"❌ Ошибки: {error_count}")
        if args.dry_run:
            print(f"⊘ Пропущено (dry-run): {skipped_count}")
        else:
            print(f"⚠️  Пропущено: {skipped_count}")
        print("=" * 60)
        
        logger.info(f"Публикация завершена. Успешно: {success_count}, Ошибки: {error_count}, Пропущено: {skipped_count}")
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
