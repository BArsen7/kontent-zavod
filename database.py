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


def init_db():
    """Инициализация базы данных: миграции и создание всех таблиц."""
    # Импортируем ВСЕ модели здесь, чтобы избежать циклических импортов
    # и чтобы create_all создал недостающие таблицы (например, users по новой схеме).
    import models  # noqa: F401

    with engine.begin() as conn:
        _migrate_users_table(conn)
        _migrate_content_plan_periods_table(conn)
        _migrate_users_is_admin(conn)

    Base.metadata.create_all(bind=engine)

    # Новые колонки добавляем ПОСЛЕ create_all: к этому моменту все таблицы
    # точно существуют (созданы или уже были), а create_all не трогает
    # существующие таблицы с устаревшей схемой.
    with engine.begin() as conn:
        _migrate_block_fields(conn)
