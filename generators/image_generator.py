"""Генерация изображений: GigaChat Premium (основной метод) и Kandinsky.

Основной путь — нативная генерация картинок моделью GigaChat через
``services.gigachat_client.GigaChatClient`` (requests, кэш OAuth-токена):

    POST /chat/completions c "function_call": "auto"  (таймаут 90 сек)
        -> ответ вида <img src="<file_id>" fuse="true"/>
    GET  /files/{file_id}/content                     (таймаут 30 сек)
        -> бинарные JPG-данные сохраняются в data/media/<uuid>.jpg

Kandinsky (FusionBrain API) оставлен как альтернативный провайдер; устаревшая
логика отдельного получения токена для картинок удалена — авторизация целиком
инкапсулирована в GigaChatClient с переиспользованием закэшированного токена.
"""

import logging
import time
import uuid
from pathlib import Path

import requests

from config import settings
from services.gigachat_client import is_gigachat_configured

logger = logging.getLogger(__name__)

# Базовый URL для FusionBrain API (Kandinsky)
FUSIONBRAIN_API_URL = "https://api-key.fusionbrain.ai"


def generate_image_gigachat(
    prompt: str,
    save_dir: str = "data/media",
    model: str = "",
) -> str:
    """
    Генерация изображения через облачный GigaChat API (нативная функция рисования).

    Делегирует запрос синхронному GigaChatClient (services/gigachat_client.py):
    клиент получает токен OAuth один раз и переиспользует его из кэша
    (expires_at в миллисекундах + буфер 60 секунд), что особенно эффективно,
    когда тот же экземпляр уже использовался для генерации текста.

    Args:
        prompt: Текстовое описание изображения.
        save_dir: Директория сохранения (создаётся автоматически).
        model: Имя модели (например "GigaChat-Pro"); пусто -> из .env/config.py.

    Returns:
        Путь к сохранённому локальному файлу (data/media/<uuid>.jpg).

    Raises:
        RuntimeError: Если креды не настроены или генерация не удалась.
    """
    if not is_gigachat_configured():
        raise RuntimeError(
            "GigaChat credentials не настроены в .env файле "
            "(GIGACHAT_CLIENT_ID / GIGACHAT_CLIENT_SECRET)"
        )

    from services.gigachat_client import get_gigachat_client

    # Синглтон клиента: OAuth-токен берётся из общего кэша (не перевыпускается
    # при каждом вызове) — эффективно, даже если текст уже генерировался ранее
    client = get_gigachat_client(
        client_id=settings.effective_gigachat_client_id,
        client_secret=settings.effective_gigachat_client_secret,
        verify_ssl=settings.gigachat_verify_ssl,
    )

    # FIX: раньше сюда передавалась ТЕКСТОВАЯ модель (GigaChat-Pro) — у неё нет
    # права image_gen, и API отклонял запрос. Для рисования используется отдельная
    # настройка gigachat_image_model (по умолчанию "GigaChat"); если параметр
    # model не передан явно — берём его из SystemSetting.
    if not (model or "").strip():
        model = (_load_dynamic_image_config().get("gigachat_image_model") or "").strip()

    try:
        return client.generate_image(
            prompt=prompt,
            save_dir=save_dir,
            model=(model or "").strip() or None,
        )
    except RuntimeError as e:
        # GigaChatAuthError / GigaChatAPIError: детали (401/429/5xx, тело ответа
        # сервера Sber) уже залогированы внутри клиента
        logger.error(f"Ошибка генерации изображения через GigaChat: {e}")
        raise
    except Exception as e:
        logger.error(f"Неизвестная ошибка генерации изображения через GigaChat: {e}")
        raise RuntimeError(f"Ошибка генерации изображения через GigaChat: {e}")


# Историческое имя функции (использовалось в более старых вызовах)
generate_gigachat_image = generate_image_gigachat


