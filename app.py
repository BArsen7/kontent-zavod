import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any
import datetime

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config import settings
from database import get_db, init_db
from models import Post
from services.content_service import generate_weekly_pack
from publishers.vk_publisher import VKPublisher

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализация БД при старте
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Инициализация базы данных...")
    init_db()
    logger.info("База данных готова")
    yield
    logger.info("Завершение работы приложения")

app = FastAPI(
    title="Autopilot Content",
    description="Система автоматического ведения соцсетей",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене заменить на конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Шаблоны
templates = Jinja2Templates(directory="web/templates")


# --- Web Routes ---

@app.get("/")
async def index(request: Request, db: Session = Depends(get_db)):
    """Рендерит главную страницу с таблицей постов."""
    posts = db.query(Post).order_by(Post.id.desc()).all()
    return templates.TemplateResponse("index.html", {"request": request, "posts": posts})


# --- API Endpoints ---

@app.get("/api/posts")
async def get_posts(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    """Возвращает список всех постов из БД."""
    posts = db.query(Post).order_by(Post.id.desc()).all()
    
    return [
        {
            "id": post.id,
            "content_plan_id": post.content_plan_id,
            "post_type": post.post_type,
            "topic": post.topic,
            "text_draft": post.text_draft,
            "text_final": post.text_final,
            "image_url": post.image_url,
            "image_source": post.image_source,
            "status": post.status,
            "publish_at": post.publish_at.isoformat() if post.publish_at else None,
            "published_at": post.published_at.isoformat() if post.published_at else None,
        }
        for post in posts
    ]


@app.post("/api/generate/weekly")
async def generate_weekly(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Запускает генерацию недельного пакета постов в фоновом режиме.
    Возвращает немедленный ответ, не дожидаясь завершения генерации.
    """
    logger.info("Получен запрос на генерацию недельного пакета")
    
    def run_generation():
        try:
            # Создаём новую сессию для фонового потока
            from database import SessionLocal
            db_session = SessionLocal()
            try:
                result = generate_weekly_pack(niche="3d_cookies", db=db_session)
                logger.info(f"Генерация завершена. Создано постов: {len(result)}")
            finally:
                db_session.close()
        except Exception as e:
            logger.error(f"Ошибка в фоновой генерации: {e}")
    
    background_tasks.add_task(run_generation)
    
    return {
        "status": "started",
        "message": "Генерация запущена в фоновом режиме",
        "count": 7
    }


@app.post("/api/posts/{post_id}/approve")
async def approve_post(post_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Меняет статус поста на 'approved'."""
    post = db.query(Post).filter(Post.id == post_id).first()
    
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    
    if post.status != "draft":
        raise HTTPException(
            status_code=400, 
            detail=f"Нельзя одобрить пост со статусом '{post.status}'. Одобрять можно только черновики."
        )
    
    post.status = "approved"
    db.commit()
    db.refresh(post)
    
    logger.info(f"Пост ID {post_id} одобрен")
    
    return {
        "success": True,
        "post_id": post_id,
        "status": post.status
    }


@app.post("/api/publish/vk/{post_id}")
async def publish_vk(post_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Публикует пост ВКонтакте через VKPublisher.
    Меняет статус на 'published' при успехе.
    """
    post = db.query(Post).filter(Post.id == post_id).first()
    
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    
    if post.status not in ["approved", "draft"]:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя опубликовать пост со статусом '{post.status}'"
        )
    
    # Проверяем наличие текста
    text_to_publish = post.text_final or post.text_draft
    if not text_to_publish:
        raise HTTPException(status_code=400, detail="У поста нет текста для публикации")
    
    logger.info(f"Публикация поста ID {post_id} ВКонтакте")
    
    try:
        # Инициализируем паблишер
        publisher = VKPublisher(
            token=settings.VK_TOKEN,
            group_id=settings.VK_GROUP_ID
        )
        
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
            post.published_at = datetime.datetime.now()
            db.commit()
            db.refresh(post)
            
            logger.info(f"Пост ID {post_id} успешно опубликован: {result.get('url')}")
            
            return {
                "success": True,
                "post_id": post_id,
                "platform_post_id": result.get("post_id"),
                "url": result.get("url"),
                "status": post.status
            }
        else:
            logger.error(f"Ошибка публикации от VKPublisher: {result.get('error')}")
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "Неизвестная ошибка при публикации")
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Критическая ошибка при публикации: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/status")
async def get_status():
    """Простой эндпоинт для проверки работоспособности API."""
    return {"status": "ok", "version": "1.0.0"}
