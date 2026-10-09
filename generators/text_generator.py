import logging
import requests

from config import settings
from services.gigachat_client import is_gigachat_configured

logger = logging.getLogger(__name__)

# Системный промпт для ниши "3D-формочки для печенья и уютная выпечка"
DEFAULT_SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник, эксперт в области уютной домашней выпечки и 3D-формочек для печенья. "
    "Твой тон — тёплый, задушевный, без пафоса и излишней официальности. "
    "Ты общаешься как подруга, которая любит печь и знает много интересных идей для творчества на кухне. "
    "Избегай сложных терминов, пиши просто и понятно. Твоя цель — вдохновить на творчество и создать ощущение уюта."
)


def generate_text_ollama(
    prompt: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    model: str = "qwen2.5:14b"
) -> str:
    """
    Генерация текста через локальный Ollama API.
    
    Args:
        prompt: Пользовательский запрос.
        system_prompt: Системная инструкция для модели.
        model: Название модели в Ollama.
    
    Returns:
        Сгенерированный текст.
    
    Raises:
        ConnectionError: Если не удалось подключиться к Ollama.
        RuntimeError: Если API вернуло ошибку.
    """
    url = f"{settings.ollama_url}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False
    }
    
    logger.info(f"Отправка запроса к Ollama (модель: {model})")
    
    try:
        # FIX: увеличено время ожидания генерации до 5 минут (300 сек)
        response = requests.post(url, json=payload, timeout=300)
        response.raise_for_status()
        result = response.json()
        text = result.get("response", "")
        
        if not text:
            logger.warning("Ollama вернула пустой ответ")
            return ""
            
        logger.info("Успешная генерация текста через Ollama")
        return text.strip()
        
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Ошибка подключения к Ollama: {e}")
        raise ConnectionError(f"Не удалось подключиться к Ollama по адресу {settings.ollama_url}. Убедитесь, что сервис запущен.")
    except requests.exceptions.Timeout:
        logger.error("Таймаут при запросе к Ollama")
        raise TimeoutError("Превышено время ожидания ответа от Ollama")
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP ошибка от Ollama: {e}")
        raise RuntimeError(f"Ollama вернула ошибку: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при работе с Ollama: {e}")
        raise RuntimeError(f"Ошибка генерации текста через Ollama: {e}")


def generate_text_gigachat(
    prompt: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    temperature: float = 0.7,
) -> str:
    """
    Генерация текста через облачный GigaChat API (тариф Premium).

    Делегирует запрос синхронному GigaChatClient (services/gigachat_client.py,
    requests): OAuth-токен получается один раз и кэшируется в памяти клиента,
    модель настраивается через .env (GIGACHAT_TEXT_MODEL: GigaChat-Pro / GigaChat-Max),
    проверка SSL — через GIGACHAT_VERIFY_SSL.

    Args:
        prompt: Пользовательский запрос.
        system_prompt: Системная инструкция для модели.
        temperature: Температура генерации.

    Returns:
        Сгенерированный текст.

    Raises:
        RuntimeError: Если не удалось получить токен или сгенерировать ответ.
    """
    if not is_gigachat_configured():
        raise RuntimeError(
            "GigaChat credentials не настроены в .env файле "
            "(GIGACHAT_CLIENT_ID / GIGACHAT_CLIENT_SECRET)"
        )

    from services.gigachat_client import get_gigachat_client

    # Общий синглтон клиента: креды и verify_ssl берутся из config.py
    # (settings.gigachat_key/secret -> effective_*, settings.gigachat_verify_ssl),
    # OAuth-токен кэшируется на уровне процесса и переиспользуется между
    # текстовыми и графическими генерациями без повторных запросов /oauth.
    client = get_gigachat_client(
        client_id=settings.effective_gigachat_client_id,
        client_secret=settings.effective_gigachat_client_secret,
        verify_ssl=settings.gigachat_verify_ssl,
    )
    try:
        return client.generate_text(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
        )
    except RuntimeError as e:
        # GigaChatAuthError / GigaChatAPIError (наследники RuntimeError):
        # детальные причины (401/429/5xx, неверный scope) уже залогированы в клиенте
        logger.error(f"Ошибка генерации текста через GigaChat: {e}")
        raise
    except Exception as e:
        logger.error(f"Неизвестная ошибка генерации текста через GigaChat: {e}")
        raise RuntimeError(f"Ошибка генерации текста через GigaChat: {e}")


