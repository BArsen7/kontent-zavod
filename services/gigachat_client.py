"""Синхронный клиент GigaChat API (тариф Premium) на библиотеке ``requests``.

Реализация строго по спецификации GigaChat Premium:

1. Аутентификация OAuth 2.0::

       POST https://ngw.devices.sberbank.ru:9443/api/v2/oauth
       Headers: Content-Type: application/x-www-form-urlencoded,
                Accept: application/json,
                RqUID: <uuid4>,
                Authorization: Basic base64(client_id:client_secret)
       Body:    scope=GIGACHAT_API_PERS
       Response: {"access_token": "...", "expires_at": 1791450321295}
                 -- timestamp в МИЛЛИСЕКУНДАХ.

   Токен кэшируется внутри экземпляра класса (``self._token`` /
   ``self._expires_at``) и переиспользуется до истечения (с запасом 60 сек),
   чтобы не упереться в лимиты сервиса авторизации.

2. Генерация текста::

       POST {base_url}/chat/completions
       {"model": "GigaChat-Pro", "messages": [...],
        "temperature": 0.7, "max_tokens": 1500}

3. Генерация изображений (нативная, через функцию рисования модели)::

       POST {base_url}/chat/completions
       {"model": "GigaChat-Pro",
        "messages": [{"role": "user", "content": "Нарисуй: ..."}],
        "function_call": "auto"}          # таймаут 90 секунд

       Модель возвращает текст вида:
           <img src="a598ae3d-d3eb-454e-a8bf-0848193a603e" fuse="true"/>
       file_id извлекается регулярным выражением, после чего картинка
       скачивается бинарными данными::

           GET {base_url}/files/{file_id}/content
           Accept: image/jpeg             # таймаут 30 секунд

       и сохраняется в save_dir (по умолчанию "data/media") под уникальным
       именем "<uuid4hex>.jpg". Возвращается путь к сохранённому файлу.

Клиент полностью синхронный (``requests``) — вызывается напрямую из
sync-эндпоинтов FastAPI, планировщика APScheduler и CLI-скриптов, без
event loop и сторонних async-зависимостей.
"""

import base64
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
import urllib3

from config import settings

logger = logging.getLogger(__name__)

# Запас обновления токена: считаем токен протухшим за 60 секунд до expires_at
TOKEN_EXPIRY_BUFFER_MS = 60_000

# Жёсткие таймауты из спецификации интеграции
IMAGE_GENERATION_TIMEOUT = 90   # генерация изображения (chat/completions + function_call)
FILE_DOWNLOAD_TIMEOUT = 30      # скачивание /files/{id}/content
TEXT_TIMEOUT = 120              # генерация текста
AUTH_TIMEOUT = 30               # получение OAuth-токена

# Регулярка для извлечения file_id из ответа модели:
# <img src="a598ae3d-d3eb-454e-a8bf-0848193a603e" fuse="true"/>
IMG_TAG_RE = re.compile(r'<img\s+src="([a-f0-9\-]+)"', re.IGNORECASE)

# Общий (процессный) кэш OAuth-токена: {"token": str|None, "expires_at": int(мс)}
_token_cache: dict = {"token": None, "expires_at": 0}
# Один lock на все экземпляры — атомарное обновление общего кэша
_auth_lock = threading.Lock()


class GigaChatAuthError(RuntimeError):
    """Ошибка авторизации (неверные client_id/secret, scope, блокировка)."""


