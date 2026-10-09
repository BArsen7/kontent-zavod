from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from config import settings


# Создаем директорию для базы данных, если она не существует
db_path = Path(settings.db_path)
if not db_path.parent.exists():
    db_path.parent.mkdir(parents=True, exist_ok=True)

# Создаем движок SQLAlchemy для SQLite
engine = create_engine(
    settings.sync_database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

# Сессия для работы с базой данных
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Базовый класс для моделей
Base = declarative_base()


def get_db():
    """Генератор сессий базы данных для зависимостей FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_users_table(conn):
    """Миграция таблицы users со старой VK OAuth-схемы на email/пароль.

    Старая схема (legacy VK-логин): vk_id, vk_first_name, vk_last_name,
    vk_photo, access_token. Новая схема: email, password_hash, first_name,
    last_name, photo. Если в таблице users нет ни одной строки, старую
    таблицу безопасно пересоздать под новую схему; иначе данные сохраняются
    как есть и выводится предупреждение (ручной перенос не выполняется).
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return  # таблицы ещё нет — create_all создаст её по новой модели

    columns = {c["name"] for c in inspector.get_columns("users")}
    if "email" in columns and "password_hash" in columns:
        return  # схема уже актуальна

    row_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
    if row_count == 0:
        conn.execute(text("DROP TABLE users"))
        print("[migration] Пустая таблица users удалена — будет создана заново по новой схеме (email/password)")
    else:
        print(
            f"[migration] ВНИМАНИЕ: таблица users содержит {row_count} строк по устаревшей "
            "VK-схеме без колонок email/password_hash. Автоматический перенос не выполнен. "
            "Сделайте резервную копию data/autopilot.db и пересоздайте таблицу вручную либо "
            "удалите БД для чистой инициализации."
        )


def _migrate_content_plan_periods_table(conn) -> None:
    """
    Лёгкая миграция таблицы content_plan_periods.

    Добавляет колонку `title` (пользовательское название черновика для
    переименования), если она отсутствует в существующей базе SQLite.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "content_plan_periods" not in inspector.get_table_names():
        return  # таблицы ещё нет — create_all создаст её по новой модели

    columns = {c["name"] for c in inspector.get_columns("content_plan_periods")}
    if "title" not in columns:
        conn.execute(text("ALTER TABLE content_plan_periods ADD COLUMN title VARCHAR(255)"))
        print("[migration] Добавлена колонка content_plan_periods.title")


def _migrate_users_is_admin(conn) -> None:
    """
    Лёгкая миграция таблицы users.

    Добавляет колонку `is_admin` (права администратора панели), если она
    отсутствует в существующей базе SQLite. Существующие пользователи
    получают значение 0 (False) — права выдаются скриптом
    scripts/make_admin.py.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return  # таблицы ещё нет — create_all создаст её по новой модели

    columns = {c["name"] for c in inspector.get_columns("users")}
    if "is_admin" not in columns:
        conn.execute(
            text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0")
        )
        print("[migration] Добавлена колонка users.is_admin (default False)")


def _add_column_if_missing(conn, table: str, column: str, ddl_type: str) -> None:
    """Безопасно добавляет колонку в существующую таблицу SQLite, если её нет.

    SQLite не поддерживает "ADD COLUMN IF NOT EXISTS", поэтому проверяем
    схему через inspector и выполняем ALTER TABLE только при отсутствии
    колонки. Любые ошибки (например, таблица ещё не создана) перехватываются,
    чтобы миграция никогда не ломала запуск приложения.
    """
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return  # таблицы ещё нет — create_all создаст её по новой модели
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column in columns:
            return
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        print(f"[migration] Добавлена колонка {table}.{column}")
    except Exception as e:  # noqa: BLE001 — миграция не должна ронять приложение
        print(f"[migration] Не удалось добавить {table}.{column}: {type(e).__name__}: {e}")


def _migrate_block_fields(conn) -> None:
    """Миграция блокировок: поля is_blocked/blocked_reason/blocked_at.

    Таблица users: is_blocked, blocked_reason, blocked_at.
    Таблица user_communities: is_blocked, blocked_reason.
    Существующие записи получают is_blocked = 0 (False).
    """
    _add_column_if_missing(conn, "users", "is_blocked", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "users", "blocked_reason", "VARCHAR(500)")
    _add_column_if_missing(conn, "users", "blocked_at", "DATETIME")
    _add_column_if_missing(conn, "user_communities", "is_blocked", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "user_communities", "blocked_reason", "VARCHAR(500)")


def _migrate_posts_table(conn) -> None:
    """
    Лёгкая миграция таблицы posts (аналитика).

    Добавляет колонку `vk_post_id` (ID записи VK в стене из ответа wall.post),
    если она отсутствует в существующей базе SQLite. Без неё SQLAlchemy падает
    с OperationalError «no such column: posts.vk_post_id» при любом SELECT из
    posts (например, GET /api/posts). Колонка nullable — старые посты остаются
    без привязки до первой публикации/импорта метрик.
    """
    _add_column_if_missing(conn, "posts", "vk_post_id", "INTEGER")


def init_db():
    """Инициализация базы данных: миграции и создание всех таблиц.

    FIX: идемпотентность — метод вызывается и из lifespan FastAPI, и напрямую
    при запуске планировщика/скриптов; повторный вызов не должен ронять приложение.
    """
    # Импортируем ВСЕ модели здесь, чтобы избежать циклических импортов
    # и чтобы create_all создал недостающие таблицы (например, users по новой схеме).
    import models  # noqa: F401

    try:
        with engine.begin() as conn:
            _migrate_users_table(conn)
            _migrate_content_plan_periods_table(conn)
            _migrate_users_is_admin(conn)
    except Exception as e:  # noqa: BLE401 — миграция не должна ронять приложение
        print(f"[migration] Пропущен этап пред-миграций: {type(e).__name__}: {e}")

    # FIX: checkfirst=True (дефолт) не спасает, если в БД есть таблицы со СТАРЫМИ
    # схемами (например, system_settings без колонки updated_at) — SQLite тогда
    # падает с OperationalError «no such column», и приложение не стартует.
    # Поэтому создаём таблицы по одной: проблемную таблицу пропускаем с логом,
    # остальные гарантированно будут созданы.
    from sqlalchemy.exc import OperationalError, ProgrammingError

    for table in Base.metadata.sorted_tables:
        try:
            table.create(bind=engine, checkfirst=True)
        except (OperationalError, ProgrammingError) as e:
            print(
                f"[migration] ВНИМАНИЕ: таблица '{table.name}' не проверена/не создана "
                f"из-за расхождения схемы со старой БД: {type(e).__name__}: {e}. "
                "Если приложение использует эту таблицу — сделайте резервную копию "
                "data/autopilot.db и удалите таблицу для пересоздания."
            )

    # Новые колонки добавляем ПОСЛЕ создания таблиц: к этому моменту все таблицы
    # точно существуют (созданы или уже были), а table.create(checkfirst) не трогает
    # существующие таблицы с устаревшей схемой.
    try:
        with engine.begin() as conn:
            _migrate_block_fields(conn)
            # FIX (500 на GET /api/posts, «no such column: posts.vk_post_id»):
            # у существующих БД таблица posts создана до появления аналитики —
            # добавляем недостающую колонку vk_post_id.
            _migrate_posts_table(conn)
    except Exception as e:  # noqa: BLE001 — миграция не должна ронять приложение
        print(f"[migration] Этап миграции блокировок пропущен: {type(e).__name__}: {e}")

    # FIX (падение /admin/settings, «no such table: system_settings»):
    # если таблица system_settings в старой БД создана по устаревшей схеме
    # (например, без обязательной колонки updated_at) — пересоздаём её.
    # Таблица хранит только настройки AI (легко восстанавливаются из админки),
    # но при отсутствии ai_provider генераторы деградируют до локальной Ollama.
    _repair_system_settings_table()


def _repair_system_settings_table() -> None:
    """Пересоздаёт таблицу system_settings, если она отсутствует или имеет
    неполную/устаревшую схему (не хватает колонок key/value/updated_at).

    Данные настроек при этом теряются — они будут восстановлены пользователем
    через страницу «Настройки AI» (или fallback из config.py). Это безопаснее,
    чем молчаливое падение всех роутов настроек и выбор локальной модели
    вместо указанной в админ-панели GigaChat.
    """
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        need_repair = False
        if "system_settings" not in tables:
            # create_all уже должен был её создать; если нет — создаём явно.
            models_table = Base.metadata.tables.get("system_settings")
            if models_table is not None:
                models_table.create(bind=engine, checkfirst=True)
            return
        cols = {c["name"] for c in inspector.get_columns("system_settings")}
        if not {"key", "value", "updated_at"} <= cols:
            need_repair = True

        if need_repair:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE system_settings"))
                Base.metadata.tables["system_settings"].create(bind=engine)
            print(
                "[migration] Таблица system_settings имела неполную схему "
                f"(колонки: {sorted(cols)}) — пересоздана заново. Настройки AI "
                "нужно задать повторно на странице /admin/settings."
            )
    except Exception as e:  # noqa: BLE001 — миграция не должна ронять приложение
        print(f"[migration] Не удалось проверить/пересоздать system_settings: {type(e).__name__}: {e}")
