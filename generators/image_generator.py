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


def _get_gigachat_token() -> str:
    """
    Получение токена авторизации GigaChat для доступа к Kandinsky.
    
    Returns:
        Access token для API.
    
    Raises:
        RuntimeError: Если не удалось получить токен.
    """
    auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    
    if not settings.effective_gigachat_client_id or not settings.effective_gigachat_client_secret:
        raise RuntimeError("GigaChat credentials не настроены в .env файле")
    
    logger.info("Получение токена авторизации для Kandinsky через GigaChat")
    
    try:
        response = requests.post(
            auth_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "RqUID": str(uuid.uuid4())
            },
            data={
                "scope": settings.gigachat_scope,
                "client_id": settings.effective_gigachat_client_id,
                "client_secret": settings.effective_gigachat_client_secret
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        access_token = data.get("access_token")
        
        if not access_token:
            logger.error("GigaChat не вернул access_token")
            raise RuntimeError("Не удалось получить access_token от GigaChat")
            
        logger.info("Токен для Kandinsky успешно получен")
        return access_token
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при получении токена: {e}")
        raise RuntimeError(f"Ошибка авторизации для Kandinsky: {e}")


def generate_gigachat_image(
    prompt: str,
    save_dir: str = "static/uploads",
    size: str = "1024x1024",
) -> str:
    """
    Генерация изображения через облачной GigaChat API (POST /images/generations).

    Если API вернул url — скачиваем его и сохраняем локально (посты в VK
    требуют путь к файлу); если вернул b64_json — клиент сам сохраняет файл.

    Args:
        prompt: Текстовое описание изображения.
        save_dir: Директория для сохранения изображений.
        size: Размер изображения (например "1024x1024").

    Returns:
        Локальный путь к сохранённому файлу.

    Raises:
        RuntimeError: Если креды не настроены или генерация не удалась.
    """
    if not is_gigachat_configured():
        raise RuntimeError(
            "GigaChat credentials не настроены в .env файле "
            "(GIGACHAT_CLIENT_ID / GIGACHAT_CLIENT_SECRET)"
        )

    from services.gigachat_client import get_gigachat_client, run_sync

    client = get_gigachat_client()
    result = run_sync(client.generate_image(prompt=prompt, size=size))

    # Результат может быть внешним URL (response_format=url) — скачиваем локально
    if result.startswith("http://") or result.startswith("https://"):
        try:
            resp = requests.get(result, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Не удалось скачать изображение из GigaChat ({result}): {e}")
            raise RuntimeError(f"Ошибка скачивания изображения GigaChat: {e}")

        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.png"
        file_path = save_path / filename
        file_path.write_bytes(resp.content)
        rel_path = str(file_path)
        logger.info(f"Изображение GigaChat сохранено: {rel_path}")
        return rel_path

    logger.info(f"Изображение GigaChat готово: {result}")
    return result


def generate_kandinsky(
    prompt: str,
    api_key: str,
    secret_key: str,
    save_dir: str = "data/media"
) -> str:
    """
    Генерация изображения через Kandinsky (FusionBrain API).
    
    Args:
        prompt: Текстовое описание изображения.
        api_key: GigaChat Client ID.
        secret_key: GigaChat Client Secret.
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
    
    # Получаем токен авторизации
    try:
        access_token = _get_gigachat_token()
    except RuntimeError as e:
        logger.error(f"Не удалось получить токен: {e}")
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
        from database import SessionLocal
        from models import SystemSetting

        db = SessionLocal()
        try:
            rows = db.query(SystemSetting).all()
            return {r.key: r.value for r in rows}
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Не удалось прочитать SystemSetting из БД: {e}")
        return {}


def generate_image(
    prompt: str,
    save_dir: str = "data/media",
    size: str = "1024x1024",
) -> str:
    """
    Единая точка входа генерации изображений с выбором провайдера.

    Провайдер берётся из таблицы SystemSetting (ключ image_provider):
      - "gigachat"  -> облачный GigaChat API (POST /images/generations),
                       основной метод; креды — из .env / config.py;
      - "kandinsky" -> Kandinsky (FusionBrain API);
      - пусто/неизвестно -> обратная совместимость: Kandinsky.

    Args:
        prompt: Текстовое описание изображения.
        save_dir: Директория для сохранения изображений.
        size: Размер изображения (для GigaChat, например "1024x1024").

    Returns:
        Локальный путь к сохранённому файлу.
    """
    cfg = _load_dynamic_image_config()
    provider = (cfg.get("image_provider") or "").strip().lower()

    if provider == "gigachat":
        logger.info("Генерация изображения через GigaChat (настройка из админ-панели)")
        gc_size = (cfg.get("gigachat_image_size") or size).strip() or size
        return generate_gigachat_image(prompt=prompt, save_dir=save_dir, size=gc_size)

    if provider == "kandinsky":
        logger.info("Генерация изображения через Kandinsky (настройка из админ-панели)")
        return generate_kandinsky(
            prompt=prompt,
            api_key=settings.effective_gigachat_client_id,
            secret_key=settings.effective_gigachat_client_secret,
            save_dir=save_dir,
        )

    # Fallback: историческое поведение — Kandinsky
    logger.info("Генерация изображения через Kandinsky (провайдер по умолчанию)")
    return generate_kandinsky(
        prompt=prompt,
        api_key=settings.effective_gigachat_client_id,
        secret_key=settings.effective_gigachat_client_secret,
        save_dir=save_dir,
    )
