from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database settings
    db_path: str = "data/autopilot.db"

    # AI Services
    ollama_url: str = "http://localhost:11434"
    gigachat_key: str = ""
    gigachat_secret: str = ""

    # VKontakte settings
    vk_token: str = ""
    vk_group_id: int = 0
    vk_client_id: str = ""
    vk_client_secret: str = ""
    vk_redirect_uri: str = "http://127.0.0.1:8000/auth/vk/callback"

    # Telegram settings
    tg_bot_token: str = ""
    tg_proxy_url: str | None = None

    # Logging
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        """Возвращает URL подключения к базе данных SQLite."""
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def sync_database_url(self) -> str:
        """Возвращает синхронный URL подключения к базе данных SQLite."""
        return f"sqlite:///{self.db_path}"


settings = Settings()
