import asyncio  # FIX: Event loop unblocked — вынос синхронных операций в поток
import logging
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional
import datetime
import threading
import secrets

import vk_api
import bcrypt as _bcrypt

from sqlalchemy import func as sa_func, or_  # FIX: or_ — фильтр своих постов в /api/posts
from sqlalchemy.exc import OperationalError  # FIX: устойчивое чтение SystemSetting (fallback при битой таблице)

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, Form, Query
from fastapi.concurrency import run_in_threadpool  # FIX: Event loop unblocked
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
# FIX: Tailwind Play CDN -> локальный /static/styles.css (раздача статики)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db, init_db
from services.gigachat_client import is_gigachat_configured
# FIX: Ненадёжные сессии в памяти -> персистентная модель Session в БД
from models import (
    ChatMessage,
    ContentPlanPeriod,
    Post,
    Session as DbSession,
    SystemSetting,
    User,
    UserCommunity,
)
from services.content_service import generate_weekly_pack, PACK_PERIODS
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
from vk_errors import create_vk_session, describe_api_error, positive_group_id
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
    
    # Инициализируем синглтон клиента GigaChat (requests; токен OAuth будет
    # получен лениво при первом запросе и закэширован внутри экземпляра)
    from services.gigachat_client import get_gigachat_client, is_gigachat_configured

    app.state.gigachat_client = get_gigachat_client()
    if is_gigachat_configured():
        logger.info("GigaChat credentials найдены — облачная генерация доступна")
    else:
        logger.info("GigaChat credentials не заданы — используются Ollama/Kandinsky из .env")
    
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

# FIX: Tailwind Play CDN -> локальный /static/styles.css — раздача статики
app.mount("/static", StaticFiles(directory="web/static"), name="static")
# FIX (UX): раздача сгенерированных изображений из data/media (Post.image_url -> /media/...)
from pathlib import Path as _Path
_Path("data/media").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory="data/media"), name="media")

# FIX: Ненадёжные сессии в памяти — глобальный словарь _session_store удалён.
# Сессии хранятся в БД (модель models.Session), что гарантирует их сохранение
# после перезапуска процесса.

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


def create_session(user: User, db: Session) -> str:
    """Создаёт сессию пользователя в БД и возвращает session_id.

    FIX: Ненадёжные сессии в памяти — запись хранится в таблице sessions,
    поэтому активные сессии переживают перезапуск приложения.
    """
    session_id = secrets.token_urlsafe(32)
    db.add(DbSession(
        token=session_id,
        user_id=user.id,
        expires_at=datetime.datetime.now() + datetime.timedelta(seconds=SESSION_MAX_AGE),
    ))
    db.commit()
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
    """Получает текущего пользователя из сессии.

    FIX: Ненадёжные сессии в памяти — проверка токена выполняется по таблице
    sessions в БД (с учётом срока жизни expires_at).
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None

    db_session = (
        db.query(DbSession)
        .filter(DbSession.token == session_id)
        .filter(DbSession.expires_at > datetime.datetime.now())
        .first()
    )
    if not db_session:
        return None

    user_id = db_session.user_id
    if not user_id:
        return None

    return db.query(User).filter(User.id == user_id).first()


def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    """Требует аутентификацию пользователя."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    if getattr(user, "is_blocked", False):
        # UX: заблокированный аккаунт — редирект на страницу с причиной
        return RedirectResponse(url="/account-blocked", status_code=302)  # type: ignore[return-value]
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт деактивирован")
    return user


def get_current_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Зависимость FastAPI: требует авторизованного пользователя с правами администратора.

    Используется для защиты эндпоинтов панели администратора.
    Права выдаются скриптом scripts/make_admin.py (поле User.is_admin).
    Заблокированный администратор перенаправляется на /account-blocked.
    """
    user = get_current_user(request, db)
    if not user or not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещен. Требуются права администратора.",
        )
    if getattr(user, "is_blocked", False):
        raise HTTPException(
            status_code=307,
            detail="account-blocked",
            headers={"Location": "/account-blocked"},
        )
    return user


# --- Admin Panel Routes (Часть 2: backend-логика панели администратора) ---
# Все роуты ниже защищены зависимостью Depends(get_current_admin).
# Шаблоны admin_*.html будут созданы в Части 3; backend готов и работает уже сейчас.

@app.get("/admin")
def admin_dashboard(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Панель администратора: сводная статистика."""
    total_users = db.query(sa_func.count(User.id)).scalar() or 0
    total_communities = db.query(sa_func.count(UserCommunity.id)).scalar() or 0
    total_posts = db.query(sa_func.count(Post.id)).scalar() or 0
    logger.info(
        f"[admin] {admin.email}: открыл дашборд "
        f"(users={total_users}, communities={total_communities}, posts={total_posts})"
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "user": admin,
            "admin": admin,
            "stats": {
                "total_users": total_users,
                "total_communities": total_communities,
                "total_posts": total_posts,
            },
        },
    )


