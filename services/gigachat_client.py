"""Асинхронный клиент GigaChat API (тариф Premium).

Реализует:
- OAuth 2.0 авторизацию (POST /oauth) с кэшированием токена в памяти
  экземпляра и автоматическим обновлением до истечения expires_at;
- генерацию текста (POST /chat/completions, модели GigaChat-Pro / GigaChat-Max);
- генерацию изображений (POST /images/generations): если API вернул url —
  возвращаем его, если b64_json — декодируем и сохраняем в static/uploads/.

Все запросы выполняются через httpx.AsyncClient, чтобы не блокировать
event loop FastAPI. Токен инкапсулирован внутри экземпляра класса;
для приложения используется синглтон get_gigachat_client().
"""

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Буфер обновления токена: считаем токен протухшим за 60 секунд до expires_at
TOKEN_EXPIRY_BUFFER_SEC = 60

# Папка для сохранения изображений, полученных в формате b64_json
UPLOADS_DIR = Path("static/uploads")


class GigaChatAuthError(RuntimeError):
    """Ошибка авторизации (неверные client_id/secret, scope и т.п.)."""


class GigaChatAPIError(RuntimeError):
    """Ошибка самого API GigaChat (4xx/5xx, лимиты, сетевые сбои)."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GigaChatClient:
    """Асинхронный клиент GigaChat (Premium) с кэшированием OAuth-токена."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[str] = None,
        base_url: Optional[str] = None,
        text_model: Optional[str] = None,
        image_model: Optional[str] = None,
    ):
        # Фолбэк на устаревшие переменные GIGACHAT_KEY / GIGACHAT_SECRET
        self.client_id = client_id or settings.effective_gigachat_client_id
        self.client_secret = client_secret or settings.effective_gigachat_client_secret
        self.scope = scope or settings.gigachat_scope
        self.base_url = (base_url or settings.gigachat_base_url).rstrip("/")
        self.text_model = text_model or settings.gigachat_text_model
        self.image_model = image_model or settings.gigachat_image_model
        # Проверка SSL (GIGACHAT_VERIFY_SSL=false — только для отладки)
        self.verify_ssl = settings.gigachat_verify_ssl

        # Кэш токена (инкапсулирован в экземпляре, без глобальных переменных)
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0
        # Lock защищает от «thundering herd» — параллельного обновления токена
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Аутентификация
    # ------------------------------------------------------------------
    def _get_auth_headers(self) -> dict:
        """Заголовки для POST /oauth: Basic Auth + form-urlencoded + RqUID."""
        credentials = f"{self.client_id}:{self.client_secret}"
        basic = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # Любой валидный UUID v4 на каждый запрос авторизации
            "RqUID": str(uuid.uuid4()),
        }

    def _is_token_valid(self) -> bool:
        """Токен валиден, если существует и истекает не раньше, чем через буфер."""
        return bool(self._access_token) and (
            self._token_expires_at > int(time.time()) + TOKEN_EXPIRY_BUFFER_SEC
        )

    async def get_token(self, force_refresh: bool = False) -> str:
        """Возвращает актуальный access_token, при необходимости обновляя его.

        Токен кэшируется в памяти экземпляра и переиспользуется до истечения
        expires_at (за вычетом буфера 60 секунд).
        """
        if not force_refresh and self._is_token_valid():
            return self._access_token  # type: ignore[return-value]

        async with self._token_lock:
            # Повторная проверка: возможно, токен уже обновила другая корутина
            if not force_refresh and self._is_token_valid():
                return self._access_token  # type: ignore[return-value]

            if not self.client_id or not self.client_secret:
                raise GigaChatAuthError(
                    "GigaChat credentials не настроены: задайте GIGACHAT_CLIENT_ID "
                    "и GIGACHAT_CLIENT_SECRET в .env"
                )

            url = f"{self.base_url}/oauth"
            logger.info("GigaChat: запрашивающий новый OAuth-токен по адресу %s", url)

            try:
                async with httpx.AsyncClient(timeout=30, verify=self.verify_ssl) as client:
                    resp = await client.post(
                        url,
                        headers=self._get_auth_headers(),
                        data={"scope": self.scope},
                    )
            except httpx.RequestError as e:
                logger.error("GigaChat: сетевая ошибка при получении токена: %s", e)
                raise GigaChatAuthError(f"Сетевая ошибка при авторизации GigaChat: {e}") from e

            if resp.status_code != 200:
                # Логируем полный текст ответа Sber для отладки (scope/креды/блокировка)
                logger.error(
                    "GigaChat OAuth: ошибка %s, тело ответа: %s",
                    resp.status_code,
                    resp.text,
                )
                raise GigaChatAuthError(
                    f"Не удалось получить токен GigaChat (HTTP {resp.status_code}): "
                    f"{resp.text[:500]}"
                )

            try:
                data = resp.json()
            except ValueError as e:
                logger.error("GigaChat OAuth: не-JSON ответ: %s", resp.text)
                raise GigaChatAuthError("OAuth-ответ GigaChat не является JSON") from e

            token = data.get("access_token")
            if not token:
                logger.error("GigaChat OAuth: в ответе нет access_token: %s", resp.text)
                raise GigaChatAuthError("GigaChat не вернул access_token")

            self._access_token = token
            # expires_at — timestamp в секундах; если поле отсутствует — 30 минут
            try:
                self._token_expires_at = int(data.get("expires_at") or 0)
            except (TypeError, ValueError):
                self._token_expires_at = 0
            if self._token_expires_at <= int(time.time()):
                self._token_expires_at = int(time.time()) + 1800

            logger.info(
                "GigaChat: токен обновлён, истекает %s (raw=%s)",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._token_expires_at)),
                data.get("expires_at"),
            )
            return self._access_token

    def invalidate_token(self) -> None:
        """Сбрасывает кэш токена (например, после получения 401)."""
        self._access_token = None
        self._token_expires_at = 0

    async def _authorized_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict,
        timeout: float = 120.0,
    ) -> dict:
        """Выполняет авторизованный запрос к API с повтором при 401.

        Возвращает разобранный JSON. Бросает GigaChatAPIError при ошибках.
        """
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            token = await self.get_token(force_refresh=(attempt == 2))
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Client-ID": self.client_id,
            }
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=self.verify_ssl) as client:
                    resp = await client.request(method, url, headers=headers, json=json_body)
            except httpx.TimeoutException as e:
                logger.error("GigaChat %s: таймаут запроса к %s", path, url)
                raise GigaChatAPIError(f"Таймаут запроса к GigaChat ({path})") from e
            except httpx.RequestError as e:
                logger.error("GigaChat %s: сетевая ошибка: %s", path, e)
                raise GigaChatAPIError(f"Сетевая ошибка GigaChat ({path}): {e}") from e

            if resp.status_code == 401 and attempt == 1:
                # Токен оказался протухшим (например, отозван досрочно) — обновляем и повторяем
                logger.warning("GigaChat %s: получил 401, обновляю токен и повторяю запрос", path)
                self.invalidate_token()
                continue

            if resp.status_code == 429:
                logger.error(
                    "GigaChat %s: превышен лимит запросов (429), тело: %s", path, resp.text
                )
                raise GigaChatAPIError(
                    "GigaChat: превышен лимит запросов (429)", 429, resp.text
                )

            if resp.status_code >= 500:
                logger.error(
                    "GigaChat %s: серверная ошибка %s, тело: %s",
                    path,
                    resp.status_code,
                    resp.text,
                )
                raise GigaChatAPIError(
                    f"GigaChat: серверная ошибка HTTP {resp.status_code}",
                    resp.status_code,
                    resp.text,
                )

            if resp.status_code >= 400:
                logger.error(
                    "GigaChat %s: ошибка клиента %s, тело: %s",
                    path,
                    resp.status_code,
                    resp.text,
                )
                raise GigaChatAPIError(
                    f"GigaChat: ошибка HTTP {resp.status_code}: {resp.text[:500]}",
                    resp.status_code,
                    resp.text,
                )

            try:
                return resp.json()
            except ValueError as e:
                logger.error("GigaChat %s: не-JSON ответ: %s", path, resp.text)
                raise GigaChatAPIError(f"GigaChat: некорректный JSON-ответ ({path})") from e

        # Сюда доходим только если повтор после 401 тоже завершился ошибкой
        raise GigaChatAuthError("GigaChat: не удалось авторизоваться даже после обновления токена")

    # ------------------------------------------------------------------
    # Генерация текста
    # ------------------------------------------------------------------
    async def generate_text(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1500,
        model: Optional[str] = None,
    ) -> str:
        """Генерация текста через POST /chat/completions (GigaChat-Pro/Max).

        Args:
            prompt: Пользовательский запрос.
            system_prompt: Системная инструкция (опционально).
            temperature: Температура генерации.
            max_tokens: Максимум токенов в ответе.
            model: Имя модели; по умолчанию self.text_model (настраиваемо).

        Returns:
            Строка с текстом ответа модели.
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": model or self.text_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        logger.info(
            "GigaChat: генерация текста (модель: %s, температура: %s)",
            body["model"],
            temperature,
        )

        data = await self._authorized_request("POST", "/chat/completions", json_body=body)

        choices = data.get("choices", [])
        if not choices:
            logger.warning("GigaChat: пустой список choices, полный ответ: %s", data)
            return ""

        text = (choices[0].get("message") or {}).get("content", "")
        if not text:
            logger.warning("GigaChat: пустой ответ модели, полный ответ: %s", data)
            return ""

        logger.info("GigaChat: текст успешно сгенерирован (%s симв.)", len(text))
        return text.strip()

    # ------------------------------------------------------------------
    # Генерация изображений
    # ------------------------------------------------------------------
    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        model: Optional[str] = None,
    ) -> str:
        """Генерация изображения через POST /images/generations.

        Если API вернул url — возвращаем его как есть.
        Если вернул b64_json — декодируем, сохраняем файл в static/uploads/
        и возвращаем локальный путь (принятый в проекте формат image_url).

        Returns:
            URL изображения или локальный путь к сохранённому файлу.
        """
        body = {
            "model": model or self.image_model,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
        }

        logger.info(
            "GigaChat: генерация изображения (модель: %s, размер: %s): %.50s...",
            body["model"],
            size,
            prompt,
        )

        data = await self._authorized_request(
            "POST", "/images/generations", json_body=body, timeout=180
        )

        items = data.get("data", [])
        if not items:
            logger.error("GigaChat images: пустое поле 'data', ответ: %s", data)
            raise GigaChatAPIError("GigaChat не вернул изображение", body=data)

        item = items[0]
        url = item.get("url")
        if url:
            logger.info("GigaChat images: получен URL: %.100s", url)
            return url

        b64 = item.get("b64_json")
        if b64:
            try:
                image_bytes = base64.b64decode(b64)
            except Exception as e:
                logger.error("GigaChat images: не удалось декодировать b64_json: %s", e)
                raise GigaChatAPIError("GigaChat вернул некорректный b64_json") from e

            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"gigachat_{uuid.uuid4().hex}.png"
            file_path = UPLOADS_DIR / filename
            file_path.write_bytes(image_bytes)

            rel_path = str(file_path)
            logger.info("GigaChat images: изображение сохранено: %s", rel_path)
            return rel_path

        logger.error("GigaChat images: в ответе нет ни url, ни b64_json: %s", data)
        raise GigaChatAPIError("GigaChat вернул ответ без данных изображения", body=data)


# ----------------------------------------------------------------------
# Синглтон клиента (без глобальных переменных для токена — он внутри
# экземпляра; сам экземпляр создаётся лениво и переиспользуется).
# ----------------------------------------------------------------------
_client_instance: Optional[GigaChatClient] = None


def get_gigachat_client() -> GigaChatClient:
    """Возвращает общий экземпляр GigaChatClient (ленивая инициализация)."""
    global _client_instance
    if _client_instance is None:
        _client_instance = GigaChatClient()
    return _client_instance


def reset_gigachat_client() -> None:
    """Сбрасывает синглтон (используется в тестах и при смене кредов)."""
    global _client_instance
    _client_instance = None


def is_gigachat_configured() -> bool:
    """True, если заданы client_id и client_secret (из .env)."""
    return bool(
        settings.effective_gigachat_client_id
        and settings.effective_gigachat_client_secret
    )


def run_sync(coro):
    """Запускает корутину из синхронного кода (скрипты, sync-эндпоинты FastAPI).

    Безопасно работает и внутри запущенного event loop (через отдельный поток),
    и в обычных скриптах (asyncio.run).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
