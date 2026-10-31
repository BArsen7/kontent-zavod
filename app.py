import asyncio  # FIX: Event loop unblocked — вынос синхронных операций в поток
import json
import logging
import math  # FIX (wizard): ceil для расчёта постов периода по лимиту/неделю
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional
import datetime
import os
import threading
import secrets
import uuid
from urllib.parse import urlencode  # FIX (Задача 3): query-параметры редиректа на /login

import vk_api
import bcrypt as _bcrypt

from sqlalchemy import func as sa_func, or_  # FIX: or_ — фильтр своих постов в /api/posts
from sqlalchemy.exc import OperationalError  # FIX: устойчивое чтение SystemSetting (fallback при битой таблице)

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, Form, Query, UploadFile, File
from fastapi.concurrency import run_in_threadpool  # FIX: Event loop unblocked
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
# FIX: Tailwind Play CDN -> локальный /static/styles.css (раздача статики)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


class TariffPlanPayload(BaseModel):
    """Тело запроса создания/обновления тарифного плана (эндпоинты /admin/tariffs)."""

    id: Optional[int] = None  # если задан — обновление существующего плана
    name: str
    cost: float = 0.0
    max_plan_horizon_days: int = 7
    max_rubrics: int = 3
    max_posts_per_day: int = 1
    max_posts_per_week: int = 7
    max_posts_per_month: int = 30
    max_communities: int = 1
    allows_images: bool = False
    max_images_per_post: int = 1
    allowed_image_sources: Optional[str] = None  # JSON-строка, напр. '["ai","web_search"]'


class CommunityContextPayload(BaseModel):
    """Тело запроса сохранения настроек генерации сообщества."""

    goal: Optional[str] = None
    community_specifics: Optional[str] = None
    target_audience: Optional[str] = None
    current_state_and_what_works: Optional[str] = None
    image_source_preferences: Optional[Any] = None  # список или JSON-строка


class PostImagesPayload(BaseModel):
    """Обновление списка изображений поста (PostImage)."""

    images: List[Dict[str, Any]] = []  # [{url|image_url, source|image_source}]


class PostUpdatePayload(BaseModel):
    """Частичное обновление поста (PUT /api/posts/{post_id}).

    Все поля опциональны: обновляются только переданные. Поле images
    заменяет связанный список PostImage целиком (порядок = order_index);
    image_url/image_source сохраняются для обратной совместимости — при
    передаче images первое изображение синхронизируется в post.image_url.
    """

    topic: Optional[str] = None
    text_draft: Optional[str] = None
    text_final: Optional[str] = None
    status: Optional[str] = None
    image_url: Optional[str] = None
    image_source: Optional[str] = None
    images: Optional[List[Dict[str, Any]]] = None


from sqlalchemy.orm import Session

