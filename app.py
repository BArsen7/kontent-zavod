from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db


def create_app() -> FastAPI:
    """Создание и настройка приложения FastAPI."""
    
    app = FastAPI(
        title="Autopilot Content",
        description="Личное ведение соцсетей с использованием AI",
        version="1.0.0",
    )

    # Настройка CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # В продакшене указать конкретные домены
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def startup_event():
        """Инициализация базы данных при запуске приложения."""
        init_db()

    @app.get("/")
    async def root():
        """Корневой эндпоинт для проверки работоспособности API."""
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
