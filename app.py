import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional
import datetime
import threading
import hashlib
import secrets

import vk_api
import requests as req_lib

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config import settings
from database import get_db, init_db
from models import Post, User, UserCommunity
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

# Хранилище сессий в памяти (для демонстрации)
# В продакшене использовать Redis или базу данных
_session_store: Dict[str, Dict[str, Any]] = {}


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Получает текущего пользователя из сессии."""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in _session_store:
        return None
    
    session_data = _session_store[session_id]
    user_id = session_data.get("user_id")
    
    if not user_id:
        return None
    
    return db.query(User).filter(User.id == user_id).first()


def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    """Требует аутентификацию пользователя."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт деактивирован")
    return user


# --- Web Routes ---

@app.get("/")
async def index(request: Request, db: Session = Depends(get_db)):
    """Рендерит главную страницу с таблицей постов."""
    user = get_current_user(request, db)
    posts = []
    if user:
        posts = db.query(Post).order_by(Post.id.desc()).all()
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"posts": posts, "user": user}
    )


@app.get("/auth/vk")
async def vk_auth(request: Request):
    """Перенаправляет на VK OAuth для авторизации."""
    import urllib.parse
    
    vk_auth_url = "https://oauth.vk.com/authorize"
    params = {
        "client_id": settings.vk_client_id,
        "redirect_uri": settings.vk_redirect_uri,
        "response_type": "code",
        "scope": "offline,groups,wall,photos",
        "v": "5.199",
    }
    
    auth_url = f"{vk_auth_url}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=auth_url)


@app.get("/auth/vk/callback")
async def vk_auth_callback(request: Request, db: Session = Depends(get_db)):
    """Обрабатывает callback от VK OAuth."""
    code = request.query_params.get("code")
    
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code not provided")
    
    # Обмениваем код на токен
    token_url = "https://oauth.vk.com/access_token"
    token_data = {
        "client_id": settings.vk_client_id,
        "client_secret": settings.vk_client_secret,
        "redirect_uri": settings.vk_redirect_uri,
        "code": code,
    }
    
    response = req_lib.post(token_url, data=token_data)
    result = response.json()
    
    if "error" in result:
        raise HTTPException(status_code=400, detail=f"VK OAuth error: {result['error']}")
    
    access_token = result.get("access_token")
    user_id = result.get("user_id")
    
    # Получаем информацию о пользователе
    try:
        vk_session = vk_api.VkApi(token=access_token)
        vk = vk_session.get_api()
        user_info = vk.users.get(user_ids=user_id)[0]
    except Exception as e:
        logger.error(f"Ошибка получения информации о пользователе: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user info from VK")
    
    # Создаем или обновляем пользователя в БД
    user = db.query(User).filter(User.vk_id == str(user_id)).first()
    
    if not user:
        user = User(
            vk_id=str(user_id),
            vk_first_name=user_info.get("first_name", ""),
            vk_last_name=user_info.get("last_name", ""),
            vk_photo=user_info.get("photo_200", ""),
            access_token=access_token,
            is_active=True
        )
        db.add(user)
        logger.info(f"Создан новый пользователь: {user_info.get('first_name')} {user_info.get('last_name')}")
    else:
        user.access_token = access_token
        user.vk_first_name = user_info.get("first_name", "")
        user.vk_last_name = user_info.get("last_name", "")
        user.vk_photo = user_info.get("photo_200", "")
        user.last_login = datetime.datetime.now()
        logger.info(f"Пользователь {user_info.get('first_name')} выполнил вход")
    
    db.commit()
    db.refresh(user)
    
    # Создаем сессию
    session_id = secrets.token_urlsafe(32)
    _session_store[session_id] = {
        "user_id": user.id,
        "created_at": datetime.datetime.now()
    }
    
    # Перенаправляем на главную страницу
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="session_id", value=session_id, httponly=True, max_age=86400*7)
    return response