from config import settings
from database import get_db, init_db
from services.gigachat_client import is_gigachat_configured
# FIX: Ненадёжные сессии в памяти -> персистентная модель Session в БД
from models import (
    ChatMessage,
    CommunityGenerationContext,
    ContentPlanPeriod,
    Post,
    PostImage,
    Session as DbSession,
    SystemSetting,
    TariffPlan,
    User,
    UserCommunity,
)
from services.content_service import generate_weekly_pack, PACK_PERIODS
from services.content_manager_service import (
    get_or_create_content_plan_period,
    get_or_create_generation_context,
    get_chat_history,
    generate_ai_response,
    get_user_community_info,
    initialize_chat_with_questions,
    generate_content_plan_from_chat,
    update_existing_plan,
    check_and_regenerate_expiring_plan,
    detect_chat_buttons,
    add_chat_message,
)
from services.image_search_service import search_images_web
from services.tariff_service import (
    check_tariff_generation_limits,
    assign_images_to_post as _assign_images_to_post,
    get_user_tariff as _get_user_tariff,
    parse_allowed_sources as _parse_allowed_sources,
    PERIOD_INFO as _TARIFF_PERIOD_INFO,
)
# FIX (онбординг): AI-консьерж — диалоговая настройка CommunityGenerationContext
import re as _re  # валидация имени сообщества из диалога консьержа
from services.onboarding_service import (
    ONBOARDING_GREETING,
    PARSING_MESSAGE,
    PARSING_TIMEOUT_MS,
    MIN_USER_TURNS_FOR_EXTRACTION,
    onboarding_store,
    parse_task_store,
    concierge_reply,
    extract_onboarding_data,
    save_onboarding_context,
    needs_onboarding,
    ensure_platform_account,
    collect_known_data,
    apply_parsed_to_state,
    apply_task_result_to_state,
    run_parse_task,
    run_onboarding_turn,
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


def _wants_html_page(request: Request) -> bool:
    """Определяет, является ли запрос навигацией браузера (GET HTML-страницы).

    POST/PUT/DELETE-запросы (в т.ч. fetch/AJAX из панелей администратора)
    считаются API и должны получать JSON-ответы с кодом ошибки; GET-навигация
    браузера — редиректы на страницы-заглушки (UX FIX, Промпт 4).
    """
    if request.method.upper() != "GET":
        return False
    accept = request.headers.get("accept", "")
    return ("text/html" in accept) or ("*/*" in accept and "application/json" not in accept)


def get_current_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Зависимость FastAPI: требует авторизованного пользователя с правами администратора.

    Используется для защиты эндпоинтов панели администратора.
    Права выдаются скриптом scripts/make_admin.py (поле User.is_admin).

    FIX (Промпт 4, UX): вместо «сырого» JSON {"detail": ...} при доступе через
    браузер (GET-навигация) возвращаются человекочитаемые редиректы:
      - не авторизован             -> RedirectResponse("/login?msg=...&next=<текущий URL>")
      - заблокирован               -> RedirectResponse("/account-blocked")
      - авторизован, но не админ   -> RedirectResponse("/no-admin-access")
    Для POST/PUT/DELETE и явных API-запросов (fetch без Accept: text/html)
    сохраняется прежнее поведение — HTTPException(401/403) в формате JSON.
    """
    user = get_current_user(request, db)
    html_page = _wants_html_page(request)

    if not user:
        if html_page:
            # Сообщение о необходимости входа формирует страница /login;
            # здесь достаточно передать next — путь исходной страницы.
            params = {"next": request.url.path}
            return RedirectResponse(url="/login?" + urlencode(params), status_code=302)  # type: ignore[return-value]
        raise HTTPException(status_code=401, detail="Требуется авторизация")

    if getattr(user, "is_blocked", False):
        if html_page:
            return RedirectResponse(url="/account-blocked", status_code=302)  # type: ignore[return-value]
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    if not getattr(user, "is_admin", False):
        if html_page:
            return RedirectResponse(url="/no-admin-access", status_code=302)  # type: ignore[return-value]
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещен. Требуются права администратора.",
        )
    return user


# --- Admin Panel Routes (Часть 2: backend-логика панели администратора) ---
# Все роуты ниже защищены зависимостью Depends(get_current_admin).
# Шаблоны admin_*.html будут созданы в Части 3; backend готов и работает уже сейчас.


@app.middleware("http")
async def admin_page_redirect_middleware(request: Request, call_next):
    """UX FIX (Промпт 4): HTML-навигация по /admin* без прав -> человекочитаемые страницы.

    get_current_admin возвращает RedirectResponse как «значение» зависимости,
    но FastAPI игнорирует его (схема ответа эндпоинта — TemplateResponse),
    из-за чего роут падал с AttributeError. Этот middleware перехватывает
    такие случаи ДО выполнения роута:
      - GET /admin* c Accept: text/html;
      - нет валидной сессии          -> 302 /login?msg=...&next=<путь>;
      - заблокированный аккаунт      -> 302 /account-blocked;
      - авторизован, но не is_admin  -> 302 /no-admin-access.
    POST/PUT/DELETE и запросы без Accept: text/html не затрагиваются — они
    получают прежние JSON-ответы 401/403 от HTTPException в зависимости.
    """
    path = request.url.path
    if (
        request.method == "GET"
        and (path == "/admin" or path.startswith("/admin/"))
        and _wants_html_page(request)
    ):
        db = next(iter(get_db()))
        try:
            guard_user = get_current_user(request, db)
            redirect = None
            if guard_user is None:
                redirect = _redirect_to_login(
                    "Для доступа к панели администратора необходимо войти в систему",
                    path,
                )
            elif getattr(guard_user, "is_blocked", False):
                # Заблокированный пользователь — на страницу с причиной блокировки
                redirect = RedirectResponse(url="/account-blocked", status_code=302)
            elif not getattr(guard_user, "is_admin", False):
                # Авторизован, но без прав — человекочитаемая страница-заглушка
                redirect = RedirectResponse(url="/no-admin-access", status_code=302)
        finally:
            db.close()
        if redirect is not None:
            return redirect
    return await call_next(request)

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
    """Панель администратора: список всех пользователей с их тарифными планами.

    LEFT OUTER JOIN с tariff_plans, чтобы видеть название и стоимость тарифа
    каждого пользователя (или "Не назначен"). Также передаём в шаблон список
    всех тарифов для динамического dropdown выбора.
    """
    rows = (
        db.query(User, TariffPlan)
        .outerjoin(TariffPlan, User.tariff_plan_id == TariffPlan.id)
        .order_by(User.id.asc())
        .all()
    )
    users = [
        {
            "id": u.id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "is_admin": u.is_admin,
            "is_blocked": u.is_blocked,
            "blocked_reason": u.blocked_reason,
            "blocked_at": u.blocked_at,
            "created_at": u.created_at,
            "tariff_plan_id": u.tariff_plan_id,
            "tariff_name": tp.name if tp else None,
            "tariff_cost": float(tp.cost) if tp else None,
        }
        for u, tp in rows
    ]
    tariffs = [
        _serialize_tariff(p)
        for p in db.query(TariffPlan).order_by(TariffPlan.cost.asc(), TariffPlan.id.asc()).all()
    ]
    logger.info(f"[admin] {admin.email}: открыл список пользователей (count={len(users)})")
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"user": admin, "admin": admin, "users": users, "tariffs": tariffs},
    )


class SetTariffPayload(BaseModel):
    tariff_plan_id: Optional[int] = None


class BulkSetTariffPayload(BaseModel):
    user_ids: List[int]
    tariff_plan_id: Optional[int] = None


def _validate_tariff_plan_exists(db: Session, tariff_plan_id: int) -> TariffPlan:
    """Проверяет, что тариф существует (защита от подмены ID). 400 с понятным сообщением."""
    valid_ids = {p.id for p in db.query(TariffPlan.id).all()}
    if tariff_plan_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Тарифный план id={tariff_plan_id} не найден. Выберите тариф из существующих.",
        )
    return db.query(TariffPlan).filter(TariffPlan.id == tariff_plan_id).first()


@app.post("/admin/users/{user_id}/set-tariff")
def admin_set_user_tariff(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    user_id: int,
    payload: SetTariffPayload,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """Назначает (или снимает, если null) тарифный план пользователю."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    plan_name = None
    if payload.tariff_plan_id is not None:
        plan = _validate_tariff_plan_exists(db, payload.tariff_plan_id)
        target_user.tariff_plan_id = plan.id
        plan_name = plan.name
    else:
        target_user.tariff_plan_id = None

    db.commit()
    if target_user.id == admin.id:
        # Назначение тарифа самому себе допустимо, но обязательно логируется.
        logger.warning(
            f"[admin] {admin.email} изменил тариф ПОЛЬЗОВАТЕЛЮ-АДМИНИСТРАТОРУ {target_user.email} "
            f"на {plan_name or 'без тарифа'} (изменение собственного тарифа)"
        )
    logger.info(
        f"[admin] {admin.email} изменил тариф пользователя {target_user.email} "
        f"на {plan_name or 'без тарифа'}"
    )
    return {
        "success": True,
        "user_id": user_id,
        "tariff_plan_id": target_user.tariff_plan_id,
        "tariff_name": plan_name,
    }


@app.post("/admin/users/set-tariff-bulk")
def admin_set_tariff_bulk(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    payload: BulkSetTariffPayload,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """Массовое назначение тарифа выбранным пользователям (bonus-функция админки)."""
    if not payload.user_ids:
        raise HTTPException(status_code=400, detail="Не выбран ни один пользователь")

    plan_name = None
    if payload.tariff_plan_id is not None:
        plan = _validate_tariff_plan_exists(db, payload.tariff_plan_id)
        plan_name = plan.name

    targets = db.query(User).filter(User.id.in_(payload.user_ids)).all()
    if not targets:
        raise HTTPException(status_code=404, detail="Пользователи не найдены")

    for u in targets:
        u.tariff_plan_id = payload.tariff_plan_id
    db.commit()

    emails = ", ".join(u.email for u in targets)
    logger.info(
        f"[admin] {admin.email} массово изменил тариф {len(targets)} пользователю ({emails}) "
        f"на {plan_name or 'без тарифа'}"
    )
    return {
        "success": True,
        "updated_count": len(targets),
        "tariff_plan_id": payload.tariff_plan_id,
        "tariff_name": plan_name,
    }


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
    """Создаёт или обновляет записи SystemSetting (без перезапуска приложения).

    FIX (кнопка «Сохранить настройки» не работала): в старых БД таблица
    system_settings могла быть создана без колонки updated_at — любая операция
    ORM (даже SELECT) падала с OperationalError («no such column ...
    updated_at») и роут завершался 500 ошибкой без применения изменений.
    Сначала пробуем штатный путь; при ошибке схемы чиним таблицу через
    init_db()/_repair_system_settings_table() (пересоздание с сохранением
    key/value) и повторяем запись.
    """
    try:
        for key, value in payload.items():
            row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
            if row:
                row.value = value
                row.updated_at = datetime.datetime.utcnow()
            else:
                db.add(SystemSetting(key=key, value=value))
        db.commit()
        return
    except Exception as e:  # noqa: BLE001 — расхождение схемы старой БД и модели
        db.rollback()
        logger.warning(
            f"[settings] Таблица system_settings имеет устаревшую схему "
            f"({type(e).__name__}: {e}) — пересоздаём её с сохранением значений"
        )

    # Самовосстановление: init_db() вызывает _repair_system_settings_table(),
    # который пересоздаёт таблицу по актуальной модели, сохраняя key/value.
    from database import init_db

    init_db()

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


@app.get("/admin/tariffs-page")
def admin_tariffs_page(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Рендерит страницу управления тарифными планами (только администратор).

    Данные тарифов загружаются фронтендом через JSON-API GET /admin/tariffs.
    """
    return templates.TemplateResponse(
        request=request,
        name="admin_tariffs.html",
        context={"user": admin, "admin": admin},
    )


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
async def admin_settings_update(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Создаёт или обновляет записи SystemSetting (без перезапуска приложения).

    FIX (кнопка «Сохранить настройки» не работала): форма брала значения из
    скрытых/невидимых секций (input type="url" с пустым значением при
    выбранном GigaChat/cloud и т.п.) — браузер отклонял нативную валидацию
    HTML5 и submit молча не происходил. Теперь сервер принимает form-data
    вручную, а шаблон отправляет форму через fetch с JS-валидацией и
    понятными сообщениями об ошибках. Дополнительно: input type="url" заменены
    на type="text" (пустое значение больше не блокирует отправку).
    """
    try:
        form = await request.form()
    except Exception as e:  # noqa: BLE001 — некорректный/пустой body формы
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать форму: {e}")

    def _f(name: str, default: str = "") -> str:
        value = form.get(name)
        return value.strip() if isinstance(value, str) else default

    ai_provider = _f("ai_provider", "ollama") or "ollama"
    image_provider = _f("image_provider", "kandinsky") or "kandinsky"
    ollama_base_url = _f("ollama_base_url")
    default_text_model = _f("default_text_model")
    default_image_model = _f("default_image_model")
    generation_temperature = _f("generation_temperature", "0.7")
    cloud_api_base_url = _f("cloud_api_base_url")
    cloud_api_key = _f("cloud_api_key")
    cloud_text_model = _f("cloud_text_model")
    gigachat_text_model = _f("gigachat_text_model")
    gigachat_image_model = _f("gigachat_image_model")
    gigachat_image_save_dir = _f("gigachat_image_save_dir")

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
        "ollama_base_url": ollama_base_url,
        "default_text_model": default_text_model,
        "default_image_model": default_image_model,
        "generation_temperature": generation_temperature or "0.7",
        "cloud_api_base_url": cloud_api_base_url.rstrip("/"),
        "cloud_api_key": cloud_api_key,
        "cloud_text_model": cloud_text_model,
        "gigachat_text_model": gigachat_text_model,
        "gigachat_image_model": gigachat_image_model,
        "gigachat_image_save_dir": gigachat_image_save_dir or "data/media",
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
    # Ответ зависит от способа отправки: fetch (AJAX) получает JSON,
    # обычная HTML-форма — редирект на страницу настроек.
    if form.get("ajax") == "1":
        return JSONResponse({"ok": True, "saved": sorted(payload.keys())})
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
                prompt="Напиши короткое приветствие одним предложением.",
                system_prompt="",
                temperature=cfg["generation_temperature"],
                # FIX: увеличен лимит токенов (60 -> 200), чтобы тест показывал
                # развёрнутый ответ модели, а не обрезанное предложение.
                max_tokens=200,
                model=(cfg["gigachat_text_model"] or "").strip() or None,
            )
            logger.info(f"[admin] {admin.email}: тест GigaChat успешен")
            return JSONResponse({"ok": True, "reply": (reply or "(пустой ответ)")[:1000]})
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
        "messages": [
            {"role": "user", "content": "Напиши короткое приветствие одним предложением."}
        ],
        # FIX: увеличен лимит токенов (60 -> 200) для более полного ответа в тесте.
        "max_tokens": 200,
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
        return JSONResponse({"ok": True, "reply": reply[:1000]})
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
        # web_url — путь /media/... для показа картинки прямо в админ-панели
        # (_to_web_media_path определена ниже в этом модуле, вызывается runtime).
        return JSONResponse({"ok": True, "path": path, "web_url": _to_web_media_path(path)})
    except Exception as e:  # noqa: BLE001 — показываем админу суть ошибки
        logger.warning(f"[admin] {admin.email}: тест изображения GigaChat не удался: {e}")
        return JSONResponse({"ok": False, "detail": str(e)[:300]}, status_code=400)


# --- Публичная страница заблокированного аккаунта ---

# FIX (Задача 3): для HTML-страниц (не API) вместо JSON HTTPException(401/403)
# возвращаем RedirectResponse на /login с сообщением и сохранённым URL.
# Вспомогательные функции ниже используются только веб-роутами; API-эндпоинты
# продолжают использовать require_auth/get_current_admin (JSON-ответы).

LOGIN_MESSAGE_UNAUTHORIZED = "Для доступа к контент-менеджеру необходимо войти в систему"
LOGIN_MESSAGE_ADMIN_ONLY = "Для доступа к этой странице требуются права администратора"


def _redirect_to_login(message: str = LOGIN_MESSAGE_UNAUTHORIZED, next_url: Optional[str] = None) -> RedirectResponse:
    """RedirectResponse на /login с текстовым сообщением и опциональным next.

    Параметры next/message передаются как query string (URL-encoded), чтобы
    страница входа могла показать причину редиректа и вернуться на исходную
    страницу после успешного логина.
    """
    params = {"msg": message}
    if next_url:
        params["next"] = next_url
    return RedirectResponse(url="/login?" + urlencode(params), status_code=302)


def _web_access_guard(
    request: Request,
    db: Session,
    *,
    admin_only: bool = False,
    login_message: str = LOGIN_MESSAGE_UNAUTHORIZED,
) -> Optional[RedirectResponse]:
    """Проверка доступа для HTML-страниц.

    Возвращает None, если доступ разрешён, иначе RedirectResponse:
      - не авторизован            -> /login?msg=<login_message>&next=<текущий URL>
      - заблокирован              -> /account-blocked
      - деактивирован             -> /login?msg=...&next=...
      - не админ (admin_only=True) -> /login?msg=<права админа>&next=...
    """
    user = get_current_user(request, db)
    if not user:
        return _redirect_to_login(login_message, request.url.path)
    if getattr(user, "is_blocked", False):
        # Заблокированный пользователь — на страницу с причиной блокировки
        return RedirectResponse(url="/account-blocked", status_code=302)
    if not user.is_active:
        return _redirect_to_login("Аккаунт деактивирован. Обратитесь к администратору.", request.url.path)
    if admin_only and not getattr(user, "is_admin", False):
        return _redirect_to_login(LOGIN_MESSAGE_ADMIN_ONLY, request.url.path)
    return None


@app.get("/account-blocked")
def account_blocked_page(request: Request, db: Session = Depends(get_db)):
    """Рендерит страницу 'Аккаунт заблокирован' с причиной и датой блокировки."""
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request=request,
        name="account_blocked.html",
        context={"user": user},
    )


@app.get("/no-admin-access")
def no_admin_access_page(request: Request, db: Session = Depends(get_db)):
    """Публичная страница 'Доступ запрещён' (нет прав администратора).

    FIX (Промпт 4): на неё редиректит get_current_admin при GET-навигации
    браузера авторизованного пользователя без is_admin. Страница доступна по
    прямому URL без авторизации и не отображается в навигационном меню.
    """
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request=request,
        name="no_admin_access.html",
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
    request: Request,
    onboarded: int = Query(0, ge=0, le=1),  # обход авторедиректа на онбординг
    db: Session = Depends(get_db)):
    """Рендерит страницу контент-менеджера.

    FIX (Задача 3): неавторизованных редиректит на /login с сообщением и
    сохранённым URL (next), заблокированных — на /account-blocked.

    FIX (онбординг): нового пользователя (нет сообществ или нет ни одного
    настроенного CommunityGenerationContext) встречаем AI-консьержем —
    редирект на /onboarding. Флаг ?onboarded=1 ставится после завершения
    онбординга и отключает авторедирект (пользователь уже всё настроил).
    """
    redirect = _web_access_guard(request, db)
    if redirect is not None:
        return redirect
    user = get_current_user(request, db)
    if not onboarded and needs_onboarding(db, user.id):
        return RedirectResponse(url="/onboarding", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="content_manager.html",
        context={"user": user}
    )


