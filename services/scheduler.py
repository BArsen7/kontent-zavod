"""
Фоновый планировщик публикаций на основе APScheduler.
Проверяет и публикует одобренные посты по расписанию.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from database import SessionLocal
from models import ContentPlanPeriod, Post, User, UserCommunity
from publishers.vk_publisher import VKPublisher
from config import settings

logger = logging.getLogger(__name__)

# Глобальная переменная для планировщика
_scheduler: BackgroundScheduler = None


def _is_publishing_allowed(db, post: Post) -> tuple[bool, str]:
    """Проверяет, разрешена ли публикация поста (защита заблокированных сущностей).

    Правила:
    1. Если пост привязан к периоду контент-плана, а владелец периода заблокирован
       (User.is_blocked) — публикация запрещена.
    2. Если у владельца есть сообщества и ВСЕ его активные для постинга сообщества
       заблокированы (UserCommunity.is_blocked) — публикация запрещена.

    Возвращает (разрешено: bool, причина_пропуска: str).
    """
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == post.content_plan_period_id
    ).first() if post.content_plan_period_id else None

    user = None
    if period and period.user_id:
        user = db.query(User).filter(User.id == period.user_id).first()

    if user is not None and getattr(user, "is_blocked", False):
        return False, f"владелец аккаунта {user.email} заблокирован"

    if user is not None:
        comms = db.query(UserCommunity).filter(
            UserCommunity.user_id == user.id,
            UserCommunity.can_post.is_(True),
        ).all()
        if comms and all(getattr(c, "is_blocked", False) for c in comms):
            return False, f"все сообщества пользователя {user.email} заблокированы"

    return True, ""


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
                # Защита заблокированных сущностей: пропускаем посты, если
                # владелец или его сообщества заблокированы (не спашим VK API).
                allowed, skip_reason = _is_publishing_allowed(db, post)
                if not allowed:
                    logger.info(
                        f"Планировщик: пропуск публикации поста ID={post.id} — {skip_reason}"
                    )
                    continue

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
                    # FIX (аналитика): сохраняем VK post_id — он нужен задаче
                    # обновления метрик (wall.getById) раз в 24 часа.
                    vk_post_id = result.get("post_id")
                    if vk_post_id is not None:
                        try:
                            post.vk_post_id = int(vk_post_id)
                        except (TypeError, ValueError):
                            logger.warning(
                                f"Не удалось сохранить vk_post_id={vk_post_id} для поста ID={post.id}"
                            )
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


def update_metrics_job() -> None:
    """Фоновая задача: обновление метрик опубликованных постов из VK.

    Запускается планировщиком раз в 24 часа; вызывает
    services.vk_analytics_service.update_metrics_for_published_posts()
    (wall.getById) и обновляет PostStats/HistoricalPost.
    Все ошибки перехватываются — задача не должна ронять планировщик.
    """
    logger.info("Планировщик: запуск задачи обновления метрик постов...")
    try:
        from services.vk_analytics_service import update_metrics_for_published_posts

        updated = update_metrics_for_published_posts()
        logger.info(f"Планировщик: обновление метрик завершено, обновлено постов: {updated}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Ошибка в update_metrics_job: {type(e).__name__}: {e}")


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

    # Задача аналитики: раз в 24 часа обновлять метрики опубликованных
    # постов из VK (wall.getById) — services/vk_analytics_service.py.
    _scheduler.add_job(
        update_metrics_job,
        trigger="interval",
        hours=24,
        id="update_metrics",
        name="Обновление метрик опубликованных постов VK",
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
