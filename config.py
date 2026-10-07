from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из переменных окружения.

    Неизвестные переменные в .env игнорируются (extra="ignore"),
    чтобы наличие зарезервированных или устаревших ключей не ломало запуск.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database settings
    db_path: str = "data/autopilot.db"

    # AI Services
    ollama_url: str = "http://localhost:11434"
    gigachat_key: str = ""
    gigachat_secret: str = ""

    # --- GigaChat API (тариф Premium) ---
    # Шаг 1: полноценная интеграция облачного API GigaChat.
    # Совместимость со старыми ключами .env (GIGACHAT_KEY/GIGACHAT_SECRET):
    # если новые переменные не заданы, используются старые.
    gigachat_client_id: str = ""
    gigachat_client_secret: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_text_model: str = "GigaChat-Pro"
    gigachat_image_model: str = "GigaChat"
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"

    @property
    def effective_gigachat_client_id(self) -> str:
        """Client ID с фолбэком на устаревший GIGACHAT_KEY."""
        return self.gigachat_client_id or self.gigachat_key

    @property
    def effective_gigachat_client_secret(self) -> str:
        """Client Secret с фолбэком на устаревший GIGACHAT_SECRET."""
        return self.gigachat_client_secret or self.gigachat_secret

    # VKontakte settings
    vk_token: str = ""
    vk_group_id: int = 0

    # Telegram settings (зарезервировано под будущего Telegram-бота)
    tg_bot_token: str = ""
    tg_proxy_url: str | None = None

    # Logging
    log_level: str = "INFO"

    @property
    def sync_database_url(self) -> str:
        """Возвращает URL подключения к базе данных SQLite."""
        return f"sqlite:///{self.db_path}"


settings = Settings()
