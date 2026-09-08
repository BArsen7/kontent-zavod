import logging
import requests
from typing import Optional

from config import settings

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
    url = f"{settings.OLLAMA_URL}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False
    }
    
    logger.info(f"Отправка запроса к Ollama (модель: {model})")
    
    try:
        response = requests.post(url, json=payload, timeout=120)
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
        raise ConnectionError(f"Не удалось подключиться к Ollama по адресу {settings.OLLAMA_URL}. Убедитесь, что сервис запущен.")
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
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
) -> str:
    """
    Генерация текста через GigaChat API.
    
    Args:
        prompt: Пользовательский запрос.
        system_prompt: Системная инструкция для модели.
    
    Returns:
        Сгенерированный текст.
    
    Raises:
        RuntimeError: Если не удалось получить токен или сгенерировать ответ.
    """
    if not settings.GIGACHAT_KEY or not settings.GIGACHAT_SECRET:
        raise RuntimeError("GigaChat credentials не настроены в .env файле")
    
    auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    chat_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    # Шаг 1: Получение токена авторизации
    logger.info("Получение токена авторизации GigaChat")
    
    try:
        auth_response = requests.post(
            auth_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "RqUID": "00000000-0000-0000-0000-000000000000"
            },
            data={
                "scope": "GIGACHAT_API_PERS",
                "client_id": settings.GIGACHAT_KEY,
                "client_secret": settings.GIGACHAT_SECRET
            },
            timeout=30
        )
        auth_response.raise_for_status()
        auth_data = auth_response.json()
        access_token = auth_data.get("access_token")
        
        if not access_token:
            logger.error("GigaChat не вернул access_token")
            raise RuntimeError("Не удалось получить access_token от GigaChat")
            
        logger.info("Токен GigaChat успешно получен")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при получении токена GigaChat: {e}")
        raise RuntimeError(f"Ошибка авторизации в GigaChat: {e}")
    
    # Шаг 2: Запрос к Chat Completions
    logger.info("Отправка запроса к GigaChat для генерации текста")
    
    try:
        chat_response = requests.post(
            chat_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}"
            },
            json={
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]
            },
            timeout=120
        )
        chat_response.raise_for_status()
        chat_data = chat_response.json()
        
        choices = chat_data.get("choices", [])
        if not choices:
            logger.warning("GigaChat вернула пустой список choices")
            return ""
            
        text = choices[0].get("message", {}).get("content", "")
        
        if not text:
            logger.warning("GigaChat вернула пустой ответ")
            return ""
            
        logger.info("Успешная генерация текста через GigaChat")
        return text.strip()
        
    except requests.exceptions.Timeout:
        logger.error("Таймаут при запросе к GigaChat")
        raise TimeoutError("Превышено время ожидания ответа от GigaChat")
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при запросе к GigaChat: {e}")
        raise RuntimeError(f"Ошибка генерации текста через GigaChat: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при работе с GigaChat: {e}")
        raise RuntimeError(f"Ошибка генерации текста через GigaChat: {e}")


def generate_text(
    prompt: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    use_local: bool = True
) -> str:
    """
    Переключатель между локальной (Ollama) и облачной (GigaChat) генерацией текста.
    
    Args:
        prompt: Пользовательский запрос.
        system_prompt: Системная инструкция для модели.
        use_local: Если True, используется Ollama, иначе GigaChat.
    
    Returns:
        Сгенерированный текст.
    """
    if use_local:
        logger.info("Используется локальная генерация (Ollama)")
        return generate_text_ollama(prompt, system_prompt)
    else:
        logger.info("Используется облачная генерация (GigaChat)")
        return generate_text_gigachat(prompt, system_prompt)
