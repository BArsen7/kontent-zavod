import logging
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional
import datetime
import threading
import secrets

import vk_api
import bcrypt as _bcrypt

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, Form
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db, init_db
from models import ContentPlanPeriod, Post, User, UserCommunity
from services.content_service import generate_weekly_pack
from services.content_manager_service import (
    get_or_create_content_plan_period,
    get_chat_history,
    generate_ai_response,
    get_user_community_info,
    initialize_chat_with_questions,
    generate_content_plan_from_chat,
    update_existing_plan,
    check_and_regenerate_expiring_plan,
)
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

# Хешмирование паролей напрямую через bcrypt
# (passlib несовместим с bcrypt >= 4.1: вызывает "password cannot be longer than 72 bytes"
#  и предупреждение "error reading bcrypt version")
_BCRYPT_MAX_BYTES = 72


def _password_bytes(password: str) -> bytes:
    """Возвращает байты пароля, обрезанные до 72 байт (лимит bcrypt)."""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Хеширует пароль."""
    return _bcrypt.hashpw(_password_bytes(password), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет соответствие пароля хешу."""
    try:
        return _bcrypt.checkpw(_password_bytes(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_session(user: User) -> str:
    """Создаёт сессию пользователя в памяти и возвращает session_id."""
    session_id = secrets.token_urlsafe(32)
    _session_store[session_id] = {
        "user_id": user.id,
        "created_at": datetime.datetime.now(),
    }
    return session_id


SESSION_COOKIE = "session_id"
SESSION_MAX_AGE = 86400 * 7  # 7 дней


def authenticated_redirect(response: RedirectResponse, session_id: str) -> RedirectResponse:
    """Устанавливает cookie с идентификатором сессии на ответ-редирект."""
    response.set_cookie(
        key=SESSION_COOKIE, value=session_id, httponly=True, max_age=SESSION_MAX_AGE
    )
    return response


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Получает текущего пользователя из сессии."""
    session_id = request.cookies.get(SESSION_COOKIE)
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


@app.get("/content-manager")
async def content_manager_page(request: Request, db: Session = Depends(get_db)):
    """Рендерит страницу контент-менеджера."""
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request=request,
        name="content_manager.html",
        context={"user": user}
    )


@app.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    """Рендерит страницу входа."""
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={}
    )


@app.post("/auth/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Обрабатывает вход пользователя по email и паролю."""
    user = db.query(User).filter(User.email == email).first()
    
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт деактивирован")
    
    # Создаем сессию и обновляем время последнего входа
    session_id = create_session(user)
    user.last_login = datetime.datetime.now()
    db.commit()

    logger.info(f"Пользователь {user.email} выполнил вход")

    # Перенаправляем на главную страницу
    return authenticated_redirect(RedirectResponse(url="/", status_code=302), session_id)


@app.post("/auth/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    first_name: str = Form(""),
    last_name: str = Form(""),
    db: Session = Depends(get_db)
):
    """Регистрирует нового пользователя."""
    # Проверяем, существует ли пользователь с таким email
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        logger.warning(f"Попытка регистрации существующего email: {email}")
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже существует")

    # Создаем нового пользователя с хешем пароля
    user = User(
        email=email,
        password_hash=hash_password(password),
        first_name=first_name,
        last_name=last_name,
        is_active=True,
    )
    db.add(user)

    try:
        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        logger.error(f"Критическая ошибка при регистрации пользователя {email}: {type(e).__name__} - {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка сервера при регистрации: {str(e)}")

    logger.info(f"Зарегистрирован новый пользователь: {email} (ID: {user.id})")

    # Создаем сессию и перенаправляем на главную страницу
    session_id = create_session(user)
    return authenticated_redirect(RedirectResponse(url="/", status_code=302), session_id)


@app.get("/logout")
async def logout(request: Request):
    """Выполняет выход пользователя."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id and session_id in _session_store:
        del _session_store[session_id]
    
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
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
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "photo": user.photo,
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


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Логирует 422 ошибки валидации с деталями (поле, тип ошибки, полученное значение)."""
    try:
        body_text = (await request.body()).decode('utf-8', errors='replace')
    except Exception:
        body_text = '<не удалось прочитать>'
    logger.error(
        f"[422] Валидация не пройдена для {request.method} {request.url.path}: "
        f"content-type={request.headers.get('content-type')}, "
        f"errors={exc.errors()}, raw_body={body_text!r}"
    )
    # Приводим detail к плоскому списку строк, чтобы клиент не получал "[object Object]"
    detail = [
        {
            "loc": [str(part) for part in err.get("loc", [])],
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


class AddCommunityRequest(BaseModel):
    """Схема JSON-тела запроса добавления сообщества."""
    group_id: int
    token: str


@app.post("/api/user/communities/add")
async def add_user_community(
    request: Request,
    payload: AddCommunityRequest,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Добавляет новое сообщество для текущего пользователя после проверки прав."""
    group_id = payload.group_id
    token = payload.token

    # Подробное логирование этапа добавления сообщества (для отладки 422/ошибок)
    logger.info(
        f"[communities/add] Запрос от пользователя (raw body будет залогирован ниже), "
        f"group_id={group_id}, token_length={len(token)}, token_prefix={token[:10]}..."
    )
    try:
        raw_body = await request.body()
        logger.info(
            f"[communities/add] content-type={request.headers.get('content-type')}, "
            f"raw_body={raw_body.decode('utf-8', errors='replace')!r}"
        )
    except Exception as log_err:
        logger.warning(f"[communities/add] Не удалось залогировать raw body: {log_err}")

    user = get_current_user(request, db)
    if not user:
        logger.warning(f"[communities/add] 401: пользователь не авторизован (group_id={group_id})")
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    logger.info(f"[communities/add] Пользователь авторизован: id={user.id}, email={user.email}")
    
    # Проверяем токен и получаем информацию о сообществе через VK API
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()

        # ВАЖНО (актуально для VK API 5.x): поля is_admin/admin_level объекта group
        # возвращаются только при вызове ОТ ИМЕНИ ПОЛЬЗОВАТЕЛЯ (user token со scope=groups).
        # Ключ доступа СООБЩЕСТВА (vk1.a....) не «знает», кто его создал, поэтому
        # groups.getById с таким токеном всегда возвращает is_admin=0 — на прошлом шаге
        # это давало ложное 403 "нет прав администратора".
        #
        # Для ключа сообщества правильный способ проверить права — метод
        # groups.getByID с параметром min_admin_level (доступен только community token):
        # вернутся только те сообщества, где создатель токена имеет админа не ниже
        # указанного уровня (1 — модератор, 2 — редактор, 3 — администратор).
        # Если список пуст — прав нет; если ошибка [15] — это user-токен, и тогда
        # используем is_admin из getById.
        group_info = vk.groups.getById(group_id=group_id)[0]
        group_name = group_info.get("name", f"Группа {group_id}")

        admin_confirmed = False
        try:
            # Проверяем право manage — оно необходимо боту (long poll, публикация от имени группы).
            # Вызов с min_admin_level=3 работает ТОЛЬКО с ключом сообщества.
            res = vk.groups.getByID(group_ids=[group_id], min_admin_level=3)
            items = res.get("items", []) if isinstance(res, dict) else []
            admin_confirmed = bool(items) and items[0].get("id") == int(group_id)
            logger.info(
                f"[communities/add] groups.getByID(min_admin_level=3) → items={items} "
                f"(ключ сообщества: права подтверждены, если id совпадает)"
            )
        except vk_api.exceptions.ApiError as e:
            if getattr(e, "code", None) == 15:
                # Это пользовательский токен — смотрим is_admin в ответе getById
                admin_level = group_info.get("is_admin", 0)
                admin_confirmed = bool(admin_level)
                logger.info(
                    f"[communities/add] min_admin_level недоступен (user-токен), "
                    f"is_admin={admin_level} (1=да, 0=нет)"
                )
            else:
                logger.warning(f"[communities/add] groups.getByID(min_admin_level): {e}")

        if not admin_confirmed:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Не удалось подтвердить права администратора в сообществе «{group_name}». "
                    "Используйте КЛЮЧ ДОСТУПА СООБЩЕСТВА, созданный администратором этого "
                    "сообщества: Сообщество → Управление → Работа с API → Ключи доступа → "
                    "«Создать ключ» (права: manage, wall, photos, messages), тип: "
                    "«Ключ сообщества». Пользовательские токены также поддерживаются, но "
                    "требуют разрешения «Данные сообществ» (scope groups)."
                )
            )

        # Дополнительно проверяем возможность управлять сообществом (нужно для бота):
        # getLongPollServer доступен только при праве manage у токена.
        try:
            vk.groups.getLongPollServer(group_id=group_id)
        except vk_api.exceptions.ApiError as lps_err:
            logger.warning(
                f"[communities/add] getLongPollServer не прошёл для группы {group_id}: {lps_err} "
                f"(скорее всего, у токена нет права manage)"
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    "Токен не имеет права «manage» (управление сообществом). "
                    "Создайте новый ключ доступа с максимальными правами: "
                    "Сообщество → Управление → Работа с API → Ключи доступа."
                )
            )

        logger.info(f"[communities/add] VK API: группа {group_name} ({group_id}), права администратора подтверждены")

    except HTTPException:
        # Уже сформированная ошибка (например 403 "нет прав") — пробрасываем как есть
        raise
    except vk_api.exceptions.ApiError as e:
        # vk_api бросает ApiError (а не AuthError) для ошибки [5] invalid access_token.
        # Формируем понятное пользователю сообщение вместо технического текста.
        error_msg = str(e)
        error_code = getattr(e, 'code', None)
        logger.warning(
            f"[communities/add] Ошибка VK API (code={error_code}): {error_msg} "
            f"(group_id={group_id}, token_prefix={token[:10]}...)"
        )
        if error_code == 5:
            if 'invalid app id' in error_msg or 'client_id' in error_msg:
                detail = (
                    "Токен недоступен для этого приложения. Создайте новый ключ сообщества: "
                    "Сообщество → Управление → Работа с API → Ключи доступа → «Создать ключ», "
                    "права: manage, photos, wall, messages, notifications."
                )
            else:
                detail = (
                    "Неверный или просроченный токен доступа. Проверьте, что вы скопировали "
                    "ключ целиком (он начинается с vk1.a....) и он создан для этого сообщества."
                )
        elif error_code == 15:
            detail = "Для этого метода недостаточно прав токена. Создайте ключ с расширенными правами."
        elif error_code in (213, 214):
            detail = f"Сообщество с ID {group_id} не найдено. Укажите числовой ID из настроек сообщества."
        else:
            detail = f"Ошибка VK API: {error_msg}"
        raise HTTPException(status_code=400, detail=detail)
    except vk_api.exceptions.AuthError:
        logger.warning(f"[communities/add] AuthError: неверный токен (group_id={group_id})")
        raise HTTPException(status_code=400, detail="Неверный токен доступа")
    except Exception as e:
        logger.exception(f"[communities/add] Неожиданная ошибка проверки токена VK: {e}")
        raise HTTPException(status_code=400, detail=f"Ошибка проверки токена: {str(e)}")
    
    # Проверяем, не добавлено ли уже это сообщество
    existing = db.query(UserCommunity).filter(
        UserCommunity.user_id == user.id,
        UserCommunity.group_id == group_id
    ).first()
    
    if existing:
        logger.info(f"[communities/add] Сообщество {group_id} уже добавлено для пользователя id={user.id}")
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
    
    logger.info(f"Пользователь {user.email} добавил сообщество {group_name} ({group_id})")
    
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
    
    logger.info(f"Пользователь {user.email} удалил сообщество {group_id}")
    
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


# --- Content Manager (Marketing Assistant) API Endpoints ---

@app.get("/api/content-manager/periods")
async def get_content_plan_periods(
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Возвращает список периодов контент-плана пользователя."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    
    periods = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.user_id == user.id
    ).order_by(ContentPlanPeriod.created_at.desc()).all()
    
    return {
        "periods": [
            {
                "id": p.id,
                "period_type": p.period_type,
                "start_date": p.start_date.isoformat(),
                "end_date": p.end_date.isoformat(),
                "status": p.status,
                "community_info": p.community_info,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                "posts_count": len(p.posts)
            }
            for p in periods
        ]
    }


@app.post("/api/content-manager/period/create")
async def create_content_plan_period(
    request: Request,
    period_type: str = "week",
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Создаёт новый период контент-плана."""
    user = require_auth(request, db)
    
    # Получаем информацию о сообществах
    community_info = get_user_community_info(db, user.id)
    
    period = get_or_create_content_plan_period(
        db=db,
        user_id=user.id,
        period_type=period_type,
        community_info=community_info
    )
    
    # Инициализируем чат с вопросами
    welcome_message = initialize_chat_with_questions(db, period.id)
    
    return {
        "success": True,
        "period": {
            "id": period.id,
            "period_type": period.period_type,
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "status": period.status
        },
        "welcome_message": welcome_message
    }


@app.get("/api/content-manager/chat/{period_id}")
async def get_chat_messages(
    period_id: int,
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Возвращает историю чата для периода контент-плана."""
    user = require_auth(request, db)
    
    # Проверяем что период принадлежит пользователю
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()
    
    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")
    
    messages = get_chat_history(db, period_id)
    
    return {
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat()
            }
            for m in messages
        ]
    }


@app.post("/api/content-manager/chat/{period_id}/send")
async def send_chat_message(
    period_id: int,
    request: Request,
    message: str,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Отправляет сообщение в чат с контент-менеджером и получает ответ ИИ."""
    user = require_auth(request, db)
    
    # Проверяем что период принадлежит пользователю
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()
    
    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")
    
    # Генерируем ответ ИИ
    ai_response_text = generate_ai_response(db, period_id, message, user.id)
    
    return {
        "success": True,
        "user_message": message,
        "ai_response": ai_response_text
    }


@app.post("/api/content-manager/period/{period_id}/generate-plan")
async def generate_plan_from_chat(
    period_id: int,
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Генерирует контент-план на основе диалога в чате."""
    user = require_auth(request, db)
    
    posts = generate_content_plan_from_chat(db, period_id, user.id)
    
    return {
        "success": True,
        "message": f"Создано {len(posts)} постов",
        "posts": posts
    }


@app.put("/api/content-manager/period/{period_id}/update")
async def update_content_plan(
    period_id: int,
    request: Request,
    modifications: Dict[str, Any],
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Обновляет существующий контент-план (кроме опубликованных постов)."""
    require_auth(request, db)
    
    period = update_existing_plan(db, period_id, modifications)
    
    return {
        "success": True,
        "period": {
            "id": period.id,
            "period_type": period.period_type,
            "status": period.status,
            "updated_at": period.updated_at.isoformat() if period.updated_at else None
        }
    }


@app.post("/api/content-manager/check-expiring")
async def check_expiring_plans(
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Проверяет истекающие планы и создаёт новые при необходимости."""
    user = require_auth(request, db)
    
    new_period = check_and_regenerate_expiring_plan(db, user.id)
    
    if new_period:
        return {
            "success": True,
            "message": "Создан новый период контент-плана",
            "new_period": {
                "id": new_period.id,
                "period_type": new_period.period_type,
                "start_date": new_period.start_date.isoformat(),
                "end_date": new_period.end_date.isoformat()
            }
        }
    else:
        return {
            "success": True,
            "message": "Активный план действителен"
        }


@app.get("/api/content-manager/period/{period_id}/posts")
async def get_period_posts(
    period_id: int,
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Возвращает посты для конкретного периода контент-плана."""
    user = require_auth(request, db)
    
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()
    
    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")
    
    posts = db.query(Post).filter(
        Post.content_plan_period_id == period_id
    ).order_by(Post.publish_at.asc()).all()
    
    return {
        "period": {
            "id": period.id,
            "period_type": period.period_type,
            "status": period.status
        },
        "posts": [
            {
                "id": p.id,
                "post_type": p.post_type,
                "topic": p.topic,
                "text_draft": p.text_draft,
                "text_final": p.text_final,
                "status": p.status,
                "publish_at": p.publish_at.isoformat() if p.publish_at else None,
                "published_at": p.published_at.isoformat() if p.published_at else None
            }
            for p in posts
        ]
    }
