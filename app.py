import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any
import datetime
import threading

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import base64
import os
from pathlib import Path

from config import settings
from database import get_db, init_db
from models import Post
from services.content_service import generate_weekly_pack
from publishers.vk_publisher import VKPublisher
from managers.community_manager import CommunityManager
from services.scheduler import start_scheduler

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализация БД при старте, запуск CommunityManager и планировщика
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Инициализация базы данных...")
    init_db()
    logger.info("База данных готова")
    
    # Запускаем CommunityManager (мастер-бот) в отдельном потоке
    logger.info("Запуск CommunityManager (мастер-бот для управления множественными сообществами)...")
    
    # Создаём глобальный экземпляр менеджера
    app.state.community_manager = CommunityManager()
    
    community_thread = threading.Thread(
        target=_run_community_manager,
        args=(app.state.community_manager,),
        name="CommunityManager",
        daemon=True
    )
    community_thread.start()
    
    # Запускаем планировщик публикаций в отдельном потоке
    logger.info("Запуск планировщика публикаций...")
    scheduler_thread = threading.Thread(target=start_scheduler, name="SchedulerStartup", daemon=True)
    scheduler_thread.start()
    
    yield
    
    logger.info("Завершение работы приложения")
    if hasattr(app.state, 'community_manager'):
        app.state.community_manager.stop()


def _run_community_manager(manager: CommunityManager):
    """Функция для запуска CommunityManager в потоке."""
    manager.run()

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

# Статические файлы (для загруженных изображений)
static_path = Path("data/media")
static_path.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


# --- Web Routes ---

@app.get("/")
async def index(request: Request, db: Session = Depends(get_db)):
    """Рендерит главную страницу с таблицей постов."""
    posts = db.query(Post).order_by(Post.id.desc()).all()
    return templates.TemplateResponse(request=request, name="index.html", context={"posts": posts})


@app.get("/post/{post_id}")
async def post_detail(request: Request, post_id: int, db: Session = Depends(get_db)):
    """Рендерит страницу редактирования поста."""
    post = db.query(Post).filter(Post.id == post_id).first()
    
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    
    return templates.TemplateResponse(request=request, name="post_detail.html", context={"post": post})


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
async def publish_vk(
    post_id: int,
    group_id: int | None = None,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Публикует пост ВКонтакте через CommunityManager.
    Если group_id не указан, публикует в первое доступное сообщество.
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
    
    # Получаем менеджер сообществ
    community_manager = app.state.community_manager
    
    # Если group_id не указан, используем первое доступное сообщество
    if group_id is None:
        communities = community_manager.get_community_list()
        if not communities:
            raise HTTPException(
                status_code=400,
                detail="Нет зарегистрированных сообществ. Добавьте сообщество через /add_token"
            )
        group_id = communities[0]["group_id"]
        logger.info(f"group_id не указан, используем первое сообщество: {group_id}")
    
    logger.info(f"Публикация поста ID {post_id} в сообщество {group_id}")
    
    try:
        # Формируем данные для публикации
        post_data = {
            "text": text_to_publish,
            "image_path": post.image_url if post.image_url else None
        }
        
        # Публикуем через CommunityManager
        result = community_manager.publish_to_community(group_id, post_data)
        
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
                "group_id": group_id,
                "platform_post_id": result.get("post_id"),
                "url": result.get("url"),
                "status": post.status
            }
        else:
            logger.error(f"Ошибка публикации от CommunityManager: {result.get('error')}")
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


@app.get("/api/communities")
async def get_communities():
    """Возвращает список всех зарегистрированных сообществ."""
    if not hasattr(app.state, 'community_manager'):
        return {"communities": [], "error": "CommunityManager ещё не инициализирован"}
    
    communities = app.state.community_manager.get_community_list()
    return {"communities": communities}


@app.post("/api/communities/register")
async def register_community(
    token: str,
    group_id: int,
) -> Dict[str, Any]:
    """
    Регистрирует новое сообщество для управления.
    
    Args:
        token: Токен доступа сообщества VK API.
        group_id: ID группы VK.
    """
    if not hasattr(app.state, 'community_manager'):
        raise HTTPException(status_code=503, detail="CommunityManager ещё не инициализирован")
    
    from managers.community_manager import CommunityAccount
    
    community = CommunityAccount(
        id=0,
        platform="vk",
        account_id=str(group_id),
        access_token=token,
        group_id=group_id
    )
    
    if app.state.community_manager.register_community(community):
        return {
            "success": True,
            "group_id": group_id,
            "message": f"Сообщество {group_id} успешно зарегистрировано"
        }
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось зарегистрировать сообщество {group_id}. Проверьте токен и права доступа."
        )


@app.delete("/api/communities/{group_id}")
async def unregister_community(group_id: int) -> Dict[str, Any]:
    """
    Удаляет сообщество из управления.
    
    Args:
        group_id: ID группы для удаления.
    """
    if not hasattr(app.state, 'community_manager'):
        raise HTTPException(status_code=503, detail="CommunityManager ещё не инициализирован")
    
    if app.state.community_manager.unregister_community(group_id):
        return {
            "success": True,
            "group_id": group_id,
            "message": f"Сообщество {group_id} удалено из управления"
        }
    else:
        raise HTTPException(
            status_code=404,
            detail=f"Сообщество {group_id} не найдено"
        )