@app.get("/admin/users")
def admin_users_page(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Панель администратора: список всех пользователей."""
    users = db.query(User).order_by(User.id.asc()).all()
    logger.info(f"[admin] {admin.email}: открыл список пользователей (count={len(users)})")
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"user": admin, "admin": admin, "users": users},
    )


@app.post("/admin/users/{user_id}/toggle_admin")
def admin_toggle_user_admin(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Переключает право is_admin у пользователя и возвращает на /admin/users."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        logger.warning(f"[admin] {admin.email}: пользователь id={user_id} не найден (toggle_admin)")
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target_user.is_admin = not target_user.is_admin
    db.commit()
    db.refresh(target_user)
    logger.info(
        f"[admin] {admin.email}: изменил права пользователя id={target_user.id} "
        f"({target_user.email}): is_admin={target_user.is_admin}"
    )
    return RedirectResponse(url="/admin/users", status_code=302)


@app.get("/admin/communities")
def admin_communities_page(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Панель администратора: все сообщества с JOIN владельцев (UserCommunity + User)."""
    rows = (
        db.query(UserCommunity, User)
        .join(User, UserCommunity.user_id == User.id)
        .order_by(UserCommunity.id.asc())
        .all()
    )
    communities = [
        {
            "id": uc.id,
            "group_id": uc.group_id,
            "group_name": uc.group_name or f"Группа {uc.group_id}",
            "owner_email": owner.email,
            "created_at": uc.created_at,
            "is_blocked": getattr(uc, "is_blocked", False),
            "blocked_reason": uc.blocked_reason,
            "owner_is_blocked": getattr(owner, "is_blocked", False),
        }
        for uc, owner in rows
    ]
    logger.info(
        f"[admin] {admin.email}: открыл список сообществ (count={len(communities)})"
    )
    return templates.TemplateResponse(
        request=request,
        name="admin_communities.html",
        context={"user": admin, "admin": admin, "communities": communities},
    )


# --- Admin: управление пользователями (блокировка/разблокировка/удаление/добавление) ---

@app.post("/admin/users/{user_id}/block")
def admin_block_user(
    user_id: int,
    blocked_reason: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Блокирует пользователя с указанием причины; активные сессии удаляются."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target_user.id == admin.id:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")

    target_user.is_blocked = True
    target_user.blocked_reason = blocked_reason.strip() or "Причина не указана"
    target_user.blocked_at = datetime.datetime.now()
    # Разрываем активные сессии заблокированного пользователя
    db.query(DbSession).filter(DbSession.user_id == target_user.id).delete()
    db.commit()
    logger.info(
        f"[admin] {admin.email}: заблокировал пользователя id={target_user.id} "
        f"({target_user.email}): причина='{target_user.blocked_reason}'"
    )
    return RedirectResponse(url="/admin/users", status_code=302)


@app.post("/admin/users/{user_id}/unblock")
def admin_unblock_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Снимает блокировку с пользователя."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target_user.is_blocked = False
    target_user.blocked_reason = None
    target_user.blocked_at = None
    db.commit()
    logger.info(
        f"[admin] {admin.email}: разблокировал пользователя id={target_user.id} "
        f"({target_user.email})"
    )
    return RedirectResponse(url="/admin/users", status_code=302)


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Удаляет пользователя; связанные данные удаляются каскадно (SQLAlchemy cascade)."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target_user.id == admin.id:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")

    email = target_user.email
    # Каскадное удаление связей через relationship(cascade="all, delete-orphan"):
    # communities (UserCommunity) — по relationship User.communities.
    db.delete(target_user)
    db.flush()
    # Сессии и периоды контент-планов удаляем явно (FK без relationship-cascade).
    db.query(DbSession).filter(DbSession.user_id == user_id).delete()
    period_ids = [
        pid for (pid,) in db.query(ContentPlanPeriod.id)
        .filter(ContentPlanPeriod.user_id == user_id).all()
    ]
    if period_ids:
        db.query(Post).filter(Post.content_plan_period_id.in_(period_ids)).update(
            {Post.content_plan_period_id: None}, synchronize_session=False
        )
        db.query(ContentPlanPeriod).filter(ContentPlanPeriod.id.in_(period_ids)).delete(
            synchronize_session=False
        )
    db.commit()
    logger.info(f"[admin] {admin.email}: удалил пользователя id={user_id} ({email})")
    return RedirectResponse(url="/admin/users", status_code=302)