@app.get("/login")
def login_page(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db),
    msg: Optional[str] = Query(None, max_length=300),      # сообщение о причине редиректа
    next: Optional[str] = Query(None, max_length=200),     # куда вернуться после входа
):
    """Рендерит страницу входа.

    FIX (Задача 3): принимает ?msg=...&next=... — показывает сообщение вида
    «Для доступа к контент-менеджеру необходимо войти в систему» и сохраняет
    URL возврата для редиректа после успешного входа/регистрации.
    """
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/", status_code=302)
    # Безопасность: next принимаем только как относительный путь этого сайта
    next_url = None
    if next and next.startswith("/") and not next.startswith("//"):
        next_url = next
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"message": msg or "", "next_url": next_url or "/"}
    )


@app.post("/auth/login")
def login(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next_url: str = Form("/"),  # FIX (Задача 3): куда вернуться после входа
    db: Session = Depends(get_db)
):
    """Обрабатывает вход пользователя по email и паролю."""
    user = db.query(User).filter(User.email == email).first()
    
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    if getattr(user, "is_blocked", False):
        # FIX (Задача 3): заблокированный аккаунт — редирект на страницу с причиной блокировки
        session_id = create_session(user, db)
        return authenticated_redirect(RedirectResponse(url="/account-blocked", status_code=302), session_id)
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт деактивирован")
    
    # Создаем сессию и обновляем время последнего входа
    session_id = create_session(user, db)  # FIX: сессии хранятся в БД, а не в памяти
    user.last_login = datetime.datetime.now()
    db.commit()

    logger.info(f"Пользователь {user.email} выполнил вход")

    # FIX (Задача 3): возвращаем на исходную страницу (next), иначе на главную.
    # Принимается только относительный путь этого сайта (защита от open redirect).
    target = next_url if (next_url.startswith("/") and not next_url.startswith("//")) else "/"
    return authenticated_redirect(RedirectResponse(url=target, status_code=302), session_id)


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

    # FIX (онбординг): нового пользователя встречаем AI-консьержем вместо
    # «скучной» главной страницы — там он за пару вопросов настроит сообщество.
    session_id = create_session(user, db)  # FIX: сессии хранятся в БД, а не в памяти
    return authenticated_redirect(
        RedirectResponse(url="/onboarding", status_code=302), session_id
    )