# --- API Endpoints для редактирования постов ---

@app.put("/api/posts/{post_id}")
async def update_post(
    post_id: int,
    request_data: dict,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Обновляет данные поста.
    
    Args:
        post_id: ID поста.
        request_data: Данные для обновления (post_type, topic, text, images, approve).
    """
    post = db.query(Post).filter(Post.id == post_id).first()
    
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")
    
    # Обновление полей
    if "post_type" in request_data:
        post.post_type = request_data["post_type"]
    if "topic" in request_data:
        post.topic = request_data["topic"]
    if "text" in request_data:
        post.text_final = request_data["text"]
    if "images" in request_data:
        # Сохраняем изображения как CSV строку
        images = request_data["images"][:10]  # Максимум 10
        if images:
            # Если это base64, сохраняем в файлы
            processed_images = []
            for img in images:
                if img.startswith("data:image"):
                    # Это base64 изображение, сохраняем в файл
                    try:
                        header, encoded = img.split(",", 1)
                        image_data = base64.b64decode(encoded)
                        filename = f"{post_id}_{len(processed_images)}_{os.urandom(8).hex()}.png"
                        file_path = static_path / filename
                        with open(file_path, "wb") as f:
                            f.write(image_data)
                        processed_images.append(f"/static/{filename}")
                    except Exception as e:
                        logger.error(f"Ошибка сохранения изображения: {e}")
                        continue
                else:
                    # Это уже URL
                    processed_images.append(img)
            
            post.image_url = ",".join(processed_images)
    
    # Одобрение если запрошено
    if request_data.get("approve"):
        if post.status == "draft":
            post.status = "approved"
    
    db.commit()
    db.refresh(post)
    
    logger.info(f"Пост ID {post_id} обновлён")
    
    return {
        "success": True,
        "post_id": post_id,
        "status": post.status
    }


@app.post("/api/generate/text")
async def generate_text_endpoint(request_data: dict) -> Dict[str, Any]:
    """
    Генерирует текст поста с помощью ИИ.
    
    Args:
        request_data: {"topic": str, "post_type": str}
    """
    from generators.text_generator import generate_text
    
    topic = request_data.get("topic", "Уютная выпечка")
    post_type = request_data.get("post_type", "benefit")
    
    type_names = {
        "benefit": "польза",
        "engagement": "вовлечение", 
        "entertainment": "развлечение",
        "sales": "продажа"
    }
    
    prompt = f"Напиши короткий пост для соцсетей на тему '{topic}' в формате '{type_names.get(post_type, 'пост')}'. Текст должен быть тёплым, дружелюбным, без излишней официальности. Добавь эмодзи. Длина 100-200 слов."
    
    try:
        text = generate_text(prompt)
        return {"text": text}
    except Exception as e:
        logger.error(f"Ошибка генерации текста: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate/rewrite")
async def rewrite_text_endpoint(request_data: dict) -> Dict[str, Any]:
    """
    Переписывает выделенный фрагмент текста с помощью ИИ.
    
    Args:
        request_data: {"text": str, "context": str}
    """
    from generators.text_generator import generate_text
    
    text_to_rewrite = request_data.get("text", "")
    context = request_data.get("context", "")
    
    if not text_to_rewrite.strip():
        raise HTTPException(status_code=400, detail="Текст для переписывания пуст")
    
    prompt = f"Перефразируй следующий текст, сохранив смысл, но сделав его более живым и интересным. Контекст: {context[:500]}... Текст для перефразирования: {text_to_rewrite}"
    
    try:
        rewritten = generate_text(prompt)
        return {"rewritten_text": rewritten}
    except Exception as e:
        logger.error(f"Ошибка переписывания текста: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate/image")
async def generate_image_endpoint(request_data: dict) -> Dict[str, Any]:
    """
    Генерирует изображение по промпту.
    
    Args:
        request_data: {"prompt": str}
    """
    from generators.image_generator import generate_kandinsky
    
    prompt = request_data.get("prompt", "")
    
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Промпт пуст")
    
    # Улучшаем промпт с помощью ИИ
    try:
        from generators.text_generator import generate_text
        
        enhance_prompt = f"Преобразуй этот запрос в детальный промпт для генерации изображения на английском языке. Добавь детали об освещении, композиции, стиле. Запрос: {prompt}"
        enhanced = generate_text(enhance_prompt, use_local=True)
        
        if enhanced:
            prompt = enhanced
    except Exception as e:
        logger.warning(f"Не удалось улучшить промпт: {e}")
    
    try:
        # Генерируем изображение через Kandinsky
        image_path = generate_kandinsky(
            prompt=prompt,
            api_key=settings.gigachat_key or "",
            secret_key=settings.gigachat_secret or ""
        )
        
        return {"image_path": image_path}
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        raise HTTPException(status_code=500, detail=str(e))
