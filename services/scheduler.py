"""
Фоновый планировщик публикаций на основе APScheduler.
Проверяет и публикует одобренные посты по расписанию.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Post
from publishers.vk_publisher import VKPublisher
from config import settings

logger = logging.getLogger(__name__)

# Глобальная переменная для планировщика
_scheduler: BackgroundScheduler = None


def check_and_publish():
    """
    Проверка и публикация постов.
    
    Каждые 30 минут проверяет БД на наличие постов со статусом 'approved'
    и publish_at <= now(). Если такие есть — публикует через VKPublisher.
    """
    logger.info("Планировщик: запуск проверки постов для публикации...")
    
    db = SessionLocal()
    try:
        # Находим все одобренные посты, которые должны быть опубликованы
        now = datetime.now()
        posts_to_publish = db.query(Post).filter(
            Post.status == "approved",
            Post.publish_at <= now
        ).order_by(Post.publish_at).all()
        
        if not posts_to_publish:
            logger.debug("Планировщик: нет постов для публикации")
            return
        
        logger.info(f"Планировщик: найдено {len(posts_to_publish)} постов для публикации")
        
        # Инициализируем паблишер
        publisher = VKPublisher(
            token=settings.vk_token,
            group_id=settings.vk_group_id
        )
        
        for post in posts_to_publish:
            try:
                logger.info(f"Публикация поста ID={post.id} (тип: {post.post_type})")
                
                # Получаем текст для публикации
                text_to_publish = post.text_final or post.text_draft
                if not text_to_publish:
                    logger.warning(f"Пост ID={post.id} не имеет текста, пропускаем")
                    continue
                
                # Формируем данные для публикации
                post_data = {
                    "text": text_to_publish,
                    "image_path": post.image_url if post.image_url else None
                }
                
                # Публикуем
                result = publisher.publish(post_data)
                
                if result.get("success"):
                    # Обновляем статус в БД
                    post.status = "published"
                    post.published_at = datetime.now()
                    db.commit()
                    
                    logger.info(
                        f"Пост ID={post.id} успешно опубликован: {result.get('url')} "
                        f"(VK post_id: {result.get('post_id')})"
                    )
                else:
                    error_msg = result.get("error", "Неизвестная ошибка")
                    logger.error(f"Ошибка публикации поста ID={post.id}: {error_msg}")
                    # Не меняем статус, чтобы попробовать позже
                    
            except Exception as e:
                logger.error(f"Критическая ошибка при публикации поста ID={post.id}: {e}")
                db.rollback()
                continue
        
        logger.info(f"Планировщик: проверка завершена. Обработано {len(posts_to_publish)} постов.")
        
    except Exception as e:
        logger.error(f"Ошибка в check_and_publish: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """
    Запуск фонового планировщика публикаций.
    
    Планировщик запускает check_and_publish() каждые 30 минут.
    """
    global _scheduler
    
    if _scheduler is not None:
        logger.warning("Планировщик уже запущен")
        return
    
    if not settings.vk_token or not settings.vk_group_id:
        logger.warning("VK_TOKEN или VK_GROUP_ID не настроены. Планировщик не будет запущен.")
        return
    
    logger.info("Запуск планировщика публикаций...")
    
    # Создаём планировщик
    _scheduler = BackgroundScheduler(
        timezone="Europe/Moscow",  # Можно настроить через settings
        job_defaults={
            "coalesce": True,  # Объединять пропущенные запуски
            "max_instances": 1,  # Только один экземпляр задачи одновременно
            "misfire_grace_time": 60  # Допустимое время опоздания (секунды)
        }
    )
    
    # Добавляем задачу: проверять каждые 30 минут
    _scheduler.add_job(
        check_and_publish,
        trigger="interval",
        minutes=30,
        id="check_and_publish",
        name="Проверка и публикация постов",
        replace_existing=True
    )
    
    # Также запускаем проверку сразу при старте (опционально)
    # _scheduler.add_job(
    #     check_and_publish,
    #     trigger="date",
    #     run_date=datetime.now(),
    #     id="check_and_publish_initial",
    #     name="Первичная проверка постов"
    # )
    
    # Запускаем планировщик
    _scheduler.start()
    
    logger.info("Планировщик публикаций запущен. Проверка каждые 30 минут.")


def stop_scheduler():
    """Остановка планировщика."""
    global _scheduler
    
    if _scheduler is None:
        return
    
    logger.info("Остановка планировщика...")
    _scheduler.shutdown(wait=True)
    _scheduler = None
    logger.info("Планировщик остановлен")


def get_scheduler() -> BackgroundScheduler:
    """Получение экземпляра планировщика."""
    return _scheduler
