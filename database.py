import os
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


def init_db():
    """Инициализация базы данных: создание всех таблиц."""
    # Импортируем модели здесь, чтобы избежать циклических импортов
    from models import PlatformAccount, ContentPlan, Post, PostStats  # noqa: F401
    
    Base.metadata.create_all(bind=engine)