def generate_kandinsky(
    prompt: str,
    api_key: str,
    secret_key: str,
    save_dir: str = "data/media"
) -> str:
    """
    Генерация изображения через Kandinsky (FusionBrain API).

    Альтернативный провайдер (выбирается в админ-панели: image_provider=kandinsky).

    Args:
        prompt: Текстовое описание изображения.
        api_key: GigaChat Client ID (X-Key FusionBrain).
        secret_key: GigaChat Client Secret (X-Secret FusionBrain).
        save_dir: Директория для сохранения изображений.

    Returns:
        Локальный путь к сохранённому файлу (например, "data/media/uuid.png").

    Raises:
        RuntimeError: Если генерация не удалась.
        TimeoutError: Если превышено время ожидания генерации.
    """
    # Создаём директорию для сохранения, если она не существует
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    # Авторизация GigaChat для доступа к Kandinsky: используем общий клиент
    # (токен получается один раз и кэшируется внутри экземпляра — раньше здесь
    # был отдельный запрос /oauth при каждом вызове, что упиралось в лимиты).
    try:
        from services.gigachat_client import get_gigachat_client

        client = get_gigachat_client(
            client_id=settings.effective_gigachat_client_id,
            client_secret=settings.effective_gigachat_client_secret,
            verify_ssl=settings.gigachat_verify_ssl,
        )
        access_token = client.get_token()
    except RuntimeError as e:
        logger.error(f"Не удалось получить токен для Kandinsky: {e}")
        raise

    headers = {
        "Content-Type": "application/json",
        "X-Key": f"AccessKey {api_key}",
        "X-Secret": f"SecretKey {secret_key}",
        "Authorization": f"Bearer {access_token}"
    }

    # Шаг 1: Отправка запроса на генерацию
    generation_url = f"{FUSIONBRAIN_API_URL}/key/api/v1/text2image/run"

    logger.info(f"Отправка запроса на генерацию изображения: {prompt[:50]}...")

    try:
        gen_response = requests.post(
            generation_url,
            headers=headers,
            json={
                "modelVersion": "6.0",
                "prompt": prompt,
                "negativePrompt": "",
                "width": 1024,
                "height": 1024
            },
            timeout=60
        )
        gen_response.raise_for_status()
        gen_data = gen_response.json()
        uuid_key = gen_data.get("uuid")

        if not uuid_key:
            logger.error("API не вернуло UUID задачи генерации")
            raise RuntimeError("Не удалось получить UUID задачи генерации")

        logger.info(f"Задача генерации создана, UUID: {uuid_key}")

    except requests.exceptions.Timeout:
        logger.error("Таймаут при отправке запроса на генерацию")
        raise TimeoutError("Превышено время ожидания ответа от API генерации")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при отправке запроса на генерацию: {e}")
        raise RuntimeError(f"Ошибка запуска генерации изображения: {e}")

    # Шаг 2: Поллинг статуса генерации
    status_url = f"{FUSIONBRAIN_API_URL}/key/api/v1/text2image/status/{uuid_key}"
    max_attempts = 60  # Максимальное количество попыток (60 * 3 сек = 180 сек)
    poll_interval = 3  # Интервал между запросами в секундах

    logger.info(f"Начало проверки статуса генерации (максимум {max_attempts} попыток)")

    for attempt in range(1, max_attempts + 1):
        try:
            status_response = requests.get(status_url, headers=headers, timeout=30)
            status_response.raise_for_status()
            status_data = status_response.json()

            status = status_data.get("status", "")
            logger.debug(f"Попытка {attempt}: статус генерации - {status}")

            if status == "DONE":
                logger.info(f"Генерация завершена успешно на попытке {attempt}")

                # Получаем URL изображения
                images = status_data.get("images", [])
                if not images:
                    logger.error("API вернуло статус DONE, но без изображений")
                    raise RuntimeError("Генерация завершена, но изображение не получено")

                image_base64 = images[0]

                # Декодируем и сохраняем изображение
                import base64

                image_data = base64.b64decode(image_base64)
                filename = f"{uuid.uuid4()}.png"
                file_path = save_path / filename

                with open(file_path, "wb") as f:
                    f.write(image_data)

                relative_path = str(file_path.relative_to(Path.cwd())) if file_path.is_absolute() else str(file_path)
                logger.info(f"Изображение сохранено: {relative_path}")
                return relative_path

            elif status == "FAIL":
                logger.error("Генерация завершилась с ошибкой (статус FAIL)")
                raise RuntimeError("Генерация изображения завершилась с ошибкой")

            elif status in ("RUNNING", "PENDING"):
                logger.debug(f"Генерация в процессе (статус: {status}), ожидание {poll_interval} сек...")
                time.sleep(poll_interval)
                continue

            else:
                logger.warning(f"Неизвестный статус генерации: {status}")
                time.sleep(poll_interval)
                continue

        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут при проверке статуса (попытка {attempt})")
            time.sleep(poll_interval)
            continue
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при проверке статуса (попытка {attempt}): {e}")
            time.sleep(poll_interval)
            continue

    # Если цикл завершился, а статус так и не стал DONE
    logger.error(f"Превышено максимальное время ожидания генерации ({max_attempts * poll_interval} сек)")
    raise TimeoutError(f"Превышено время ожидания генерации изображения ({max_attempts * poll_interval} секунд)")