@app.get("/logout")
async def logout(request: Request):
    """Выполняет выход пользователя."""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in _session_store:
        del _session_store[session_id]
    
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session_id")
    return response


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


@app.get("/api/user/me")
async def get_current_user_info(request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Возвращает информацию о текущем авторизованном пользователе."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    
    return {
        "id": user.id,
        "vk_id": user.vk_id,
        "vk_first_name": user.vk_first_name,
        "vk_last_name": user.vk_last_name,
        "vk_photo": user.vk_photo,
        "is_active": user.is_active
    }


@app.get("/api/user/communities")
async def get_user_communities(request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Возвращает список сообществ текущего пользователя."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    
    communities = db.query(UserCommunity).filter(UserCommunity.user_id == user.id).all()
    
    return {
        "communities": [
            {
                "id": comm.id,
                "group_id": comm.group_id,
                "group_name": comm.group_name or f"Сообщество {comm.group_id}",
                "is_admin": comm.is_admin,
                "can_post": comm.can_post
            }
            for comm in communities
        ]
    }


@app.post("/api/user/communities/add")
async def add_user_community(
    request: Request,
    group_id: int,
    token: str,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Добавляет новое сообщество для текущего пользователя после проверки прав."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    
    # Проверяем токен и получаем информацию о сообществе через VK API
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        
        # Получаем информацию о группе
        group_info = vk.groups.getById(group_id=group_id)[0]
        
        # Проверяем, является ли пользователь администратором
        # Для этого используем метод groups.getCatalog (доступен только админам)
        # или проверяем через groups.get с фильтром
        try:
            # Пытаемся получить информацию о участниках - доступно только админам
            members = vk.groups.getMembers(group_id=group_id, filter="admins")
            is_admin = any(str(user.vk_id) == str(m['user_id']) for m in members.get('items', []))
            
            if not is_admin:
                # Альтернативная проверка: пробуем получить доступ к управлению
                admin_check = vk.groups.getLongPollServer(group_id=group_id)
                is_admin = True
        except vk_api.exceptions.ApiError:
            raise HTTPException(
                status_code=403, 
                detail="У вас нет прав администратора в этом сообществе"
            )
        
        group_name = group_info.get("name", f"Группа {group_id}")
        
    except vk_api.exceptions.AuthError:
        raise HTTPException(status_code=400, detail="Неверный токен доступа")
    except Exception as e:
        logger.error(f"Ошибка проверки токена VK: {e}")
        raise HTTPException(status_code=400, detail=f"Ошибка проверки токена: {str(e)}")
    
    # Проверяем, не добавлено ли уже это сообщество
    existing = db.query(UserCommunity).filter(
        UserCommunity.user_id == user.id,
        UserCommunity.group_id == group_id
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Это сообщество уже добавлено")
    
    # Добавляем сообщество в БД
    user_community = UserCommunity(
        user_id=user.id,
        group_id=group_id,
        group_name=group_name,
        group_token=token,
        is_admin=True,
        can_post=True
    )
    db.add(user_community)
    db.commit()
    db.refresh(user_community)
    
    logger.info(f"Пользователь {user.vk_id} добавил сообщество {group_name} ({group_id})")
    
    return {
        "success": True,
        "group_id": group_id,
        "group_name": group_name,
        "message": f"Сообщество {group_name} успешно добавлено"
    }


@app.delete("/api/user/communities/{group_id}")
async def remove_user_community(
    request: Request,
    group_id: int,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Удаляет сообщество из списка пользователя."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    
    community = db.query(UserCommunity).filter(
        UserCommunity.user_id == user.id,
        UserCommunity.group_id == group_id
    ).first()
    
    if not community:
        raise HTTPException(status_code=404, detail="Сообщество не найдено")
    
    db.delete(community)
    db.commit()
    
    logger.info(f"Пользователь {user.vk_id} удалил сообщество {group_id}")
    
    return {
        "success": True,
        "group_id": group_id,
        "message": "Сообщество удалено"
    }


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