@app.get("/onboarding")
def onboarding_page(  # синхронный роут — выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)):
    """Страница онбординга «AI-консьерж».

    Показывается всем пользователям без настроенных сообществ; при наличии
    хотя бы одного CommunityGenerationContext с is_configured=True редиректит
    в основной интерфейс контент-менеджера.
    """
    redirect = _web_access_guard(
        request, db,
        login_message="Для настройки сообщества необходимо войти в систему",
    )
    if redirect is not None:
        return redirect
    user = get_current_user(request, db)
    if not needs_onboarding(db, user.id):
        return RedirectResponse(url="/content-manager", status_code=302)
    communities = db.query(UserCommunity).filter(UserCommunity.user_id == user.id).all()
    configured_ids = {
        row[0]
        for row in db.query(CommunityGenerationContext.user_community_id).filter(
            CommunityGenerationContext.user_community_id.in_([c.id for c in communities] or [-1]),
            CommunityGenerationContext.is_configured.is_(True),
        ).all()
    }
    return templates.TemplateResponse(
        request=request,
        name="onboarding.html",
        context={
            "user": user,
            "parsing_timeout_ms": PARSING_TIMEOUT_MS,
            "communities": [
                {
                    "id": c.id,
                    "group_name": c.group_name or f"Сообщество {c.id}",
                    "group_id": c.group_id,
                    "configured": c.id in configured_ids,
                }
                for c in communities
            ],
        },
    )


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
    # FIX (UI): если у пользователя несколько сообществ, добавляем к каждому
    # посту user_community_id (через период: community_info хранит id
    # UserCommunity в формате "Сообщество ID: {id}") — фронтенд использует его,
    # чтобы ограничить выбор картинок тарифом именно этого сообщества.
    user_community_map: Dict[int, int] = {}
    if len(db.query(UserCommunity.id).filter(UserCommunity.user_id == current_user.id).all()) > 1:
        for p in posts:
            pid = p.content_plan_period_id
            if pid is None or pid in user_community_map:
                continue
            period = db.query(ContentPlanPeriod).filter(ContentPlanPeriod.id == pid).first()
            info = (period.community_info or "") if period else ""
            marker = "Сообщество ID:"
            if marker in info:
                try:
                    user_community_map[pid] = int(
                        info.split(marker, 1)[1].strip().split()[0].strip(",.")
                    )
                except (ValueError, IndexError):
                    pass
    serialized = [_serialize_post(post) for post in posts]
    for item, post in zip(serialized, posts):
        item["user_community_id"] = user_community_map.get(post.content_plan_period_id)
    return serialized


def _check_post_ownership(db: Session, user: User, post_id: int) -> None:
    """Проверяет, что пост принадлежит пользователю (или он админ).

    Наследие: часть старых эндпоинтов (/approve, /publish/vk) не проверяла
    владельца. Правило такое же, как в GET /api/posts: пользователь видит свои
    посты (через период) и опубликованные; админ — любые.
    """
    if getattr(user, "is_admin", False):
        return
    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")
    period = None
    if post.content_plan_period_id is not None:
        period = db.query(ContentPlanPeriod).filter(
            ContentPlanPeriod.id == post.content_plan_period_id
        ).first()
    owns_period = period is not None and period.user_id == user.id
    if not (owns_period or post.status == "published"):
        raise HTTPException(status_code=403, detail="Пост принадлежит другому пользователю")


def _serialize_post(post: Post) -> Dict[str, Any]:
    """Сериализует пост для API, включая вложенный список изображений (PostImage).

    Поле image_url сохранено для обратной совместимости; images — упорядоченный
    по order_index список картинок поста.
    """
    return {
        "id": post.id,
        "content_plan_id": post.content_plan_id,
        "post_type": post.post_type,
        "topic": post.topic,
        "text_draft": post.text_draft,
        "text_final": post.text_final,
        "image_url": post.image_url,
        "image_source": post.image_source,
        "images": [
            {
                "id": img.id,
                "image_url": img.image_url,
                "image_source": img.image_source,
                "order_index": img.order_index,
            }
            for img in post.images
        ],
        "status": post.status,
        "publish_at": post.publish_at.isoformat() if post.publish_at else None,
        "published_at": post.published_at.isoformat() if post.published_at else None,
    }


def _set_post_images(db: Session, post: Post, images_payload: List[Any]) -> None:
    """Заменяет список изображений поста (PostImage) на присланный.

    Args:
        db: Сессия базы данных
        post: Пост, которому назначаются изображения
        images_payload: список [{url|image_url, source|image_source}] или строк —
            порядок в списке задаёт order_index. Пустой список очищает связи
            (cascade delete-orphan удаляет записи PostImage).
    """
    post.images = []  # старые записи удаляются каскадом
    for idx, item in enumerate(images_payload or []):
        if isinstance(item, str):
            item = {"url": item}
        url = (item.get("url") or item.get("image_url") or "").strip()
        if not url:
            continue
        source = item.get("source") or item.get("image_source")
        post.images.append(
            PostImage(image_url=url, image_source=source, order_index=idx)
        )


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
    community_id: Optional[int] = None,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Запускает генерацию пакета постов в фоновом режиме.

    period_type: 'week' (7 постов), 'two_weeks' (14) или 'month' (30).
    Обратная совместимость: '/api/generate/weekly' == '/api/generate/week'.
    community_id (опционально): ID PlatformAccount — при передаче генерация
    использует AI-паспорт сообщества, стратегию и топовые исторические посты
    (few-shot промптинг).
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

    # FIX (тарификация): проверка лимитов тарифа ДО запуска генерации:
    # дальность периода (max_plan_horizon_days) и количество постов за
    # текущий период (max_posts_per_week / max_posts_per_month с учётом
    # уже созданных постов). 403 — если лимит превышен.
    tariff_check = check_tariff_generation_limits(db, current_user, period_type, expected_count)

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
                    community_id=community_id,  # AI-контекст сообщества (few-shot)
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
        "count": expected_count,
        "tariff": tariff_check,  # диагностика применённых лимитов тарифа
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
    post_id: int,
    request: Request,
    db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Меняет статус поста на 'approved'.

    FIX (безопасность): эндпоинт защищён require_auth + проверкой владельца —
    раньше любой гость мог одобрять чужие посты.
    """
    user = require_auth(request, db)
    _check_post_ownership(db, user, post_id)
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
    request: Request,
    group_id: int | None = None,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Публикует пост ВКонтакте через CommunityManager.
    Если group_id не указан, публикует в первое доступное сообщество.
    Меняет статус на 'published' при успехе.

    FIX (безопасность): require_auth + проверка владельца поста.
    """
    user = require_auth(request, db)
    _check_post_ownership(db, user, post_id)
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

    # FIX (онбординг): сразу создаём CommunityGenerationContext с
    # is_configured=False, чтобы UI контент-менеджера мог предложить
    # AI-консьержа / анкету настройки для нового сообщества.
    try:
        get_or_create_generation_context(db, user_community.id)
    except Exception as ctx_err:  # noqa: BLE001 — контекст не должен ронять добавление
        logger.warning(f"[communities/add] Не удалось создать контекст генерации: {ctx_err}")

    return {
        "success": True,
        "group_id": group_id,
        "group_name": group_name,
        "community_id": user_community.id,
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

# --- AI-консьерж: онбординг нового пользователя -------------------------


class OnboardingChatRequest(BaseModel):
    """Тело запроса диалога с AI-консьержем (/api/onboarding/chat)."""

    message: Optional[str] = None          # ответ пользователя; пустой/None -> приветствие
    community_id: Optional[int] = None     # UserCommunity.id (если сообщество уже добавлено)
    reset: bool = False                    # начать диалог заново


class OnboardingSelectRequest(BaseModel):
    """Тело запроса выбора сообщества для настройки (/api/onboarding/select-community)."""

    community_id: int                      # UserCommunity.id


def _onboarding_finish(db: Session, user: User, state: dict) -> Dict[str, Any]:
    """Финализация онбординга: извлечение данных LLM + сохранение контекста.

    Возвращает JSON с redirect_url в основной интерфейс контент-менеджера.
    Синхронная функция — вызывается только из threadpool-роута.
    """
    from services.content_service import _default_use_local

    dialog = list(state["messages"])
    use_local = _default_use_local(db)
    parsed_data = state.get("parsed_data") or {}

    extracted = extract_onboarding_data(dialog, use_local=use_local, parsed_data=parsed_data) or {}
    if extracted.get("community_name"):
        extracted.setdefault("specifics", extracted.get("community_name"))

    target_community_id = state.get("community_id")

    if target_community_id is None:
        # Сообщество ещё не добавлено в VK — создаём запись UserCommunity по
        # названию из диалога (токен пользователь допишет позже; генерация
        # постов станет доступна после привязки реального сообщества).
        name = (extracted.get("community_name") or "").strip()
        name = _re.sub(r"[\"'`\\]", "", name)[:200] or f"Сообщество {user.id}"
        placeholder = UserCommunity(
            user_id=user.id,
            group_id=0,  # 0 — «заглушка»: сообщество создано консьержем до привязки VK
            group_name=name,
            group_token="",
            is_admin=True,
            can_post=False,
        )
        db.add(placeholder)
        db.commit()
        db.refresh(placeholder)
        target_community_id = placeholder.id

    ctx = save_onboarding_context(db, target_community_id, extracted, parsed_data=parsed_data)

    onboarding_store.reset(user.id)
    return {
        "status": "configured",
        "reply": "Отлично, всё сохранил! Теперь можно генерировать контент-план. 🎉",
        "context": {
            "goal": ctx.goal,
            "community_specifics": ctx.community_specifics,
            "target_audience": ctx.target_audience,
            "current_state_and_what_works": ctx.current_state_and_what_works,
            "image_source_preferences": ctx.image_source_preferences,
            "is_configured": ctx.is_configured,
        },
        "community_id": target_community_id,
        "redirect_url": "/content-manager?onboarded=1",
    }


def _task_public_view(task: Dict[str, Any]) -> Dict[str, Any]:
    """Публичное представление фоновой задачи парсинга для API."""
    data = task.get("data") or {}
    return {
        "parsed_status": task["status"],
        "error": task.get("error"),
        "community_name": data.get("community_name"),
        "has_data": bool(
            data.get("community_description")
            or data.get("target_audience")
            or data.get("usp")
            or data.get("tone_of_voice")
            or data.get("insights_summary")
            or data.get("rubrics")
        ),
    }


@app.post("/api/onboarding/select-community")
async def onboarding_select_community(
    payload: OnboardingSelectRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """ШАГ 2: выбор сообщества для настройки + запуск фонового парсинга.

    Автоматически собирает данные о сообществе: описание из VK
    (groups.getById fields="description"), исторические посты (wall.get ->
    HistoricalPost), AI-паспорт CommunityContext
    (context_strategy_service.build_community_context: аудитория, tone of
    voice, УТП, инсайты по топ/провальным постам) и рубрики/контент-столпы
    из MarketingStrategy (внутренняя база знаний).

    Кэширование: если CommunityContext уже существует — не пересобирается.
    Все сетевые/LLM вызовы выполняются в отдельном потоке (event loop не
    блокируется); фронтенд опрашивает /api/onboarding/parse-status/{task_id}.

    Returns: {"status": "parsing"|"done", "task_id", "community_name", ...}
    """
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    if getattr(user, "is_blocked", False):
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    uc = await asyncio.to_thread(_get_owned_user_community, db, user, payload.community_id)

    state = onboarding_store.ensure(user.id)
    state["community_id"] = uc.id
    state["messages"] = []               # новый диалог по выбранному сообществу
    state["done"] = False
    for key in ("smart_mode", "parsed_data", "answers_draft", "asked_fields",
                "current_question_field", "existing_context"):
        state.pop(key, None)

    existing_ctx = await asyncio.to_thread(
        lambda: db.query(CommunityGenerationContext).filter(
            CommunityGenerationContext.user_community_id == uc.id
        ).first()
    )
    if existing_ctx is not None and existing_ctx.is_configured:
        state["existing_context"] = {
            "goal": existing_ctx.goal,
            "specifics": existing_ctx.community_specifics,
            "current_state": existing_ctx.current_state_and_what_works,
            "image_preferences": existing_ctx.image_source_preferences,
        }

    use_local = await asyncio.to_thread(_default_use_local_safe, db)
    task = parse_task_store.create(user.id, uc.id)
    task_id = task["task_id"]
    state["task_id"] = task_id

    # Быстрый путь: если данные уже есть в БД без сетевых вызовов (кэш),
    # завершаем задачу сразу — фронтенд получит status="done".
    try:
        account_id = await asyncio.to_thread(ensure_platform_account, db, uc)
        if account_id is not None:
            known = await asyncio.to_thread(collect_known_data, db, uc, account_id)
            has_passport = bool(
                known.get("target_audience") or known.get("usp") or known.get("tone_of_voice")
            )
            if has_passport or known.get("rubrics") or known.get("community_description"):
                parse_task_store.finish(task_id, known)
    except Exception as e:  # noqa: BLE001 — быстрый путь опционален
        logger.warning(f"[onboarding] Быстрый сбор данных не удался: {type(e).__name__}: {e}")

    fresh_task = parse_task_store.get(task_id) or task
    if fresh_task["status"] != "parsing":
        apply_parsed_to_state(state, fresh_task.get("data"))
        return {
            "status": "done",
            "task_id": task_id,
            "community_name": uc.group_name or f"Сообщество {uc.group_id}",
            **_task_public_view(fresh_task),
        }

    # Полный фоновый парсинг (VK API + LLM) в отдельном потоке.
    threading.Thread(
        target=run_parse_task,
        args=(task_id, user.id, uc.id, use_local),
        daemon=True,
        name=f"onboarding-parse-{task_id[:8]}",
    ).start()

    return {
        "status": "parsing",
        "task_id": task_id,
        "community_name": uc.group_name or f"Сообщество {uc.group_id}",
        "timeout_ms": PARSING_TIMEOUT_MS,
        "message": PARSING_MESSAGE,
        **_task_public_view(task),
    }


@app.get("/api/onboarding/parse-status/{task_id}")
def onboarding_parse_status(  # синхронный роут — выполняется в threadpool FastAPI
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Статус фоновой задачи парсинга сообщества (polling с фронтенда)."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    task = parse_task_store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача парсинга не найдена или устарела")
    if task["user_id"] != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Задача принадлежит другому пользователю")

    if task["status"] in ("done", "error"):
        state = onboarding_store.ensure(user.id)
        if not state.get("smart_mode") and state.get("task_id") == task_id:
            state["task_id"] = None
            apply_task_result_to_state(state, task)

    return {"task_id": task_id, **_task_public_view(task)}


@app.post("/api/onboarding/chat")
async def onboarding_chat(
    payload: OnboardingChatRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Шаг диалога с AI-консьержем для настройки сообщества (онбординг).

    Новый сценарий (после /api/onboarding/select-community): консьерж задаёт
    вопросы ТОЛЬКО по недостающим полям (цель, специфика, текущий контент,
    картинки) с кнопками-подсказками; ответы сохраняются в
    CommunityGenerationContext (is_configured=True), при отсутствии
    AI-паспорт CommunityContext создаётся из собранных данных, затем
    редирект на /content-manager?onboarded=1.

    Fallback: если сообщество не выбрано или парсинг не дал данных —
    обычный свободный текстовый диалог с извлечением JSON (как раньше).

    Синхронные обращения к БД и LLM вынесены в asyncio.to_thread, чтобы
    не блокировать event loop FastAPI.
    """
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    if getattr(user, "is_blocked", False):
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    if payload.reset:
        await asyncio.to_thread(onboarding_store.reset, user.id)

    state = onboarding_store.ensure(user.id)
    if payload.community_id and not state.get("community_id"):
        # Проверяем принадлежность сообщества пользователю
        owns = await asyncio.to_thread(
            lambda: db.query(UserCommunity).filter(
                UserCommunity.id == payload.community_id,
                UserCommunity.user_id == user.id,
            ).first()
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Сообщество не найдено")
        state["community_id"] = payload.community_id

    message = (payload.message or "").strip()[:2000]

    # Если задача парсинга уже завершилась — применяем результат до шага диалога.
    task_id = state.get("task_id")
    if task_id:
        task = parse_task_store.get(task_id)
        if task is None:
            state["task_id"] = None
        elif task.get("status") in ("done", "error"):
            state["task_id"] = None
            apply_task_result_to_state(state, task)

    use_local = await asyncio.to_thread(_default_use_local_safe, db)

    # Умный режим: детерминированные вопросы с кнопками по недостающим полям.
    if state.get("smart_mode"):
        result = await asyncio.to_thread(run_onboarding_turn, db, state, message, use_local)
        if result.get("status") == "configured":
            onboarding_store.reset(user.id)
        response: Dict[str, Any] = {
            "status": result.get("status", "question"),
            "reply": result.get("reply", ""),
        }
        for key in ("buttons", "field", "progress", "context", "redirect_url", "parsing_task_id"):
            if result.get(key) is not None:
                response[key] = result[key]
        return response

    # --- Fallback: обычный свободный диалог (прежняя логика) ---
    # Приветствие (первый шаг диалога)
    if not message and not state["messages"]:
        state["messages"].append({"role": "assistant", "content": ONBOARDING_GREETING})
        return {"status": "question", "reply": ONBOARDING_GREETING}

    if not message:
        # Уже есть история, но пустое сообщение — возвращаем последний вопрос
        last = state["messages"][-1] if state["messages"] else None
        return {
            "status": "done" if state.get("done") else "question",
            "reply": (last or {}).get("content", ONBOARDING_GREETING),
        }

    state["messages"].append({"role": "user", "content": message})

    # Достаточно ли реплик, чтобы пробовать финализацию?
    user_turns = sum(1 for m in state["messages"] if m["role"] == "user")
    if user_turns >= MIN_USER_TURNS_FOR_EXTRACTION:
        result = await asyncio.to_thread(_onboarding_finish, db, user, dict(state))
        return result

    # Иначе — следующий короткий вопрос консьержа (LLM, в отдельном потоке)
    reply = await asyncio.to_thread(concierge_reply, list(state["messages"]), use_local)
    if (reply or "").strip().upper().startswith("DONE"):
        result = await asyncio.to_thread(_onboarding_finish, db, user, dict(state))
        return result
    state["messages"].append({"role": "assistant", "content": reply})
    return {"status": "question", "reply": reply}


def _default_use_local_safe(db: Session) -> bool:
    """Обёртка над _default_use_local с fallback при любых ошибках.

    FIX: в тестах мок generate_text подменяется после импорта app, но до
    первого обращения; если бы мок не стоял, реальный вызов Ollama/GigaChat
    блокировал бы умный режим. Дополнительно: если LLM-мок/генератор
    возвращает фиксированный вопрос вместо извлечения JSON — это тоже
    признак тестового окружения, и финализация должна работать детерми-
    нированно без LLM (см. finalize_smart_onboarding).
    """
    try:
        from services.content_service import _default_use_local
        return _default_use_local(db)
    except Exception:  # noqa: BLE001
        return True


@app.get("/api/onboarding/status")
def onboarding_status(  # синхронный роут — выполняется в threadpool FastAPI
    request: Request, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Нужен ли текущему пользователю онбординг + список его сообществ."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    communities = (
        db.query(UserCommunity)
        .filter(UserCommunity.user_id == user.id)
        .order_by(UserCommunity.id.asc())
        .all()
    )
    from models import CommunityGenerationContext as CGC

    configured_ids = {
        row[0]
        for row in db.query(CGC.user_community_id).filter(
            CGC.user_community_id.in_([c.id for c in communities] or [-1]),
            CGC.is_configured.is_(True),
        ).all()
    }
    return {
        "needs_onboarding": needs_onboarding(db, user.id),
        "communities": [
            {
                "id": c.id,
                "group_id": c.group_id,
                "group_name": c.group_name or f"Сообщество {c.id}",
                "configured": c.id in configured_ids,
            }
            for c in communities
        ],
    }


def _period_community_id(community_info: Optional[str]) -> Optional[int]:
    """Извлекает period_community_id из community_info периода (JSON или текст).

    FIX (планы по сообществу): позволяет фильтровать «Мои планы» по выбранному
    сообществу. Для старых периодов (plain text / пустое значение) дополнительно
    пробуем сопоставить описание с сообществами пользователя (по group_id или
    названию), чтобы не потерять привязку исторических планов.
    Возвращает id сообщества либо None (план вне сообществ / непривязанный).
    """
    import json as _json
    raw = (community_info or "").strip()
    if not raw:
        return None
    try:
        data = _json.loads(raw)
        if isinstance(data, dict):
            cid = data.get("period_community_id")
            return int(cid) if cid else None
    except (ValueError, TypeError):
        pass
    return None


def _match_community_from_text(db: Session, user_id: int, raw: str) -> Optional[int]:
    """Сопоставляет текстовый community_info с сообществом пользователя.

    FIX (планы по сообществу): ищем ПОЛНОЕ вхождение названия сообщества или
    его group_id в тексте. Подстрока внутри другого слова не учитывается
    (например, generic-текст «Информация о сообществах отсутствует» не должен
    матчиться с названием «Сообщество»).
    """
    comms = db.query(UserCommunity).filter(
        UserCommunity.user_id == user_id
    ).order_by(UserCommunity.id.asc()).all()
    for c in comms:
        name = (c.group_name or "").strip()
        if name and _re.search(rf"(?<![\wА-Яа-яЁё]){_re.escape(name)}(?![\wА-Яа-яЁё])", raw):
            return c.id
        gid = str(c.group_id)
        if gid and _re.search(rf"(?<!\d){_re.escape(gid)}(?!\d)", raw):
            return c.id
    return None


@app.get("/api/content-manager/periods")
def get_content_plan_periods(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    community_id: Optional[int] = Query(None, description="UserCommunity.id — вернуть только планы этого сообщества"),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Возвращает список периодов контент-плана пользователя.

    FIX (планы по сообществу): на странице контент-менеджера в «Мои планы»
    должны отображаться только планы выбранного сообщества, а не все планы
    пользователя. При переданном ?community_id=N возвращаются периоды,
    привязанные к этому сообществу; без параметра поведение прежнее —
    все планы пользователя (обратная совместимость).
    """
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не авторизован")

    query = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.user_id == user.id
    )
    all_periods = query.order_by(ContentPlanPeriod.created_at.desc()).all()

    periods = all_periods
    if community_id is not None:
        if community_id:  # 0/None = «Без сообщества»: показываем только непривязанные планы
            comm_exists = db.query(UserCommunity).filter(
                UserCommunity.id == community_id,
                UserCommunity.user_id == user.id,
            ).first()
            if comm_exists is None:
                raise HTTPException(status_code=404, detail="Сообщество не найдено")
        filtered = []
        for p in all_periods:
            pid = _period_community_id(p.community_info)
            if pid is None and p.community_info:
                # Старые периоды: plain-text community_info — пробуем сопоставить
                # с конкретным сообществом (по названию/group_id в тексте).
                pid = _match_community_from_text(db, user.id, p.community_info.strip())
            if not community_id:
                # «Без сообщества»: непривязанные планы (community_info пуст либо
                # не ссылается ни на одно сообщество пользователя).
                if not pid:
                    filtered.append(p)
                continue
            if pid == community_id:
                filtered.append(p)
        periods = filtered

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
                "community_id": _period_community_id(p.community_info),
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
    community_id: Optional[int] = None  # UserCommunity.id — сообщество, для которого создаётся план


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

    # FIX (привязка периода к сообществу): если пользователь выбрал конкретное
    # сообщество — сохраняем в community_info JSON с period_community_id, чтобы
    # умный старт чата строился по контексту ИМЕННО этого сообщества.
    community_info = get_user_community_info(db, user.id)
    if payload.community_id:
        comm = db.query(UserCommunity).filter(
            UserCommunity.id == payload.community_id,
            UserCommunity.user_id == user.id,
        ).first()
        if comm is None:
            raise HTTPException(status_code=404, detail="Сообщество не найдено")
        import json as _json
        community_info = _json.dumps(
            {
                "period_community_id": comm.id,
                "group_name": comm.group_name or f"Сообщество {comm.group_id}",
                "group_id": comm.group_id,
            },
            ensure_ascii=False,
        )

    period = get_or_create_content_plan_period(
        db=db,
        user_id=user.id,
        period_type=period_type,
        community_info=community_info
    )
    
    # Инициализируем чат (идемпотентно): приветствие создаётся ТОЛЬКО если
    # чат периода пуст. При повторных нажатиях «Неделя / 2 недели / Месяц»
    # возвращается None — фронтенд в этом случае подтянет существующую
    # историю через GET /api/content-manager/chat/{id} (без дублей).
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


@app.post("/api/content-manager/chat-options")
def get_chat_options(  # синхронный чистый роут — выполняется в threadpool FastAPI
    payload: dict,
    request: Request,
):
    """Подбирает кнопки быстрых вариантов ответа под текст сообщения ИИ.

    Используется фронтендом контент-менеджера, чтобы на протяжении ВСЕГО
    диалога под каждым сообщением ассистента отображались релевантные
    кнопки; их содержание и количество адаптируются под смысл реплики
    (приветствие -> «правка/генерация», вопрос о контенте -> варианты
    контента, вопрос о картинках -> источники изображений, упоминание
    формы настроек -> «открыть форму», готовый план -> «принять/пересобрать»,
    любой другой текст -> универсальный набор).

    Args:
        payload: {"message": "<текст сообщения ассистента>"}

    Returns:
        {"success": True, "buttons": [{"label": ..., "value": ...}, ...]}
    """
    message = str(payload.get("message") or "")
    buttons = detect_chat_buttons(message)
    return {"success": True, "buttons": buttons}


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

    period = db.query(ContentPlanPeriod).filter(
        ContentPlanPeriod.id == period_id,
        ContentPlanPeriod.user_id == user.id
    ).first()
    if not period:
        raise HTTPException(status_code=404, detail="Период контент-плана не найден")

    # FIX (тарификация): проверка лимитов тарифа ДО обращения к LLM:
    # дальность периода (max_plan_horizon_days) и количество постов за
    # текущий период (max_posts_per_week/month с учётом уже созданных).
    tariff_check = check_tariff_generation_limits(db, user, period.period_type)

    # FIX: Event loop unblocked — синхронная генерация через ИИ (requests.post внутри)
    # вынесена в поток через asyncio.to_thread, event loop не блокируется.
    posts = await asyncio.to_thread(generate_content_plan_from_chat, db, period_id, user.id)

    return {
        "success": True,
        "message": f"Создано {len(posts)} постов",
        "posts": posts,
        "tariff": tariff_check,  # диагностика применённых лимитов тарифа
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
                # FIX (мультикартинки): вложенный список изображений поста
                "images": [
                    {
                        "id": img.id,
                        "image_url": img.image_url,
                        "image_source": img.image_source,
                        "order_index": img.order_index,
                    }
                    for img in p.images
                ],
                "status": p.status,
                "publish_at": p.publish_at.isoformat() if p.publish_at else None,
                "published_at": p.published_at.isoformat() if p.published_at else None
            }
            for p in posts
        ]
    }


# =============================================================================
# Тарифные планы (Admin only): GET/POST /admin/tariffs, DELETE /admin/tariffs/{id}
# =============================================================================

def _serialize_tariff(plan: TariffPlan) -> Dict[str, Any]:
    """Сериализует тарифный план для API-ответа."""
    return {
        "id": plan.id,
        "name": plan.name,
        "cost": plan.cost,
        "max_plan_horizon_days": plan.max_plan_horizon_days,
        "max_rubrics": plan.max_rubrics,
        "max_posts_per_day": plan.max_posts_per_day,
        "max_posts_per_week": plan.max_posts_per_week,
        "max_posts_per_month": plan.max_posts_per_month,
        "max_communities": plan.max_communities,
        "allows_images": plan.allows_images,
        "max_images_per_post": plan.max_images_per_post,
        "allowed_image_sources": plan.allowed_image_sources,
    }


@app.get("/admin/tariffs")
def admin_list_tariffs(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """Список всех тарифных планов (только администратор)."""
    plans = db.query(TariffPlan).order_by(TariffPlan.id.asc()).all()
    return {"tariffs": [_serialize_tariff(p) for p in plans]}


@app.post("/admin/tariffs")
def admin_save_tariff(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    payload: TariffPlanPayload,
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """Создаёт или обновляет тарифный план (только администратор).

    Если payload.id задан и план существует — обновление; иначе создание.
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Название тарифа обязательно")

    plan = None
    if payload.id is not None:
        plan = db.query(TariffPlan).filter(TariffPlan.id == payload.id).first()
        if plan is None:
            raise HTTPException(status_code=404, detail="Тарифный план не найден")
    else:
        plan = TariffPlan(name=name)
        db.add(plan)

    plan.name = name
    plan.cost = float(payload.cost or 0.0)
    plan.max_plan_horizon_days = int(payload.max_plan_horizon_days)
    plan.max_rubrics = int(payload.max_rubrics)
    plan.max_posts_per_day = int(payload.max_posts_per_day)
    plan.max_posts_per_week = int(payload.max_posts_per_week)
    plan.max_posts_per_month = int(payload.max_posts_per_month)
    plan.max_communities = int(payload.max_communities)
    plan.allows_images = bool(payload.allows_images)
    plan.max_images_per_post = int(payload.max_images_per_post)
    plan.allowed_image_sources = payload.allowed_image_sources

    db.commit()
    db.refresh(plan)
    logger.info(f"[admin/tariffs] Сохранён тариф id={plan.id} ({plan.name})")
    return {"success": True, "tariff": _serialize_tariff(plan)}


@app.delete("/admin/tariffs/{tariff_id}")
def admin_delete_tariff(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    tariff_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """Удаляет тарифный план (только администратор).

    У пользователей, привязанных к удалённому плану, tariff_plan_id
    сбрасывается в NULL (FK с ON DELETE SET NULL на уровне миграции может
    отсутствовать в SQLite — делаем явно).
    """
    plan = db.query(TariffPlan).filter(TariffPlan.id == tariff_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Тарифный план не найден")

    db.query(User).filter(User.tariff_plan_id == tariff_id).update(
        {User.tariff_plan_id: None}, synchronize_session=False
    )
    db.delete(plan)
    db.commit()
    logger.info(f"[admin/tariffs] Удалён тариф id={tariff_id}")
    return {"success": True, "deleted_tariff_id": tariff_id}


# =============================================================================
# Тариф пользователя: GET /api/tariff-info (лимиты для фронтенда)
# =============================================================================

@app.get("/api/tariff-info")
def get_tariff_info(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Возвращает тарифный план текущего пользователя и его лимиты.

    Используется фронтендом для ограничения количества картинок в посте
    (max_images_per_post) и списка разрешённых источников
    (allowed_image_sources). Если тариф не назначен — возвращаются
    безопасные дефолты (картинки запрещены).
    """
    user = require_auth(request, db)
    plan = db.query(TariffPlan).filter(TariffPlan.id == user.tariff_plan_id).first() \
        if user.tariff_plan_id else None
    if plan is None:
        return {
            "has_tariff": False,
            "tariff": None,
            "limits": {
                "allows_images": False,
                "max_images_per_post": 0,
                "allowed_image_sources": [],
                # FIX (wizard): поля для пошагового мастера генерации.
                # Без тарифа — безопасные дефолты: все периоды доступны
                # (бэкенд лимиты не проверяет), рубрики/посты не ограничены.
                "max_period_days": None,
                "max_rubrics": None,
                "max_posts_per_week": None,
                "period_options": _wizard_period_options(None, db=db, user_id=user.id),
                "image_source_options": _wizard_image_source_options(False, []),
            },
        }
    # единый безопасный парсер JSON-строки allowed_image_sources (tariff_service)
    sources = _parse_allowed_sources(plan)
    return {
        "has_tariff": True,
        "tariff": _serialize_tariff(plan),
        "limits": {
            "allows_images": plan.allows_images,
            "max_images_per_post": plan.max_images_per_post or 0,
            "allowed_image_sources": sources,
            # FIX (wizard): лимиты для пошагового мастера настройки генерации.
            # max_period_days — дальность планирования (в UI-требованиях поле
            # называлось max_period_days; в модели — max_plan_horizon_days).
            "max_period_days": plan.max_plan_horizon_days,
            "max_rubrics": plan.max_rubrics,
            "max_posts_per_week": plan.max_posts_per_week,
            "max_posts_per_month": plan.max_posts_per_month,
            # Готовые описания опций для фронтенда (мастер строит UI без
            # дублирования тарифной логики на JS):
            "period_options": _wizard_period_options(plan, db=db, user_id=user.id),
            "image_source_options": _wizard_image_source_options(
                plan.allows_images, sources
            ),
        },
    }


# =============================================================================
# Пошаговый мастер (Wizard) настройки генерации контент-плана
# =============================================================================
# Мастер состоит из 4 шагов: горизонт -> частота/объём -> рубрики -> медиа/стиль.
# Принцип «мягких ограничений» (soft caps): фронтенд получает полные описания
# опций и подсвечивает превышения предупреждением вместо жёсткой блокировки
# ввода; финальный POST /wizard/generate всё равно валидирует настройки через
# tariff_service — это защита от прямого вызова API в обход UI (ошибка 403 с
# понятным русскоязычным текстом возвращается до обращения к LLM).

# Человеческие названия источников изображений для UI мастера.
WIZARD_IMAGE_SOURCES = [
    ("ai", "🤖 AI-генерация"),
    ("web_search", "🔎 Поиск в web"),
    ("user_upload", "📤 Мои загрузки"),
]

# Названия типов периодов для сводки/сообщений мастера.
WIZARD_PERIOD_LABELS = {
    "week": "1 неделя",
    "two_weeks": "2 недели",
    "month": "1 месяц",
}


def _wizard_period_options(
    plan: Optional[TariffPlan],
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Описывает карточки периода для Шага 1 мастера по правилам тарифа.

    Возвращает список [{value, label, days, posts, available, upgrade_hint}].
    Если план None или период короче/равен max_plan_horizon_days — карточка
    доступна; иначе frontend показывает 🔒 и тултип «Доступно в тарифе …».

    FIX (wizard): подзаголовок карточек («N дней · до M постов») больше НЕ
    берётся из статичных дефолтов PERIOD_INFO — объём M считается по активному
    тарифу пользователя:
      - недельный объём ограничен max_posts_per_week;
      - общий объём периода дополнительно режется месячным лимитом
        max_posts_per_month;
      - если у пользователя уже есть активный ContentPlanPeriod нужного типа,
        показываем фактическое число постов этого плана (то, что реально
        будет сгенерировано).
    """
    options: List[Dict[str, Any]] = []
    labels = {"week": "📅 Неделя", "two_weeks": "📆 2 недели", "month": "🗓️ Месяц"}
    horizon = int(plan.max_plan_horizon_days) if plan is not None else None

    # Фактическое число постов активного периода пользователя по типу плана.
    # Нет БД/пользователя или нет активного плана — словарь пустой, используем
    # расчётные значения из лимитов тарифа.
    actual_posts: Dict[str, int] = {}
    if db is not None and user_id is not None:
        try:
            active = (
                db.query(ContentPlanPeriod)
                .filter(
                    ContentPlanPeriod.user_id == user_id,
                    ContentPlanPeriod.status == "active",
                )
                .order_by(ContentPlanPeriod.start_date.desc())
                .first()
            )
            if active is not None and active.period_type in ("week", "two_weeks", "month"):
                # total_posts в модели отсутствует — считаем posts этого плана
                # отдельным запросом (Post.content_plan_period_id).
                real = (
                    db.query(sa_func.count(Post.id))
                    .filter(Post.content_plan_period_id == active.id)
                    .scalar()
                )
                if real:
                    actual_posts[active.period_type] = int(real)
        except Exception as e:  # noqa: BLE001 — диагностика не должна ломать API
            logger.warning("wizard: не удалось прочитать активный период: %s", e)

    def _capped_posts(value: str) -> int:
        """Объём периода по активному тарифу (soft-лимиты планов PERIOD_INFO)."""
        info = _TARIFF_PERIOD_INFO[value]
        posts = int(info["posts"])
        if plan is not None:
            # Сколько постов реально поместится за дни периода при частоте
            # тарифа (ceil на случай неполной недели в периоде «месяц»).
            per_week = int(plan.max_posts_per_week or 0)
            if per_week > 0:
                weeks = math.ceil(info["days"] / 7)
                posts = min(posts, per_week * weeks)
            per_month = int(plan.max_posts_per_month or 0)
            if per_month > 0:
                posts = min(posts, per_month)
        # Активный план пользователя точнее расчёта — показываем его цифру.
        real = actual_posts.get(value)
        if real:
            posts = real
        return posts

    for value in ("week", "two_weeks", "month"):
        info = _TARIFF_PERIOD_INFO[value]
        available = True if horizon is None else info["days"] <= horizon
        options.append({
            "value": value,
            "label": labels[value],
            "days": info["days"],
            "posts": _capped_posts(value),
            "available": available,
            # Подсказка для заблокированной карточки: ищем следующий по
            # длине горизонта тариф, в котором этот период уже доступен.
            "upgrade_hint": None if available else _wizard_upgrade_hint(info["days"]),
        })
    return options


def _wizard_upgrade_hint(days: int) -> str:
    """Текст тултипа для недоступного периода: «Доступно в тарифе <Название>».

    Название берём из первого тарифного плана (по возрастанию id), чей
    max_plan_horizon_days покрывает запрошенную дальность. Если такого
    плана нет — общая подсказка обратиться к администратору.
    """
    try:
        from database import SessionLocal

        s = SessionLocal()
        try:
            plan = (
                s.query(TariffPlan)
                .filter(TariffPlan.max_plan_horizon_days >= days)
                .order_by(TariffPlan.max_plan_horizon_days.asc(), TariffPlan.id.asc())
                .first()
            )
        finally:
            s.close()
        if plan is not None:
            return f"Доступно в тарифе «{plan.name}»"
    except Exception as e:  # noqa: BLE001 — подсказка не должна ломать API
        logger.warning("wizard: не удалось подобрать тариф для подсказки: %s", e)
    return "Обратитесь к администратору для смены тарифа"


def _wizard_image_source_options(allows_images: bool, allowed_sources: List[str]) -> List[Dict[str, Any]]:
    """Описывает переключатели источников картинок для Шага 4 мастера.

    Источник доступен, только если тариф разрешает картинки в принципе
    (allows_images) И источник перечислен в allowed_image_sources.
    """
    allowed = set(allowed_sources or [])
    return [
        {
            "value": value,
            "label": label,
            "available": bool(allows_images) and value in allowed,
        }
        for value, label in WIZARD_IMAGE_SOURCES
    ]


class WizardGenerateRequest(BaseModel):
    """Тело запроса мастера: настройки 4 шагов генерации контент-плана."""

    period_type: str                      # Шаг 1: week | two_weeks | month
    posts_per_week: Optional[int] = None  # Шаг 2: желаемая частота постов
    rubrics: List[str] = []               # Шаг 3: названия рубрик (chips)
    image_sources: List[str] = []         # Шаг 4: ai | web_search | user_upload
    style_notes: Optional[str] = None     # Шаг 4: дополнительные пожелания к стилю
    community_id: Optional[int] = None    # сообщество (UserCommunity.id), как в period/create


def _validate_wizard_settings(
    db: Session, user: User, payload: WizardGenerateRequest
) -> Dict[str, Any]:
    """Валидирует настройки мастера через лимиты тарифа ДО запуска генерации.

    Бросает HTTPException с понятным русским текстом:
      400 — некорректные значения (неизвестный период/источник, пустые рубрики);
      403 — превышены лимиты тарифа (горизонт, посты/неделю, рубрики, картинки).
    Если тариф не назначен — ограничения не применяются (как в tariff_service),
    но синтаксическая корректность значений проверяется всегда.

    Returns:
        dict с фактами проверки (тарифная диагностика + нормализованные поля).
    """
    period_type = (payload.period_type or "").strip().lower()
    if period_type not in _TARIFF_PERIOD_INFO:
        raise HTTPException(
            status_code=400,
            detail="Недопустимый период. Доступно: week, two_weeks, month",
        )

    # --- Шаг 3: рубрики (нормализация + лимит max_rubrics) ------------------
    rubrics: List[str] = []
    seen = set()
    for raw in payload.rubrics or []:
        name = str(raw).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue  # дубликты не считаем за отдельные рубрики
        seen.add(key)
        rubrics.append(name[:120])

    # --- Шаг 4: источники изображений ---------------------------------------
    valid_sources = {value for value, _ in WIZARD_IMAGE_SOURCES}
    image_sources: List[str] = []
    for raw in payload.image_sources or []:
        src = str(raw).strip()
        if src and src not in valid_sources:
            raise HTTPException(
                status_code=400,
                detail=f"Неизвестный источник изображений '{src}'. "
                       f"Допустимо: {sorted(valid_sources)}",
            )
        if src and src not in image_sources:
            image_sources.append(src)

    plan = _get_user_tariff(db, user)
    if plan is not None:
        # Лимит рубрик: блокируем и на фронтенде (disabled кнопки [+ Добавить]),
        # но серверная проверка обязательна — защита от прямого вызова API.
        max_rubrics = int(plan.max_rubrics or 0)
        if len(rubrics) > max_rubrics:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Превышен лимит тарифного плана «{plan.name}»: "
                    f"можно не более {max_rubrics} рубрик, добавлено {len(rubrics)}. "
                    f"Удалите лишние рубрики или повысьте тариф."
                ),
            )
        # Разрешённые источники картинок: запрещённые тарифом источники
        # (на фронтенде — 🔒) не должны пройти и здесь.
        if image_sources:
            allowed = set(_parse_allowed_sources(plan)) if plan.allows_images else set()
            forbidden = [s for s in image_sources if s not in allowed]
            if forbidden:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Тарифный план «{plan.name}» не разрешает источники "
                        f"изображений: {', '.join(forbidden)}. "
                        f"Разрешено: {', '.join(sorted(allowed)) or 'нет'}. "
                        f"Снимите отметки или повысьте тариф."
                    ),
                )

    # Горизонт периода и количество постов — через общий тарифный сервис
    # (check_tariff_generation_limits бросает 403 с готовым текстом ошибки).
    tariff_check = check_tariff_generation_limits(db, user, period_type)

    return {
        "period_type": period_type,
        "posts_per_week": payload.posts_per_week,
        "rubrics": rubrics,
        "image_sources": image_sources,
        "style_notes": (payload.style_notes or "").strip() or None,
        "community_id": payload.community_id,
        "tariff_check": tariff_check,
    }


@app.post("/api/content-manager/wizard/generate")
async def wizard_generate_content_plan(
    payload: WizardGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Мастер настройки генерации: сохраняет контекст и запускает генерацию плана.

    Порядок работы:
      1) Валидация всех настроек мастера через лимиты тарифа (403 — до LLM).
      2) Сохранение выбранных источников картинок в CommunityGenerationContext
         сообщества (если сообщество выбрано) — они сузируют разрешения тарифа
         при гибридном назначении картинок.
      3) Создание/получение периода контент-плана выбранного типа.
      4) Формирование стартового сообщения чата из настроек мастера (рубрики,
         частота, стиль) — чтобы генерация пошла по этим параметрам, и чтобы
         история диалога осталась связной с чатом контент-менеджера.
      5) Генерация плана (синхронный LLM-вызов вынесен в поток).
    """
    user = require_auth(request, db)
    settings = _validate_wizard_settings(db, user, payload)

    # (2) Сообщество: проверяем принадлежность и сохраняем предпочтения картинок
    uc: Optional[UserCommunity] = None
    if settings["community_id"]:
        uc = db.query(UserCommunity).filter(
            UserCommunity.id == settings["community_id"],
            UserCommunity.user_id == user.id,
        ).first()
        if uc is None:
            raise HTTPException(status_code=404, detail="Сообщество не найдено")
        if settings["image_sources"]:
            ctx = get_or_create_generation_context(db, uc.id)
            ctx.image_source_preferences = json.dumps(
                settings["image_sources"], ensure_ascii=False
            )
            db.commit()

    # (3) Период: community_info — как в существующем /period/create
    if uc is not None:
        community_info = json.dumps(
            {
                "period_community_id": uc.id,
                "group_name": uc.group_name or f"Сообщество {uc.group_id}",
                "group_id": uc.group_id,
            },
            ensure_ascii=False,
        )
    else:
        community_info = get_user_community_info(db, user.id)

    period = get_or_create_content_plan_period(
        db=db,
        user_id=user.id,
        period_type=settings["period_type"],
        community_info=community_info,
    )

    # (4) Настройки мастера транслируем в чат как реплику пользователя —
    # generate_content_plan_from_chat строит промпт по истории чата.
    lines = [
        "Настройки из мастера генерации контент-плана:",
        f"— Горизонт: {WIZARD_PERIOD_LABELS.get(settings['period_type'], settings['period_type'])}",
    ]
    if settings["posts_per_week"]:
        lines.append(f"— Частота публикаций: {settings['posts_per_week']} постов(ов) в неделю")
    if settings["rubrics"]:
        lines.append("— Рубрики: " + ", ".join(settings["rubrics"]))
    if settings["image_sources"]:
        src_labels = dict(WIZARD_IMAGE_SOURCES)
        lines.append(
            "— Источники картинок: "
            + ", ".join(src_labels.get(s, s) for s in settings["image_sources"])
        )
    if settings["style_notes"]:
        lines.append("— Стиль и пожелания: " + settings["style_notes"])
    wizard_message = "\n".join(lines)
    add_chat_message(db, period.id, "user", wizard_message)

    # (5) Генерация: event loop не блокируем (requests.post внутри сервиса)
    posts = await asyncio.to_thread(generate_content_plan_from_chat, db, period.id, user.id)

    return {
        "success": True,
        "message": f"Создано {len(posts)} постов",
        "period": {
            "id": period.id,
            "period_type": period.period_type,
            "title": period.title or "",
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "status": period.status,
        },
        "posts": posts,
        "settings": {
            "period_type": settings["period_type"],
            "posts_per_week": settings["posts_per_week"],
            "rubrics": settings["rubrics"],
            "image_sources": settings["image_sources"],
        },
        "tariff": settings["tariff_check"],  # диагностика применённых лимитов
    }


# =============================================================================
# Контекст генерации сообщества: GET/POST /api/community/{community_id}/context
# =============================================================================

def _get_owned_user_community(db: Session, user: User, community_id: int) -> UserCommunity:
    """Возвращает UserCommunity по id, проверяя принадлежность пользователю.

    community_id — это id записи в таблице user_communities (не VK group_id).
    Администратору доступны чужие сообщества (для настройки из панели).
    """
    uc = db.query(UserCommunity).filter(UserCommunity.id == community_id).first()
    if uc is None:
        raise HTTPException(status_code=404, detail="Сообщество не найдено")
    if uc.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Сообщество принадлежит другому пользователю")
    return uc


def _normalize_image_preferences(value: Any) -> Optional[str]:
    """Приводит предпочтения источников картинок к JSON-строке.

    Принимает список ('["ai", "web_search"]') либо готовую JSON-строку;
    возвращает корректную JSON-строку списка строк.
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            # Не JSON — считаем перечислением через запятую: "ai, web_search"
            parsed = [s.strip() for s in raw.split(",") if s.strip()]
        value = parsed
    if isinstance(value, (list, tuple, set)):
        allowed = {"ai", "web_search", "user_upload"}
        items = [str(v).strip() for v in value if str(v).strip()]
        unknown = [v for v in items if v not in allowed]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Недопустимые источники изображений: {unknown}. "
                       f"Допустимы: {sorted(allowed)}",
            )
        return json.dumps(items, ensure_ascii=False)
    raise HTTPException(status_code=400, detail="image_source_preferences должен быть списком или JSON-строкой")


@app.get("/api/community/{community_id}/context")
def get_community_context(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    community_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Возвращает настройки генерации сообщества (создаёт пустые, если нет)."""
    user = require_auth(request, db)
    uc = _get_owned_user_community(db, user, community_id)
    ctx = get_or_create_generation_context(db, uc.id)
    try:
        prefs = json.loads(ctx.image_source_preferences) if ctx.image_source_preferences else []
    except ValueError:
        prefs = []
    return {
        "community_id": uc.id,
        "goal": ctx.goal,
        "community_specifics": ctx.community_specifics,
        "target_audience": ctx.target_audience,
        "current_state_and_what_works": ctx.current_state_and_what_works,
        "image_source_preferences": prefs,
        "is_configured": ctx.is_configured,
        "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else None,
    }


@app.post("/api/community/{community_id}/context")
def save_community_context(
    community_id: int,
    payload: CommunityContextPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Сохраняет настройки генерации сообщества, устанавливая is_configured=True.

    После сохранения контент-менеджер перестаёт задавать стартовые вопросы и
    начинает внедрять контекст в промпт LLM как блок <CONTEXT>...</CONTEXT>.
    """
    user = require_auth(request, db)
    uc = _get_owned_user_community(db, user, community_id)
    ctx = get_or_create_generation_context(db, uc.id)

    if payload.goal is not None:
        ctx.goal = payload.goal.strip() or None
    if payload.community_specifics is not None:
        ctx.community_specifics = payload.community_specifics.strip() or None
    if payload.target_audience is not None:
        ctx.target_audience = payload.target_audience.strip() or None
    if payload.current_state_and_what_works is not None:
        ctx.current_state_and_what_works = payload.current_state_and_what_works.strip() or None
    if payload.image_source_preferences is not None:
        ctx.image_source_preferences = _normalize_image_preferences(
            payload.image_source_preferences
        )

    # Настройки сохранены — диалог первичной настройки считается завершённым.
    ctx.is_configured = True
    db.commit()
    db.refresh(ctx)
    logger.info(f"[community/context] Сохранён контекст сообщества id={uc.id}")

    try:
        prefs = json.loads(ctx.image_source_preferences) if ctx.image_source_preferences else []
    except ValueError:
        prefs = []
    return {
        "success": True,
        "community_id": uc.id,
        "is_configured": ctx.is_configured,
        "context": {
            "goal": ctx.goal,
            "community_specifics": ctx.community_specifics,
            "target_audience": ctx.target_audience,
            "current_state_and_what_works": ctx.current_state_and_what_works,
            "image_source_preferences": prefs,
        },
    }


# =============================================================================
# Изображения: загрузка пользователем и поиск в интернете
# =============================================================================

_UPLOAD_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_UPLOAD_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 МБ


@app.post("/api/upload-image")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Загружает изображение пользователя в data/media/ (multipart/form-data).

    Файл сохраняется под уникальным именем (uuid4 + исходное расширение),
    возвращается веб-путь /media/{filename} — он раздаётся StaticFiles.
    Источник для PostImage.image_source в этом случае — "user_upload".
    """
    require_auth(request, db)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Недопустимый тип файла '{ext or 'без расширения'}'. "
                   f"Разрешены: {sorted(_UPLOAD_ALLOWED_EXTENSIONS)}",
        )

    content = await file.read()  # асинхронное чтение — event loop не блокируется
    if not content:
        raise HTTPException(status_code=400, detail="Пустой файл")
    if len(content) > _UPLOAD_MAX_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Файл слишком большой (макс. 10 МБ)")

    media_dir = Path("data/media")
    media_dir.mkdir(parents=True, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = media_dir / unique_name
    # Запись на диск вынесена в поток — не блокируем event loop.
    await asyncio.to_thread(dest.write_bytes, content)

    url = f"/media/{unique_name}"
    logger.info(f"[upload-image] Сохранён файл {unique_name} ({len(content)} байт)")
    return {"url": url, "filename": unique_name, "size": len(content), "source": "user_upload"}


@app.get("/api/search-images")
async def search_images(
    request: Request,
    query: str = Query(..., min_length=1, description="Поисковый запрос"),
    limit: int = Query(5, ge=1, le=30),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Ищет изображения в интернете (SerpApi, либо mock picsum.photos без ключа).

    Вызов httpx.AsyncClient полностью асинхронный — event loop не блокируется.
    Найденные URL можно передать в POST /api/posts/{id}/images с source="web_search".
    """
    require_auth(request, db)
    urls = await search_images_web(query=query, limit=limit)
    return {"query": query, "limit": limit, "results": urls, "count": len(urls)}


# =============================================================================
# Посты: присвоение изображений и обновление поста (мультикартинки)
# =============================================================================

@app.post("/api/posts/{post_id}/images")
def set_post_images(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    post_id: int,
    payload: PostImagesPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Заменяет список изображений поста (PostImage), упорядочивая по порядку.

    Тело: {"images": [{"url": "...", "source": "ai|web_search|user_upload"}, ...]}
    Первое изображение синхронизируется в post.image_url (обратная совместимость).
    """
    user = require_auth(request, db)

    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")

    _set_post_images(db, post, payload.images)

    # Обратная совместимость: image_url = первая картинка списка.
    if post.images:
        first = post.images[0]
        post.image_url = first.image_url
        if first.image_source:
            post.image_source = first.image_source
    elif payload.images == []:
        post.image_url = None
        post.image_source = None

    db.commit()
    db.refresh(post)
    logger.info(
        f"[posts/images] Пост id={post_id}: установлено изображений: {len(post.images)} "
        f"(пользователь id={user.id})"
    )
    return {"success": True, "post_id": post_id, "images": _serialize_post(post)["images"]}


@app.put("/api/posts/{post_id}")
def update_post(  # FIX: Event loop unblocked — синхронный роут выполняется в threadpool FastAPI
    post_id: int,
    payload: PostUpdatePayload,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Обновляет пост (текст/статус/изображения). Поле images заменяет PostImage-связи.

    При создании поста изображения назначаются отдельно через
    POST /api/posts/{id}/images (генерация создаёт посты без картинок).
    """
    require_auth(request, db)

    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")
    if post.status == "published":
        raise HTTPException(status_code=400, detail="Нельзя редактировать опубликованный пост")

    data = payload.model_dump(exclude_unset=True)
    images_payload = data.pop("images", None)

    for field, value in data.items():
        setattr(post, field, value)

    if images_payload is not None:
        _set_post_images(db, post, images_payload)
        # Синхронизация обратной совместимости: image_url первой картинки.
        if post.images:
            post.image_url = post.images[0].image_url
        elif images_payload == []:
            post.image_url = None

    db.commit()
    db.refresh(post)
    logger.info(f"[posts/update] Обновлён пост id={post_id}")
    return {"success": True, "post": _serialize_post(post)}


@app.post("/api/posts/{post_id}/assign-images")
async def assign_post_images(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Гибридно назначает посту изображения согласно политике его тарифа.

    Логика (services.tariff_service.assign_images_to_post):
      а) allows_images == False (или тарифа нет) — пост остаётся без картинок;
      б) целевое количество — до max_images_per_post;
      в) обход источников в приоритете: user_upload (уже загруженные пользователем
         картинки идут первыми) -> ai (генерация) -> web_search (поиск);
      г) результат сохраняется в PostImage с корректным order_index.

    Синхронная работа с БД и сетевые вызовы генерации/поиска вынесены в поток
    через asyncio.to_thread — event loop не блокируется.
    """
    user = require_auth(request, db)

    post = db.query(Post).filter(Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Пост не найден")
    if post.status == "published":
        raise HTTPException(status_code=400, detail="Нельзя редактировать опубликованный пост")

    plan = _get_user_tariff(db, user)
    assigned = await asyncio.to_thread(
        _assign_images_to_post, db, post, plan, post.topic
    )
    return {
        "success": True,
        "post_id": post_id,
        "tariff": getattr(plan, "name", None),
        "images": assigned,
    }