def _load_dynamic_image_config() -> dict:
    """Читает настройки изображений из SystemSetting (без импорта app.py)."""
    try:
        from database import SessionLocal, init_db
        from models import SystemSetting

        db = SessionLocal()
        try:
            rows = db.query(SystemSetting).all()
            return {r.key: r.value for r in rows}
        except Exception as e:  # noqa: BLE001
            # FIX: см. аналогичный фикс в text_generator — при «no such table»
            # пытаемся выполнить миграции и прочитать настройки повторно,
            # иначе провайдер изображений из админ-панели молча игнорируется.
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
                return {}
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Не удалось подключиться к БД для чтения SystemSetting: {e}")
        return {}


def generate_image(
    prompt: str,
    save_dir: str = "data/media",
) -> str:
    """
    Единая точка входа генерации изображений с выбором провайдера.

    Провайдер берётся из таблицы SystemSetting (ключ image_provider):
      - "gigachat"  -> облачный GigaChat API (основной метод):
                       chat/completions + function_call -> /files/{id}/content;
      - "kandinsky" -> Kandinsky (FusionBrain API);
      - пусто/неизвестно -> GigaChat, если креды настроены; иначе Kandinsky.

    Args:
        prompt: Текстовое описание изображения.
        save_dir: Директория для сохранения изображений.

    Returns:
        Локальный путь к сохранённому файлу.
    """
    cfg = _load_dynamic_image_config()
    provider = (cfg.get("image_provider") or "").strip().lower()

    if provider == "gigachat":
        logger.info("Генерация изображения через GigaChat (настройка из админ-панели)")
        # FIX: раньше сюда передавался cfg["gigachat_text_model"] — имя ТЕКСТОВОЙ
        # модели (GigaChat-Pro). Для рисования у него нет права image_gen, и API
        # возвращал ошибку/пустой ответ. Модель изображений — отдельная настройка
        # gigachat_image_model (по умолчанию "GigaChat").
        return generate_image_gigachat(
            prompt=prompt,
            save_dir=save_dir,
            model=cfg.get("gigachat_image_model", ""),
        )

    if provider == "kandinsky":
        logger.info("Генерация изображения через Kandinsky (настройка из админ-панели)")
        return generate_kandinsky(
            prompt=prompt,
            api_key=settings.effective_gigachat_client_id,
            secret_key=settings.effective_gigachat_client_secret,
            save_dir=save_dir,
        )

    # Fallback: GigaChat как основной метод, если креды настроены;
    # иначе — историческое поведение (Kandinsky).
    if is_gigachat_configured():
        logger.info("Генерация изображения через GigaChat (провайдер по умолчанию)")
        return generate_image_gigachat(prompt=prompt, save_dir=save_dir)

    logger.info("Генерация изображения через Kandinsky (креды GigaChat не настроены)")
    return generate_kandinsky(
        prompt=prompt,
        api_key=settings.effective_gigachat_client_id,
        secret_key=settings.effective_gigachat_client_secret,
        save_dir=save_dir,
    )