class GigaChatAPIError(RuntimeError):
    """Ошибка API GigaChat (4xx/5xx, лимиты, сетевые сбои, таймауты)."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GigaChatClient:
    """Синхронный клиент GigaChat (Premium) с кэшированием OAuth-токена."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        base_url: Optional[str] = None,
        oauth_url: Optional[str] = None,
        text_model: Optional[str] = None,
    ):
        # Фолбэк на устаревшие переменные .env: GIGACHAT_KEY / GIGACHAT_SECRET
        self.client_id = client_id or settings.effective_gigachat_client_id
        self.client_secret = client_secret or settings.effective_gigachat_client_secret
        self.scope = scope or settings.gigachat_scope
        self.base_url = (base_url or settings.gigachat_base_url).rstrip("/")
        self.oauth_url = oauth_url or settings.gigachat_oauth_url
        self.text_model = text_model or settings.gigachat_text_model

        if verify_ssl is None:
            verify_ssl = settings.gigachat_verify_ssl
        self.verify_ssl = bool(verify_ssl)
        # Отключаем предупреждения urllib3 при verify_ssl=False,
        # чтобы не засорять логи FastAPI/планировщика (InsecureRequestWarning).
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # --- Кэш токена: на уровне ПРОЦЕССА (модульные `_token`/`_expires_at`) ---
        # Такой кэш переживает пересоздание экземпляра клиента (например, при
        # смене имени модели в админке) и гарантированно соблюдает лимиты
        # сервиса авторизации: токен запрашивается не чаще, чем раз в ~55 минут.
        # Экземпляр хранит ссылки на общий кэш для совместимости тестов.
        self._shared_token_ref = _token_cache
        # Lock: планировщик и потоки FastAPI могут обращаться к клиенту параллельно
        self._lock = _auth_lock

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _log_http_error(self, resp: requests.Response, context: str) -> None:
        """Логирует полный текст ответа Sber для отладки (scope/креды/лимиты)."""
        try:
            body = resp.text
        except Exception:  # noqa: BLE001
            body = "<не удалось прочитать тело ответа>"
        logger.error(
            "GigaChat %s: HTTP %s, тело ответа сервера: %s",
            context,
            resp.status_code,
            body,
        )

    # ------------------------------------------------------------------
    # Аутентификация OAuth 2.0
    # ------------------------------------------------------------------
    def _get_auth_headers(self) -> dict:
        """Заголовки POST /oauth: Basic Auth + form-urlencoded + RqUID."""
        credentials = f"{self.client_id}:{self.client_secret}"
        basic = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # Любой валидный UUID v4 на каждый запрос авторизации
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {basic}",
        }

    @property
    def _token(self) -> Optional[str]:
        return _token_cache["token"]

    @_token.setter
    def _token(self, value: Optional[str]) -> None:
        _token_cache["token"] = value

    @property
    def _expires_at(self) -> int:
        return _token_cache["expires_at"]

    @_expires_at.setter
    def _expires_at(self, value: int) -> None:
        _token_cache["expires_at"] = value

    def _is_token_valid(self) -> bool:
        """Токен валиден, если существует и истекает не раньше, чем через буфер."""
        return bool(_token_cache["token"]) and (
            _token_cache["expires_at"] > self._now_ms() + TOKEN_EXPIRY_BUFFER_MS
        )

    def get_token(self, force_refresh: bool = False) -> str:
        """Возвращает актуальный access_token; при истечении — обновляет и кэширует."""
        if not force_refresh and self._is_token_valid():
            return self._token  # type: ignore[return-value]

        with self._lock:
            # Double-check: возможно, токен уже обновила другая нить
            if not force_refresh and self._is_token_valid():
                return self._token  # type: ignore[return-value]

            if not self.client_id or not self.client_secret:
                raise GigaChatAuthError(
                    "GigaChat credentials не настроены: задайте GIGACHAT_CLIENT_ID "
                    "и GIGACHAT_CLIENT_SECRET в .env"
                )

            logger.info("GigaChat: запрашиваю новый OAuth-токен: %s", self.oauth_url)
            try:
                resp = requests.post(
                    self.oauth_url,
                    headers=self._get_auth_headers(),
                    data={"scope": self.scope},
                    timeout=AUTH_TIMEOUT,
                    verify=self.verify_ssl,
                )
            except requests.exceptions.RequestException as e:
                logger.error("GigaChat OAuth: сетевая ошибка при получении токена: %s", e)
                raise GigaChatAuthError(f"Сетевая ошибка при авторизации GigaChat: {e}") from e

            if resp.status_code != 200:
                self._log_http_error(resp, "OAuth")
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

            # expires_at — timestamp в миллисекундах; фолбэк: +30 минут
            try:
                raw_expires = int(data.get("expires_at") or 0)
            except (TypeError, ValueError):
                raw_expires = 0
            if raw_expires <= self._now_ms():
                raw_expires = self._now_ms() + 30 * 60 * 1000

            self._token = token
            self._expires_at = raw_expires
            logger.info(
                "GigaChat: токен обновлён, истекает %s (raw expires_at=%s)",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw_expires / 1000)),
                data.get("expires_at"),
            )
            return self._token

    # Псевдоним, соответствующий спецификации интеграции
    _get_token = get_token

    def invalidate_token(self) -> None:
        """Сбрасывает кэш токена (например, после получения 401)."""
        with self._lock:
            self._token = None
            self._expires_at = 0

    # ------------------------------------------------------------------
    # Авторизованные запросы к API
    # ------------------------------------------------------------------
    def _authorized_post(self, path: str, body: dict, timeout: float) -> dict:
        """POST {base_url}{path} с Bearer-токеном; авто-повтор один раз при 401."""
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            token = self.get_token(force_refresh=(attempt == 2))
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Client-ID": self.client_id,
            }
            try:
                resp = requests.post(
                    url, headers=headers, json=body,
                    timeout=timeout, verify=self.verify_ssl,
                )
            except requests.exceptions.Timeout as e:
                logger.error("GigaChat %s: таймаут запроса к %s (%s сек)", path, url, timeout)
                raise GigaChatAPIError(f"Таймаут запроса к GigaChat ({path})") from e
            except requests.exceptions.RequestException as e:
                logger.error("GigaChat %s: сетевая ошибка: %s", path, e)
                raise GigaChatAPIError(f"Сетевая ошибка GigaChat ({path}): {e}") from e

            if resp.status_code == 401 and attempt == 1:
                logger.warning(
                    "GigaChat %s: получил 401 — обновляю токен и повторяю запрос", path
                )
                self.invalidate_token()
                continue

            if resp.status_code == 429:
                self._log_http_error(resp, f"{path} (rate limit)")
                raise GigaChatAPIError(
                    "GigaChat: превышен лимит запросов (429)", 429, resp.text
                )

            if resp.status_code >= 400:
                self._log_http_error(resp, path)
                raise GigaChatAPIError(
                    f"GigaChat: ошибка HTTP {resp.status_code}: {resp.text[:500]}",
                    resp.status_code,
                    resp.text,
                )

            try:
                return resp.json()
            except ValueError as e:
                logger.error("GigaChat %s: не-JSON ответ: %s", path, resp.text)
                raise GigaChatAPIError(
                    f"GigaChat: некорректный JSON-ответ ({path})"
                ) from e

        raise GigaChatAuthError(
            "GigaChat: не удалось авторизоваться даже после обновления токена"
        )

    # ------------------------------------------------------------------
    # Генерация текста
    # ------------------------------------------------------------------
    def generate_text(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1500,
        model: Optional[str] = None,
    ) -> str:
        """Генерация текста через POST /chat/completions (GigaChat-Pro / Max).

        Args:
            prompt: Пользовательский запрос.
            system_prompt: Системная инструкция (опционально).
            temperature: Температура генерации.
            max_tokens: Максимум токенов в ответе.
            model: Имя модели; по умолчанию self.text_model (настраиваемо).

        Returns:
            Строка с текстом ответа модели.

        Raises:
            GigaChatAuthError / GigaChatAPIError (RuntimeError).
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
            body["model"], temperature,
        )

        data = self._authorized_post("/chat/completions", body, timeout=TEXT_TIMEOUT)

        choices = data.get("choices", [])
        if not choices:
            logger.warning("GigaChat: пустой список choices, полный ответ: %s", data)
            return ""

        text = (choices[0].get("message") or {}).get("content", "") or ""
        if not text.strip():
            logger.warning("GigaChat: пустой ответ модели, полный ответ: %s", data)
            return ""

        logger.info("GigaChat: текст успешно сгенерирован (%s симв.)", len(text))
        return text.strip()

    # ------------------------------------------------------------------
    # Генерация изображений (нативная: function_call + /files)
    # ------------------------------------------------------------------
    def generate_image(
        self,
        prompt: str,
        save_dir: str = "data/media",
        model: Optional[str] = None,
    ) -> str:
        """Генерация изображения моделью GigaChat (активация функции рисования).

        Шаги:
        1. POST /chat/completions c ``function_call: "auto"`` (таймаут 90 сек);
        2. Извлечение file_id из ``<img src="..." fuse="true"/>`` (regex);
        3. GET /files/{file_id}/content (таймаут 30 сек) — бинарные JPG-данные;
        4. Сохранение в save_dir под именем ``<uuid4hex>.jpg``.

        Args:
            prompt: Текстовое описание изображения.
            save_dir: Каталог сохранения (создаётся автоматически).
            model: Имя модели; по умолчанию self.text_model.

        Returns:
            Путь к сохранённому файлу (например ``data/media/<hex>.jpg``).

        Raises:
            RuntimeError: ошибки API/сети или невозможность сохранить файл.
        """
        chat_body = {
            "model": model or self.text_model,
            "messages": [
                {"role": "user", "content": f"Нарисуй: {prompt}"}
            ],
            "function_call": "auto",
        }

        logger.info("GigaChat: генерация изображения (запрос): %.80s...", prompt)

        data = self._authorized_post(
            "/chat/completions", chat_body, timeout=IMAGE_GENERATION_TIMEOUT
        )

        choices = data.get("choices", [])
        if not choices:
            logger.error("GigaChat images: пустой ответ модели: %s", data)
            raise GigaChatAPIError("GigaChat не вернул ответ генерации изображения")

        content = (choices[0].get("message") or {}).get("content", "") or ""
        match = IMG_TAG_RE.search(content)
        if not match:
            logger.error(
                'GigaChat images: в ответе нет тега <img src="<file_id>" ...>: %s', content
            )
            raise GigaChatAPIError(
                "Модель не вернула изображение (нет <img src=\"...\"> в ответе): "
                f"{content[:300]}"
            )

        file_id = match.group(1)
        logger.info("GigaChat images: получен file_id=%s, скачиваю файл...", file_id)

        # Шаг 3: скачивание бинарных данных изображения
        url = f"{self.base_url}/files/{file_id}/content"
        for attempt in (1, 2):
            token = self.get_token(force_refresh=(attempt == 2))
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/jpg",          # Исправлено согласно док. Сбера
                "X-Client-ID": self.client_id,        # КРИТИЧЕСКИ ВАЖНО: без этого шлюз вернет 403
            }
            try:
                resp = requests.get(
                    url, headers=headers,
                    timeout=FILE_DOWNLOAD_TIMEOUT, verify=self.verify_ssl,
                )
            except requests.exceptions.Timeout as e:
                logger.error("GigaChat files: таймаут скачивания файла %s", file_id)
                raise GigaChatAPIError("Таймаут скачивания изображения GigaChat") from e
            except requests.exceptions.RequestException as e:
                logger.error("GigaChat files: сетевая ошибка скачивания файла: %s", e)
                raise GigaChatAPIError(f"Сетевая ошибка скачивания изображения: {e}") from e

            if resp.status_code == 401 and attempt == 1:
                logger.warning("GigaChat files: 401 при скачивании — обновляю токен")
                self.invalidate_token()
                continue

            if resp.status_code >= 400:
                self._log_http_error(resp, f"/files/{file_id}/content")
                raise GigaChatAPIError(
                    f"GigaChat: ошибка скачивания файла (HTTP {resp.status_code})",
                    resp.status_code,
                    resp.text,
                )
            break

        if not resp.content:
            logger.error("GigaChat files: пустое тело ответа для файла %s", file_id)
            raise GigaChatAPIError("GigaChat вернул пустой файл изображения")

        # Шаг 4: сохранение в save_dir (создаём, если отсутствует)
        try:
            save_path = Path(save_dir)
            save_path.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4().hex}.jpg"
            file_path = save_path / filename
            file_path.write_bytes(resp.content)
        except OSError as e:
            logger.error("GigaChat images: не удалось сохранить файл в %s: %s", save_dir, e)
            raise RuntimeError(f"Ошибка сохранения изображения GigaChat: {e}") from e

        rel_path = str(file_path)
        logger.info(
            "GigaChat images: изображение сохранено: %s (%s байт)",
            rel_path, len(resp.content),
        )
        return rel_path


# ----------------------------------------------------------------------
# Синглтон клиента: токен живёт внутри экземпляра (без глобальных
# переменных с токеном); сам экземпляр создаётся лениво и переиспользуется,
# что даёт эффективное кэширование авторизации для всех генераторов.
# ----------------------------------------------------------------------
_client_instances: dict = {}
_client_lock = threading.Lock()


def get_gigachat_client(**overrides) -> GigaChatClient:
    """Возвращает общий экземпляр GigaChatClient (ленивая инициализация).

    Экземпляры кешируются по ключу (клиент_id, verify_ssl, base_url), поэтому
    повторные вызовы с одинаковыми параметрами (стандартный путь из config.py)
    переиспользуют один и тот же объект и его кэш настроек. Сам OAuth-токен
    живёт в общем процессном кэше ``_token_cache`` и не теряется даже при
    пересоздании клиента.
    """
    probe = GigaChatClient(**overrides)
    key = (probe.client_id, probe.verify_ssl, probe.base_url, probe.oauth_url, probe.scope)
    with _client_lock:
        instance = _client_instances.get(key)
        if instance is None:
            instance = probe
            _client_instances[key] = instance
        return instance


def reset_gigachat_client(clear_token: bool = False) -> None:
    """Сбрасывает кеш экземпляров (смена настроек в админке); опционально — токен."""
    global _client_instances
    with _client_lock:
        _client_instances = {}
    if clear_token:
        with _auth_lock:
            _token_cache["token"] = None
            _token_cache["expires_at"] = 0


def is_gigachat_configured() -> bool:
    """True, если заданы client_id и client_secret (из .env / config.py)."""
    return bool(
        settings.effective_gigachat_client_id
        and settings.effective_gigachat_client_secret
    )