def generate_text_cloud(
    prompt: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    temperature: float = 0.7,
) -> str:
    """
    Генерация текста через любой OpenAI-совместимый облачный API
    (OpenRouter, OpenAI, DeepSeek, YandexGPT и т.д.).

    Настройки приходят из таблицы SystemSetting (задаются в админ-панели),
    поэтому подключение новой облачной модели не требует изменения кода
    и перезапуска приложения.

    Args:
        prompt: Пользовательский запрос.
        system_prompt: Системная инструкция для модели.
        base_url: Базовый URL API (например https://openrouter.ai/api/v1).
        api_key: Ключ для заголовка Authorization: Bearer <ключ>.
        model: Название модели (например deepseek/deepseek-chat).
        temperature: Температура генерации.

    Returns:
        Сгенерированный текст.

    Raises:
        RuntimeError: Если настройки не заданы или API вернуло ошибку.
    """
    if not base_url or not model:
        raise RuntimeError(
            "Облачная модель не настроена: задайте cloud_api_base_url и "
            "cloud_text_model в админ-панели (Настройки AI)"
        )

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    logger.info(f"Отправка запроса к облачной модели {model} ({url})")

    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
            },
            timeout=300,  # FIX: увеличено время ожидания генерации до 5 минут
        )
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            logger.warning("Облачный API вернул пустой список choices")
            return ""

        text = choices[0].get("message", {}).get("content", "")
        if not text:
            logger.warning("Облачный API вернул пустой ответ")
            return ""

        logger.info("Успешная генерация текста через облачную модель")
        return text.strip()

    except requests.exceptions.Timeout:
        logger.error("Таймаут при запросе к облачной модели")
        raise TimeoutError("Превышено время ожидания ответа облачной модели")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе к облачной модели: {e}")
        raise RuntimeError(f"Ошибка генерации текста через облачную модель: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при работе с облачной моделью: {e}")
        raise RuntimeError(f"Ошибка генерации текста через облачную модель: {e}")


def _load_dynamic_ai_config():
    """Читает настройки AI из SystemSetting напрямую из БД (без импорта app.py)."""
    try:
        from database import SessionLocal, init_db
        from models import SystemSetting

        db = SessionLocal()
        try:
            rows = db.query(SystemSetting).all()
            return {r.key: r.value for r in rows}
        except Exception as e:  # noqa: BLE001
            # FIX: «no such table: system_settings» в старой/битой БД — раньше
            # ошибка молча глоталась ниже и провайдер из админ-панели
            # (например, gigachat) игнорировался: генерация деградировала до
            # локальной Ollama. Пытаемся один раз выполнить миграции и повторить.
            logger.warning(
                f"Не удалось прочитать SystemSetting ({type(e).__name__}: {e}) — "
                "пробую выполнить миграции БД и повторить чтение настроек"
            )
            db.rollback()
            try:
                init_db()
                rows = db.query(SystemSetting).all()
                return {r.key: r.value for r in rows}
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Повторное чтение SystemSetting не удалось: {e2}")
                db.rollback()
                return None
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Не удалось подключиться к БД для чтения SystemSetting: {e}")
        return None


def generate_text(
    prompt: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    use_local: bool = True
) -> str:
    """
    Переключатель провайдеров генерации текста.

    Приоритет выбора:
    1. Таблица SystemSetting (ключ ai_provider) — настраивается в админ-панели:
       - "ollama"        -> локальный Ollama;
       - "cloud"         -> любой OpenAI-совместимый облачный API;
       - "gigachat"      -> полноценный GigaChat Premium (OAuth + /chat/completions);
    2. Fallback по .env: если ai_provider не задан, но в config.py настроены
       GIGACHAT_CLIENT_ID/SECRET и use_local == False — используется GigaChatClient.
    3. Иначе — обратная совместимость: use_local=True -> Ollama, False -> GigaChat.

    Args:
        prompt: Пользовательский запрос.
        system_prompt: Системная инструкция для модели.
        use_local: Фолбэк-переключатель, если ai_provider не настроен в БД.

    Returns:
        Сгенерированный текст.
    """
    cfg = _load_dynamic_ai_config() or {}
    provider = (cfg.get("ai_provider") or "").strip().lower()

    try:
        temp = float(cfg.get("generation_temperature", "0.7"))
    except ValueError:
        temp = 0.7

    if provider == "cloud":
        logger.info("Используется облачная генерация (настройка из админ-панели)")
        return generate_text_cloud(
            prompt,
            system_prompt,
            base_url=cfg.get("cloud_api_base_url", ""),
            api_key=cfg.get("cloud_api_key", ""),
            model=cfg.get("cloud_text_model", ""),
            temperature=temp,
        )

    if provider == "gigachat":
        # Полноценная интеграция GigaChat Premium: имя модели можно переопределить
        # из админки (ключ gigachat_text_model), иначе берётся из config.py
        logger.info("Используется облачная генерация (GigaChat Premium)")
        from services.gigachat_client import get_gigachat_client

        client = get_gigachat_client()
        gc_model = (cfg.get("gigachat_text_model") or "").strip() or None
        return client.generate_text(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temp,
            model=gc_model,
        )

    if provider == "ollama":
        logger.info("Используется локальная генерация (Ollama, настройка из админ-панели)")
        return generate_text_ollama(prompt, system_prompt)

    # ai_provider не настроен в БД: fallback на настройки из .env
    if not use_local and is_gigachat_configured():
        logger.info("Используется облачная генерация (GigaChat, креды из .env)")
        return generate_text_gigachat(prompt, system_prompt, temperature=temp)

    # Обратная совместимость: поведение до появления SystemSetting
    if use_local:
        logger.info("Используется локальная генерация (Ollama)")
        return generate_text_ollama(prompt, system_prompt)
    else:
        logger.info("Используется облачная генерация (GigaChat)")
        return generate_text_gigachat(prompt, system_prompt, temperature=temp)