@app.post("/admin/users/add")
def admin_add_user(
    email: str = Form(...),
    password: str = Form(...),
    first_name: str = Form(""),
    last_name: str = Form(""),
    make_admin: str = Form("off"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Создаёт нового пользователя из админ-панели (пароль хешируется bcrypt)."""
    email = email.strip().lower()
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email и пароль обязательны")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже существует")

    user = User(
        email=email,
        password_hash=hash_password(password),  # bcrypt-хеширование
        first_name=first_name.strip() or None,
        last_name=last_name.strip() or None,
        is_active=True,
        is_admin=(make_admin == "on"),
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        logger.error(f"[admin] {admin.email}: ошибка создания пользователя {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка сервера при создании: {str(e)}")
    logger.info(f"[admin] {admin.email}: создал пользователя id={user.id} ({email})")
    return RedirectResponse(url="/admin/users", status_code=302)


# --- Admin: управление сообществами (блокировка/разблокировка/удаление/добавление) ---

@app.post("/admin/communities/{comm_id}/block")
def admin_block_community(
    comm_id: int,
    blocked_reason: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Блокирует сообщество (планировщик перестанет публиковать его посты)."""
    uc = db.query(UserCommunity).filter(UserCommunity.id == comm_id).first()
    if not uc:
        raise HTTPException(status_code=404, detail="Сообщество не найдено")

    uc.is_blocked = True
    uc.blocked_reason = blocked_reason.strip() or "Причина не указана"
    db.commit()
    logger.info(
        f"[admin] {admin.email}: заблокировал сообщество id={uc.id} "
        f"(group_id={uc.group_id}): причина='{uc.blocked_reason}'"
    )
    return RedirectResponse(url="/admin/communities", status_code=302)


@app.post("/admin/communities/{comm_id}/unblock")
def admin_unblock_community(
    comm_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Снимает блокировку с сообщества."""
    uc = db.query(UserCommunity).filter(UserCommunity.id == comm_id).first()
    if not uc:
        raise HTTPException(status_code=404, detail="Сообщество не найдено")

    uc.is_blocked = False
    uc.blocked_reason = None
    db.commit()
    logger.info(
        f"[admin] {admin.email}: разблокировал сообщество id={uc.id} (group_id={uc.group_id})"
    )
    return RedirectResponse(url="/admin/communities", status_code=302)


@app.post("/admin/communities/{comm_id}/delete")
def admin_delete_community(
    comm_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Удаляет связь пользователя с сообществом из БД."""
    uc = db.query(UserCommunity).filter(UserCommunity.id == comm_id).first()
    if not uc:
        raise HTTPException(status_code=404, detail="Сообщество не найдено")

    group_id = uc.group_id
    db.delete(uc)
    db.commit()
    logger.info(
        f"[admin] {admin.email}: удалил сообщество id={comm_id} (group_id={group_id})"
    )
    return RedirectResponse(url="/admin/communities", status_code=302)


@app.post("/admin/communities/add")
def admin_add_community(
    owner_email: str = Form(...),
    group_id: int = Form(...),
    group_token: str = Form(...),
    group_name: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Привязывает существующее VK-сообщество к выбранному пользователю."""
    owner = db.query(User).filter(User.email == owner_email.strip().lower()).first()
    if not owner:
        # Пробуем точное совпадение email (регистр мог быть сохранён при регистрации)
        owner = db.query(User).filter(User.email == owner_email.strip()).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Владелец с таким email не найден")

    exists = (
        db.query(UserCommunity)
        .filter(UserCommunity.user_id == owner.id, UserCommunity.group_id == group_id)
        .first()
    )
    if exists:
        raise HTTPException(
            status_code=400,
            detail="Это сообщество уже привязано к данному пользователю",
        )

    uc = UserCommunity(
        user_id=owner.id,
        group_id=group_id,
        group_name=group_name.strip() or f"Группа {group_id}",
        group_token=group_token.strip(),
        is_admin=True,
        can_post=True,
    )
    db.add(uc)
    db.commit()
    db.refresh(uc)
    logger.info(
        f"[admin] {admin.email}: добавил сообщество id={uc.id} (group_id={group_id}) "
        f"для пользователя {owner.email}"
    )
    return RedirectResponse(url="/admin/communities", status_code=302)


# --- Admin: системные настройки AI-моделей (SystemSetting) ---

# Ключи настроек и их fallback-значения из config.py / дефолты генераторов.
# FIX: добавлена поддержка облачных провайдеров (OpenAI-совместимые API:
# OpenRouter, OpenAI, DeepSeek, GigaChat, YandexGPT и т.д.) — настраиваются
# динамически через SystemSetting без перезапуска приложения.
AI_SETTING_KEYS = {
    # Локальный Ollama
    "ollama_base_url": lambda: settings.ollama_url,
    "default_text_model": lambda: "qwen2.5:14b",
    "default_image_model": lambda: "",
    "generation_temperature": lambda: "0.7",
    # Облачные модели (OpenAI-совместимый Chat Completions API)
    "ai_provider": lambda: "ollama",  # ollama | cloud | gigachat
    "cloud_api_base_url": lambda: "",  # например https://openrouter.ai/api/v1
    "cloud_api_key": lambda: settings.gigachat_key or "",
    "cloud_text_model": lambda: "",  # например deepseek/deepseek-chat
    # GigaChat Premium (полноценная интеграция: OAuth + /chat/completions)
    # Значения по умолчанию берутся из config.py (.env), в БД можно переопределить
    "gigachat_text_model": lambda: settings.gigachat_text_model,
    "gigachat_image_model": lambda: settings.gigachat_image_model,
    # kandinsky | gigachat (по умолчанию — нативная генерация GigaChat Premium)
    "image_provider": lambda: "gigachat" if is_gigachat_configured() else "kandinsky",
}


def get_system_setting(db: Session, key: str) -> str:
    """Читает настройку из SystemSetting; при отсутствии — fallback из config.py.

    FIX: если таблица system_settings отсутствует/недоступна в старой БД
    (OperationalError «no such table»), не роняем роут (/admin/settings,
    сохранение настроек, тесты подключения), а отдаём fallback-значение.
    init_db() пересоздаст таблицу при следующем старте приложения.
    """
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is not None:
            return row.value
    except OperationalError as e:
        db.rollback()  # сессия после ошибки БД должна быть откачена
        logger.warning(
            f"Таблица system_settings недоступна ({e}) — используется fallback из config.py"
        )
    fallback = AI_SETTING_KEYS.get(key)
    return fallback() if fallback else ""


def save_system_settings(db: Session, payload: Dict[str, str]) -> None:
    """Создаёт или обновляет записи SystemSetting (без перезапуска приложения)."""
    for key, value in payload.items():
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row:
            row.value = value
            row.updated_at = datetime.datetime.utcnow()
        else:
            db.add(SystemSetting(key=key, value=value))
    db.commit()


def build_ai_config(db: Session) -> Dict[str, Any]:
    """Собирает актуальную конфигурацию AI из SystemSetting (fallback — config.py).

    Используется админ-роутами и может использоваться генераторами для выбора
    провайдера (локальный Ollama или облачный OpenAI-совместимый API).
    """
    provider = get_system_setting(db, "ai_provider") or "ollama"
    try:
        temperature = float(get_system_setting(db, "generation_temperature"))
    except ValueError:
        temperature = 0.7
    text_model = get_system_setting(db, "default_text_model")
    if provider == "cloud":
        text_model = get_system_setting(db, "cloud_text_model") or text_model
    if provider == "gigachat":
        text_model = get_system_setting(db, "gigachat_text_model") or text_model
    return {
        "provider": provider if provider in ("ollama", "cloud", "gigachat") else "ollama",
        "ollama_base_url": get_system_setting(db, "ollama_base_url"),
        "default_text_model": text_model,
        "default_image_model": get_system_setting(db, "default_image_model"),
        "generation_temperature": temperature,
        "cloud_api_base_url": get_system_setting(db, "cloud_api_base_url"),
        "cloud_api_key": get_system_setting(db, "cloud_api_key"),
        "cloud_text_model": get_system_setting(db, "cloud_text_model"),
        # GigaChat Premium
        "gigachat_text_model": get_system_setting(db, "gigachat_text_model"),
        "gigachat_image_model": get_system_setting(db, "gigachat_image_model"),
        "gigachat_image_save_dir": get_system_setting(db, "gigachat_image_save_dir"),
        "image_provider": get_system_setting(db, "image_provider"),
        "gigachat_configured": is_gigachat_configured(),
    }


@app.get("/admin/settings")
def admin_settings_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Страница настроек AI: значения из SystemSetting или fallback из config.py."""
    values = {key: get_system_setting(db, key) for key in AI_SETTING_KEYS}

    # Маскируем client_id для отображения read-only в админке
    cid = settings.effective_gigachat_client_id
    if cid:
        masked = cid[:4] + "…" + cid[-4:] if len(cid) > 8 else "•" * len(cid)
    else:
        masked = "(не задан в .env)"

    return templates.TemplateResponse(
        request=request,
        name="admin_settings.html",
        context={
            "user": admin,
            "admin": admin,
            "values": values,
            "gigachat_configured": is_gigachat_configured(),
            "gigachat_client_id_masked": masked,
        },
    )


@app.post("/admin/settings")
def admin_settings_update(
    ai_provider: str = Form("ollama"),
    ollama_base_url: str = Form(""),
    default_text_model: str = Form(""),
    default_image_model: str = Form(""),
    generation_temperature: str = Form("0.7"),
    cloud_api_base_url: str = Form(""),
    cloud_api_key: str = Form(""),
    cloud_text_model: str = Form(""),
    gigachat_text_model: str = Form(""),
    gigachat_image_model: str = Form(""),
    gigachat_image_save_dir: str = Form(""),
    image_provider: str = Form("gigachat"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Создаёт или обновляет записи SystemSetting (без перезапуска приложения)."""
    if ai_provider not in ("ollama", "cloud", "gigachat"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Неизвестный провайдер AI: выберите 'ollama', 'cloud' или 'gigachat'"
            ),
        )
    if image_provider not in ("kandinsky", "gigachat"):
        raise HTTPException(
            status_code=400,
            detail="Неизвестный провайдер изображений: 'kandinsky' или 'gigachat'",
        )

    payload = {
        "ai_provider": ai_provider,
        "ollama_base_url": ollama_base_url.strip(),
        "default_text_model": default_text_model.strip(),
        "default_image_model": default_image_model.strip(),
        "generation_temperature": generation_temperature.strip() or "0.7",
        "cloud_api_base_url": cloud_api_base_url.strip().rstrip("/"),
        "cloud_api_key": cloud_api_key.strip(),
        "cloud_text_model": cloud_text_model.strip(),
        "gigachat_text_model": gigachat_text_model.strip(),
        "gigachat_image_model": gigachat_image_model.strip(),
        "gigachat_image_save_dir": gigachat_image_save_dir.strip() or "data/media",
        "image_provider": image_provider,
    }
    # Валидация температуры
    try:
        t = float(payload["generation_temperature"])
        if not (0.0 <= t <= 2.0):
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Температура генерации должна быть числом в диапазоне 0.0–2.0",
        )
    # Валидация облачной конфигурации: без URL/ключа/модели облако работать не будет
    if payload["ai_provider"] == "cloud":
        if not payload["cloud_api_base_url"] or not payload["cloud_text_model"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Для облачного провайдера обязательны: базовый URL API "
                    "и название модели"
                ),
            )
    # GigaChat требует креды из .env (client_id/client_secret)
    if (
        payload["ai_provider"] == "gigachat" or payload["image_provider"] == "gigachat"
    ) and not is_gigachat_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "GigaChat выбран провайдером, но GIGACHAT_CLIENT_ID / "
                "GIGACHAT_CLIENT_SECRET не заданы в .env — добавьте их и "
                "перезапустите приложение"
            ),
        )

    save_system_settings(db, payload)

    # Пересоздаём синглтон клиента GigaChat, чтобы изменения (в т.ч. динамически
    # обновлённые значения config.py и смена кредов в .env) применились без перезапуска
    from services import gigachat_client as gc

    gc.reset_gigachat_client()

    logger.info(
        f"[admin] {admin.email}: обновил системные настройки AI "
        f"(текст: {payload['ai_provider']}, изображения: {payload['image_provider']})"
    )
    return RedirectResponse(url="/admin/settings", status_code=302)


@app.post("/admin/settings/test-cloud")
async def admin_settings_test_cloud(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Проверяет подключение к облачной модели: POST {base_url}/chat/completions.

    Для провайдера 'gigachat' использует полноценный GigaChatClient
    (OAuth с кэшированием токена + повтор при 401). Синхронные сетевые
    вызовы вынесены в поток (asyncio.to_thread), чтобы не блокировать event loop.
    """
    cfg = build_ai_config(db)

    # --- Тест GigaChat Premium через асинхронный клиент ---
    if cfg["provider"] == "gigachat":
        if not is_gigachat_configured():
            return JSONResponse(
                {
                    "ok": False,
                    "detail": (
                        "GigaChat credentials не заданы в .env "
                        "(GIGACHAT_CLIENT_ID / GIGACHAT_CLIENT_SECRET)"
                    ),
                },
                status_code=400,
            )
        from services.gigachat_client import get_gigachat_client

        client = get_gigachat_client()
        try:
            reply = await asyncio.to_thread(
                client.generate_text,
                prompt="ping",
                system_prompt="",
                temperature=cfg["generation_temperature"],
                max_tokens=5,
                model=(cfg["gigachat_text_model"] or "").strip() or None,
            )
            logger.info(f"[admin] {admin.email}: тест GigaChat успешен")
            return JSONResponse({"ok": True, "reply": (reply or "(пустой ответ)")[:200]})
        except Exception as e:  # noqa: BLE001 — показываем админу суть ошибки
            logger.warning(f"[admin] {admin.email}: тест GigaChat не удался: {e}")
            return JSONResponse({"ok": False, "detail": str(e)[:300]}, status_code=400)

    # --- Тест OpenAI-совместимого облака ---
    if cfg["provider"] != "cloud":
        return JSONResponse(
            {"ok": False, "detail": "Текущий провайдер — не облачный (cloud/gigachat)."},
            status_code=400,
        )
    if not cfg["cloud_api_base_url"] or not cfg["cloud_text_model"]:
        return JSONResponse(
            {"ok": False, "detail": "Не заданы base URL или модель облачного API."},
            status_code=400,
        )

    import requests as _requests

    headers = {"Content-Type": "application/json"}
    if cfg["cloud_api_key"]:
        headers["Authorization"] = f"Bearer {cfg['cloud_api_key']}"
    url = f"{cfg['cloud_api_base_url']}/chat/completions"
    body = {
        "model": cfg["cloud_text_model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "temperature": cfg["generation_temperature"],
    }
    try:
        resp = _requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        reply = (
            data.get("choices", [{}])[0].get("message", {}).get("content", "")
            or "(пустой ответ)"
        )
        logger.info(f"[admin] {admin.email}: тест облачной модели успешен")
        return JSONResponse({"ok": True, "reply": reply[:200]})
    except Exception as e:  # noqa: BLE001 — показываем админу суть ошибки
        logger.warning(f"[admin] {admin.email}: тест облачной моделине удался: {e}")
        return JSONResponse({"ok": False, "detail": str(e)[:300]}, status_code=400)


@app.post("/admin/settings/test-gigachat-image")
async def admin_settings_test_gigachat_image(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Тест нативной генерации изображения через GigaChat Premium.

    Делает реальный (дорогой по времени) запрос: chat/completions c
    function_call="auto" (таймаут 90 сек) + скачивание /files/{id}/content
    (30 сек). Синхронные сетевые вызовы вынесены в поток через
    asyncio.to_thread — event loop FastAPI не блокируется.
    """
    if not is_gigachat_configured():
        return JSONResponse(
            {
                "ok": False,
                "detail": (
                    "GigaChat credentials не заданы в .env "
                    "(GIGACHAT_CLIENT_ID / GIGACHAT_CLIENT_SECRET)"
                ),
            },
            status_code=400,
        )

    cfg = build_ai_config(db)
    save_dir = (cfg.get("gigachat_image_save_dir") or "").strip() or "data/media"

    from generators.image_generator import generate_image_gigachat

    try:
        path = await asyncio.to_thread(
            generate_image_gigachat,
            "Уютная кухня, печенье с цветным рельефным узором, мягкий свет",
            save_dir,
            cfg.get("gigachat_text_model", ""),
        )
        logger.info(f"[admin] {admin.email}: тест генерации изображения GigaChat успешен: {path}")
        return JSONResponse({"ok": True, "path": path})
    except Exception as e:  # noqa: BLE001 — показываем админу суть ошибки
        logger.warning(f"[admin] {admin.email}: тест изображения GigaChat не удался: {e}")
        return JSONResponse({"ok": False, "detail": str(e)[:300]}, status_code=400)


# --- Публичная страница заблокированного аккаунта ---

@app.get("/account-blocked")
def account_blocked_page(request: Request, db: Session = Depends(get_db)):
    """Рендерит страницу 'Аккаунт заблокирован' с причиной и датой блокировки."""
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request=request,
        name="account_blocked.html",
        context={"user": user},
    )


# --- Web Routes ---

@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    """Рендерит главную страницу.

    FIX: Двойная загрузка — запрос к БД за постами (db.query(Post).all()) удалён;
    посты загружаются фронтендом один раз через /api/posts с пагинацией.
    # FIX: Event loop unblocked — синхронный роут без async, FastAPI выносит его в threadpool.
    """
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"user": user}
    )


@app.get("/content-manager")
def content_manager_page(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)):
    """Рендерит страницу контент-менеджера."""
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request=request,
        name="content_manager.html",
        context={"user": user}
    )


@app.get("/login")
def login_page(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)):
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
def login(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
    session_id = create_session(user, db)  # FIX: сессии хранятся в БД, а не в памяти
    user.last_login = datetime.datetime.now()
    db.commit()

    logger.info(f"Пользователь {user.email} выполнил вход")

    # Перенаправляем на главную страницу
    return authenticated_redirect(RedirectResponse(url="/", status_code=302), session_id)


@app.post("/auth/register")
def register(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
    session_id = create_session(user, db)  # FIX: сессии хранятся в БД, а не в памяти
    return authenticated_redirect(RedirectResponse(url="/", status_code=302), session_id)


@app.get("/logout")
def logout(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)):
    """Выполняет выход пользователя."""
    # FIX: Ненадёжные сессии в памяти — удаляем запись сессии из БД
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        db_session = db.query(DbSession).filter(DbSession.token == session_id).first()
        if db_session:
            db.delete(db_session)
            db.commit()

    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- API Endpoints ---

@app.get("/api/posts")
def get_posts(
    request: Request,
    skip: int = Query(0, ge=0),              # FIX: Пагинация — смещение
    limit: int = Query(20, ge=1, le=100),    # FIX: Пагинация — размер страницы
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),  # FIX: Безопасность — эндпоинт требует авторизацию
) -> List[Dict[str, Any]]:
    """Возвращает список постов текущего пользователя из БД (с пагинацией).

    FIX: Безопасность — доступ только для авторизованных пользователей;
    возвращаются только посты, принадлежащие текущему пользователю
    (через его периоды контент-плана ContentPlanPeriod.user_id).
    # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI.
    """
    posts = (
        db.query(Post)
        .outerjoin(ContentPlanPeriod, Post.content_plan_period_id == ContentPlanPeriod.id)
        # FIX: Безопасность — фильтр по владельцу поста
        .filter(or_(ContentPlanPeriod.user_id == current_user.id, Post.status == "published"))
        .order_by(Post.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    
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


def _to_web_media_path(image_path):
    """FIX (UX): приводит локальный путь к изображению (data/media/xxx.jpg)
    к веб-пути /media/xxx.jpg — data/media смонтирован как StaticFiles,
    иначе картинки сгенерированных постов не отображаются в UI."""
    if not image_path:
        return image_path
    p = str(image_path).replace("\\", "/")
    if p.startswith("http://") or p.startswith("https://") or p.startswith("/"):
        return p
    marker = "data/media/"
    idx = p.find(marker)
    if idx != -1:
        return "/media/" + p[idx + len(marker):]
    return "/media/" + p.lstrip("./")


# --- Фоновая генерация пакетов: трекинг статуса для UI ---
# task_id -> {status, period_type, total, created, failed, errors, started_at, finished_at}
_PACK_TASKS: Dict[str, Dict[str, Any]] = {}
_PACK_TASKS_LOCK = threading.Lock()
_PACK_TASK_TTL_SECONDS = 60 * 60  # храним статус час после завершения


def _pack_task_update(task_id: str, **changes) -> None:
    """Потокобезопасно обновляет запись о фоновой задаче генерации."""
    with _PACK_TASKS_LOCK:
        task = _PACK_TASKS.get(task_id)
        if task is not None:
            task.update(changes)


def _pack_task_cleanup() -> None:
    """Удаляет старые завершённые задачи (защита от утечки памяти)."""
    cutoff = datetime.datetime.now() - datetime.timedelta(seconds=_PACK_TASK_TTL_SECONDS)
    with _PACK_TASKS_LOCK:
        stale = [
            tid for tid, t in _PACK_TASKS.items()
            if t.get("finished_at") and t["finished_at"] < cutoff
        ]
        for tid in stale:
            _PACK_TASKS.pop(tid, None)


@app.post("/api/generate/{period_type}")
def generate_pack(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    period_type: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Запускает генерацию пакета постов в фоновом режиме.

    period_type: 'week' (7 постов), 'two_weeks' (14) или 'month' (30).
    Обратная совместимость: '/api/generate/weekly' == '/api/generate/week'.
    Возврашает немедленный ответ с task_id; прогресс доступен через
    GET /api/generate/status/{task_id}.
    """
    # FIX: Безопасность — генерация доступна только авторизованным пользователям
    current_user = require_auth(request, db)

    if period_type == "weekly":  # обратная совместимость со старым URL
        period_type = "week"
    if period_type not in PACK_PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный тип периода '{period_type}'. Доступно: week, two_weeks, month"
        )

    expected_count = len(PACK_PERIODS[period_type]["post_types"])
    logger.info(
        f"Получен запрос на генерацию пакета ({period_type}, {expected_count} постов) "
        f"пользователем id={current_user.id}"
    )

    _pack_task_cleanup()
    task_id = secrets.token_hex(8)
    with _PACK_TASKS_LOCK:
        _PACK_TASKS[task_id] = {
            "status": "running",
            "period_type": period_type,
            "user_id": current_user.id,  # FIX (UX): для восстановления статуса в UI (/api/generate/active)
            "total": expected_count,
            "created": 0,
            "failed": 0,
            "errors": [],
            "started_at": datetime.datetime.now().isoformat(),
            "finished_at": None,
        }

    def run_generation(pt: str = period_type):
        try:
            # Создаём новую сессию для фонового потока
            from database import SessionLocal
            db_session = SessionLocal()
            try:
                result = generate_weekly_pack(
                    niche="3d_cookies",
                    db=db_session,
                    period_type=pt,
                    user_id=current_user.id,  # FIX: привязка постов к пользователю
                    progress_cb=_on_progress,
                )
                created = len(result)
                failed = expected_count - created
                logger.info(f"Генерация завершена ({pt}). Создано постов: {created}")
                _pack_task_update(
                    task_id,
                    status="completed",
                    created=created,
                    failed=failed,
                    finished_at=datetime.datetime.now().isoformat(),
                )
            finally:
                db_session.close()
        except Exception as e:
            logger.error(f"Ошибка в фоновой генерации: {e}")
            _pack_task_update(
                task_id,
                status="failed",
                errors=[str(e)[:300]],
                finished_at=datetime.datetime.now().isoformat(),
            )

    # FIX (UX): раньше использовался BackgroundTasks — его задачи выполняются
    # СИНХРОННО после отправки HTTP-ответа и до обработки следующего запроса.
    # Генерация пака идёт минутами, из-за чего все остальные запросы к сайту
    # («зависший» интерфейс, пустой список постов, таймауты fetch в браузере)
    # блокировались, и статус генерации на сайте не отображался.
    # Теперь задача запускается в отдельном потоке — ответ возвращается сразу,
    # а прогресс доступен через GET /api/generate/status/{task_id}.
    thread = threading.Thread(
        target=run_generation, name=f"pack-gen-{task_id}", daemon=True
    )
    thread.start()

    return {
        "status": "started",
        "task_id": task_id,
        "message": "Генерация запущена в фоновом режиме",
        "period_type": period_type,
        "count": expected_count
    }


@app.get("/api/generate/status/{task_id}")
def get_generate_status(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Возвращает статус фоновой генерации пакета (для отображения прогресса в UI)."""
    require_auth(request, db)  # FIX: Безопасность — статус доступен только авторизованным
    with _PACK_TASKS_LOCK:
        task = _PACK_TASKS.get(task_id)
        if task is None:
            return {"status": "unknown", "task_id": task_id}
        return dict(task)


@app.get("/api/generate/active")
def get_active_generate_task(
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Возвращает активную/последнюю задачу генерации текущего пользователя.

    FIX (UX): страница могла быть перезагружена (или открыта в другой вкладке)
    во время фоновой генерации — task_id терялся, и пользователь не видел ни
    статуса, ни результата. Этот эндпоинт позволяет UI восстановить прогресс
    при загрузке страницы.
    """
    user = require_auth(request, db)
    with _PACK_TASKS_LOCK:
        candidates = [
            (tid, t) for tid, t in _PACK_TASKS.items()
            if t.get("user_id") == user.id
        ]
    if not candidates:
        return {"status": "none"}
    # самая свежая задача по started_at
    tid, task = max(candidates, key=lambda kv: kv[1].get("started_at") or "")
    return {"task_id": tid, **task}


@app.delete("/api/posts/{post_id}")
def delete_post(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    post_id: int,
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Удаляет пост (черновик/одобренный). Опубликованные посты удалять нельзя."""
    require_auth(request, db)

    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Пост не найден")

    if post.status == "published":
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить опубликованный пост"
        )

    db.delete(post)
    db.commit()
    logger.info(f"[posts/delete] Удалён пост id={post_id} (статус: {post.status})")

    return {"success": True, "deleted_post_id": post_id}


@app.post("/api/posts/{post_id}/approve")
def approve_post(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    post_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
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
def publish_vk(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
def get_current_user_info(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
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
def get_user_communities(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
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
    # FIX: msg может быть bytes (например "JSON decode error") — приводим к str.
    detail = [
        f"{'.'.join(str(part) for part in err.get('loc', []))}: {err.get('msg', '')!s}"
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


class AddCommunityRequest(BaseModel):
    """Схема JSON-тела запроса добавления сообщества."""
    group_id: int
    token: str


@app.post("/api/user/communities/add")
def add_user_community(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
    # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI,
    # поэтому здесь нельзя использовать await request.body(); raw body логируется
    # в async-обработчике ошибок валидации (validation_exception_handler).
    logger.info(
        f"[communities/add] content-type={request.headers.get('content-type')}"
    )

    user = get_current_user(request, db)
    if not user:
        logger.warning(f"[communities/add] 401: пользователь не авторизован (group_id={group_id})")
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")
    logger.info(f"[communities/add] Пользователь авторизован: id={user.id}, email={user.email}")
    
    # Проверяем токен и получаем информацию о сообществе через VK API.
    # group_id внутри системы всегда положительный (VkApi — с api_version=5.199).
    group_id = positive_group_id(group_id)
    token = token.strip()
    try:
        vk_session = create_vk_session(token)
        vk = vk_session.get_api()

        # ВАЖНО (VK API 5.199): метод groups.getById принимает параметр group_ids
        # (строка со значениями через запятую), а НЕ group_id. Значение должно быть
        # ПОЛОЖИТЕЛЬНЫМ числом (без минуса). Метод требует scope `groups` у токена.
        # Формат ответа зависит от версии vk_api:
        #   - {'groups': [{'id': ..., 'name': ...}], 'profiles': []}  (актуальный формат)
        #   - [{'id': ..., 'name': ...}]                              (готовый список)
        #   - {'items': [...], 'count': N}                            (обратная совместимость)
        logger.info(
            f"[communities/add] Вызов groups.getById: group_ids='{abs(int(group_id))}', "
            f"token_mask='{token[:5]}***' (api_version=5.199)"
        )
        result = vk.groups.getById(group_ids=str(abs(int(group_id))))
        if isinstance(result, dict):
            items = result.get("groups", result.get("items", [])) or []
        elif isinstance(result, list):
            items = result
        else:
            items = []
        if not items:
            logger.warning(
                f"[communities/add] groups.getById вернул пустой ответ [] для группы "
                f"{group_id}. Возможные причины: 1) у токена отсутствует право 'groups'; "
                f"2) ID сообщества указан неверно; 3) токен отозван или создан для другого "
                f"сообщества (token_mask='{token[:5]}***')."
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Не удалось проверить сообщество. Убедитесь, что: "
                    "1) Токен имеет право 'groups', "
                    "2) ID сообщества указан верно, "
                    "3) Вы являетесь администратором этого сообщества."
                )
            )
        group_info = items[0]
        group_name = group_info.get("name", f"Группа {group_id}")
        logger.info(
            f"[communities/add] groups.getById OK: id={group_info.get('id')}, "
            f"name='{group_name}'"
        )

        # Права администратора для сервисного ключа сообщества подтверждаются
        # доступностью getLongPollServer (требует права manage): этот вызов
        # возможен только ключом администратора данного сообщества. Поле is_admin
        # с community-токеном всегда 0 и непригодно для проверки.
        try:
            vk.groups.getLongPollServer(group_id=group_id)
        except vk_api.exceptions.ApiError as lps_err:
            logger.warning(
                f"[communities/add] getLongPollServer не прошёл для группы {group_id}: {lps_err} "
                f"(у токена нет права manage / это не ключ данного сообщества)"
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Не удалось подтвердить права администратора в сообществе «{group_name}». "
                    "Используйте КЛЮЧ ДОСТУПА СООБЩЕСТВА, созданный администратором этого "
                    "сообщества: Сообщество → Управление → Работа с API → Ключи доступа → "
                    "«Создать ключ» (обязательные права: groups, manage, wall, photos, "
                    "messages), тип: «Ключ сообщества»."
                )
            )

        logger.info(f"[communities/add] VK API: группа {group_name} ({group_id}), права подтверждены (getLongPollServer OK)")

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
def remove_user_community(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
def get_communities():  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    """Возвращает список всех зарегистрированных сообществ."""
    if not hasattr(app.state, 'community_manager'):
        return {"communities": [], "error": "CommunityManager ещё не инициализирован"}
    
    communities = app.state.community_manager.get_community_list()
    return {"communities": communities}


@app.post("/api/communities/register")
def register_community(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
def unregister_community(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    group_id: int) -> Dict[str, Any]:
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
def get_content_plan_periods(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
                "title": p.title or "",
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


class PeriodCreateRequest(BaseModel):
    """Тело запроса создания периода контент-плана."""
    period_type: str = "week"


class PeriodRenameRequest(BaseModel):
    """Тело запроса переименования периода (черновика)."""
    title: str


@app.post("/api/content-manager/period/create")
def create_content_plan_period(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    payload: PeriodCreateRequest,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Создаёт новый период контент-плана на выбранный срок."""
    user = require_auth(request, db)

    period_type = payload.period_type.strip().lower()
    if period_type not in ("week", "two_weeks", "month"):
        raise HTTPException(
            status_code=400,
            detail=f"Недопустимый тип периода '{period_type}'. Доступно: week, two_weeks, month"
        )
    logger.info(f"[period/create] Создаём период типа '{period_type}' для пользователя id={user.id}")

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
            "title": period.title or "",
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "status": period.status
        },
        "welcome_message": welcome_message
    }


@app.get("/api/content-manager/chat/{period_id}")
def get_chat_messages(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Отправляет сообщение в чат с контент-менеджером и получает ответ ИИ.

    FIX: сообщение принимается из JSON-тела {"message": "..."} (так отправляет
    фронтенд content_manager.html), а не из query-параметра — раньше FastAPI
    искал message в query и возвращал 422 «Field required».
    """
    # Читаем тело: основной вариант — JSON; fallback — form-data/urlencoded.
    message = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            message = str(body.get("message", "")).strip()
    except Exception:  # noqa: BLE001 — не JSON: пробуем форму
        pass
    if not message:
        try:
            form = await request.form()
            message = str(form.get("message", "")).strip()
        except Exception:  # noqa: BLE001
            pass
    if not message:
        raise HTTPException(status_code=400, detail="Поле 'message' обязательно и не может быть пустым")

    user = require_auth(request, db)
    
    # Проверяем что период принадлежит пользователю
    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()
    
    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")
    
    # Генерируем ответ ИИ
    # FIX: Event loop unblocked — синхронный вызов ИИ (requests.post внутри)
    # вынесён в поток через asyncio.to_thread, event loop не блокируется.
    ai_response_text = await asyncio.to_thread(
        generate_ai_response, db, period_id, message, user.id
    )
    
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
    
    # FIX: Event loop unblocked — синхронная генерация через ИИ (requests.post внутри)
    # вынесена в поток через asyncio.to_thread, event loop не блокируется.
    posts = await asyncio.to_thread(generate_content_plan_from_chat, db, period_id, user.id)
    
    return {
        "success": True,
        "message": f"Создано {len(posts)} постов",
        "posts": posts
    }


@app.put("/api/content-manager/period/{period_id}/update")
def update_content_plan(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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


@app.put("/api/content-manager/period/{period_id}/rename")
def rename_content_plan_period(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    period_id: int,
    request: Request,
    payload: PeriodRenameRequest,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Переименовывает период контент-плана (черновик)."""
    user = require_auth(request, db)

    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Название не может быть пустым")
    if len(title) > 255:
        raise HTTPException(status_code=400, detail="Название слишком длинное (макс. 255 символов)")

    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()

    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")

    old_title = period.title or ""
    period.title = title
    period.updated_at = datetime.datetime.now()
    db.commit()
    logger.info(f"[period/rename] Период id={period_id}: '{old_title}' -> '{title}' (user id={user.id})")

    return {
        "success": True,
        "period": {
            "id": period.id,
            "title": period.title,
            "period_type": period.period_type,
            "status": period.status
        }
    }


@app.delete("/api/content-manager/period/{period_id}")
def delete_content_plan_period(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    period_id: int,
    request: Request,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Удаляет период контент-плана вместе с его постами и чатом (cascade в модели).

    Публикации со статусом 'published' или 'scheduled' блокируют удаление,
    чтобы случайно не удалить уже ушедший в VK контент.
    """
    user = require_auth(request, db)

    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()

    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")

    protected_statuses = ("published", "scheduled")
    protected_posts = db.query(Post).filter(
        Post.content_plan_period_id == period_id,
        Post.status.in_(protected_statuses)
    ).count()

    if protected_posts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Нельзя удалить: в плане {protected_posts} постов со статусом "
                "'published'/'scheduled'. Сначала удалите или отмените эти публикации."
            )
        )

    posts_count = len(period.posts)
    chat_count = len(period.chat_messages)
    db.delete(period)
    db.commit()
    logger.info(
        f"[period/delete] Удалён период id={period_id} (постов: {posts_count}, "
        f"сообщений чата: {chat_count}) пользователем id={user.id}"
    )

    return {
        "success": True,
        "deleted_period_id": period_id,
        "deleted_posts": posts_count,
        "deleted_chat_messages": chat_count
    }


@app.post("/api/content-manager/check-expiring")
def check_expiring_plans(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
def get_period_posts(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
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
