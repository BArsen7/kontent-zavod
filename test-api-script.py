"""
Тестовый скрипт для проверки интеграции GigaChat API (Premium).
ИСПРАВЛЕНО: Отключена проверка SSL для тестового окружения.
"""

import os
import uuid
import base64
import requests
import urllib3
from dotenv import load_dotenv

# Отключаем предупреждения urllib3 о небезопасных запросах (только для теста!)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Загружаем переменные из .env файла
load_dotenv()

# --- Конфигурация из .env ---
CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID") or os.getenv("GIGACHAT_KEY", "")
CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET") or os.getenv("GIGACHAT_SECRET", "")
SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
TEXT_MODEL = os.getenv("GIGACHAT_TEXT_MODEL", "GigaChat-Pro")
IMAGE_MODEL = os.getenv("GIGACHAT_IMAGE_MODEL", "GigaChat")

# Эндпоинты
AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
TEXT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
IMAGE_URL = "https://gigachat.devices.sberbank.ru/api/v1/images/generations"


def get_auth_token() -> str:
    """Шаг 1: Получение OAuth-токена GigaChat."""
    print("🔄 Шаг 1: Получение токена авторизации...")
    
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("❌ Ошибка: GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET не найдены в .env!")

    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {encoded_credentials}"
    }
    
    payload = {"scope": SCOPE}

    try:
        # ВАЖНО: verify=False отключает проверку SSL-сертификата
        response = requests.post(AUTH_URL, headers=headers, data=payload, timeout=15, verify=False)
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        expires_at = data.get("expires_at")
        print(f"✅ Токен успешно получен! (Действует до timestamp: {expires_at})")
        return token
    except requests.exceptions.HTTPError as e:
        print(f"❌ Ошибка HTTP при получении токена: {e}")
        print(f"Ответ сервера: {response.text}")
        raise
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        raise


def test_text_generation(token: str):
    """Шаг 2: Тест генерации текста."""
    print("\n🔄 Шаг 2: Тест генерации текста...")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": "Ты полезный ассистент, который отвечает кратко и по делу."},
            {"role": "user", "content": "Напиши короткий приветственный пост для блога о тестировании API. Используй эмодзи."}
        ],
        "temperature": 0.7,
        "max_tokens": 300
    }

    try:
        response = requests.post(TEXT_URL, headers=headers, json=payload, timeout=30, verify=False)
        response.raise_for_status()
        data = response.json()
        
        content = data["choices"][0]["message"]["content"]
        print("✅ Текст успешно сгенерирован!")
        print("-" * 50)
        print(content)
        print("-" * 50)
    except requests.exceptions.HTTPError as e:
        print(f"❌ Ошибка HTTP при генерации текста: {e}")
        print(f"Ответ сервера: {response.text}")
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")


import re # Убедитесь, что это есть в начале файла

def test_image_generation(token: str):
    """Шаг 3: Тест генерации изображения через function_call (с увеличенным таймаутом)"""
    print("\n🔄 Шаг 3: Тест генерации изображения (через chat/completions)...")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "model": "GigaChat-Pro", 
        "messages": [
            {"role": "system", "content": "Ты умеешь генерировать изображения по запросу пользователя."},
            {"role": "user", "content": "Нарисуй футуристического робота-программиста за работой, неоновый киберпанк стиль, высокая детализация, 4k"}
        ],
        "function_call": "auto", 
        "temperature": 0.7,
        "max_tokens": 1500
    }

    try:
        print("⏳ Отправка запроса на генерацию...")
        print("⚠️  ВНИМАНИЕ: Генерация картинки может занять 40-90 секунд. Пожалуйста, подождите...")
        
        # УВЕЛИЧИВАЕМ ТАЙМАУТ ДО 90 СЕКУНД
        response = requests.post(TEXT_URL, headers=headers, json=payload, timeout=300, verify=False)
        response.raise_for_status()
        data = response.json()
        
        # 1. Извлекаем контент с тегом img
        content = data["choices"][0]["message"]["content"]
        print(f"📝 Ответ модели получен: {content}")
        
        # 2. Парсим UUID из тега <img src="UUID" fuse="true"/>
        match = re.search(r'<img src="([a-f0-9\-]+)"', content, re.IGNORECASE)
        if not match:
            print("❌ Не удалось найти тег <img> в ответе модели.")
            print(f"Полный ответ для отладки: {content}")
            return
            
        file_id = match.group(1)
        print(f"✅ Идентификатор файла получен: {file_id}")
        
        # 3. Скачиваем изображение по новому эндпоинту
        print("⏳ Скачивание бинарных данных изображения...")
        file_url = f"https://gigachat.devices.sberbank.ru/api/v1/files/{file_id}/content"
        
        download_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "image/jpeg"
        }
        
        # Для скачивания тоже ставим запас по времени (30 сек обычно хватает на скачивание)
        img_response = requests.get(file_url, headers=download_headers, timeout=30, verify=False)
        img_response.raise_for_status()
        
        # 4. Сохраняем файл
        filename = "gigachat_generated_image.jpg"
        with open(filename, "wb") as f:
            f.write(img_response.content)
            
        print(f"🎉 УСПЕХ! Изображение успешно сгенерировано и сохранено как: {os.path.abspath(filename)}")
        print("👉 Откройте этот файл, чтобы проверить результат.")
        
    except requests.exceptions.ReadTimeout:
        print("❌ Ошибка: Превышено время ожидания (90 сек). Сервер Сбера слишком долго генерирует изображение.")
        print("💡 Совет: Попробуйте упростить промпт или повторить запрос позже, когда нагрузка на серверы будет ниже.")
    except requests.exceptions.HTTPError as e:
        print(f"❌ Ошибка HTTP: {e}")
        if 'response' in locals():
            print(f"Ответ сервера: {response.text}")
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")




if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Запуск тестирования GigaChat API (Premium)")
    print(f"📌 Модель текста: {TEXT_MODEL}")
    print(f"📌 Модель изображений: {IMAGE_MODEL}")
    print("⚠️  ВНИМАНИЕ: Проверка SSL отключена (verify=False)")
    print("=" * 60)
    
    try:
        token = get_auth_token()
       # test_text_generation(token)
        test_image_generation(token)
        print("\n🎉 Все тесты завершены!")
    except Exception as e:
        print(f"\n💥 Тестирование прервано с ошибкой: {e}")
