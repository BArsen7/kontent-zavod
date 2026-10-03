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


def init_db():
    """Инициализация базы данных: миграции и создание всех таблиц."""
    # Импортируем ВСЕ модели здесь, чтобы избежать циклических импортов
    # и чтобы create_all создал недостающие таблицы (например, users по новой схеме).
    import models  # noqa: F401

    with engine.begin() as conn:
        _migrate_users_table(conn)
        _migrate_content_plan_periods_table(conn)

    Base.metadata.create_all(bind=engine)
