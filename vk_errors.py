"""
Общий модуль работы с VK API (версия 5.199+).

Содержит:
- единую точку инициализации vk_api.VkApi с явным указанием api_version;
- retry-обёртку с экспоненциальной задержкой для ошибки rate-limit (код 6);
- структурированное логирование специфичных кодов ошибок VK API
  (6, 7, 14, 15 и др.).

Используется во всех компонентах, работающих с VK API:
publishers/vk_publisher.py, managers/community_manager.py, bots/vk_bot.py.
"""
import logging
import time
from typing import Any, Callable, Optional

import vk_api
from vk_api.exceptions import ApiError

logger = logging.getLogger(__name__)

# Актуальная версия VK API, используемая во всём проекте.
VK_API_VERSION: str = "5.199"

# Максимальное количество повторов при ошибкеrate-limit (код 6).
MAX_RETRIES: int = 3

# Базовая задержка между повторами (секунды), удваивается каждый раз.
RETRY_BASE_DELAY: float = 1.0


def create_vk_session(token: str) -> vk_api.VkApi:
    """
    Создаёт сессию VK API с явным указанием актуальной версии API.

    Все серверные компоненты должны использовать сервисный ключ доступа
    сообщества (Community Service Token) с правами: wall, photos, messages, groups.

    Args:
        token: Сервисный ключ доступа сообщества.

    Returns:
        Инициализированный экземпляр vk_api.VkApi с api_version=VK_API_VERSION.
    """
    session = vk_api.VkApi(token=token, api_version=VK_API_VERSION)
    logger.debug(f"Создана VK-сессия (api_version={VK_API_VERSION})")
    return session


def describe_api_error(error: ApiError, scope: str = "") -> str:
    """
    Формирует человекочитаемое описание ошибки VK API c рекомендациями.

    Логирует специфичные коды ошибок:
    - 6  (Too many requests): требует retry с экспоненциальной задержкой;
    - 7  (Permission denied): токен не имеет нужных прав (wall/photos/messages/groups);
    - 14 (Captcha): критическая ошибка — капча недопустима для сервисных токенов;
    - 15 (Access denied): нет доступа к объекту/нет прав в сообществе.

    Args:
        error: Исключение vk_api.exceptions.ApiError.
        scope: Контекст вызова для логов (например, имя метода или группы).

    Returns:
        Строка с описанием ошибки и рекомендацией.
    """
    code: int = getattr(error, "code", 0)
    message: str = str(error)
    prefix = f"[{scope}] " if scope else ""

    if code == 6:
        text = (
            f"{prefix}Too many requests per second (код 6). "
            "Применяется повторной запрос с экспоненциальной задержкой."
        )
        logger.warning(text)
        return text

    if code == 7:
        text = (
            f"{prefix}Permission denied (код 7): у токена отсутствуют необходимые "
            "права. Проверьте, что при создании сервисного ключа сообщества "
            "включены права: wall, photos, messages, groups."
        )
        logger.error(text)
        return text

    if code == 14:
        text = (
            f"{prefix}CAPTCHA needed (код 14): КРИТИЧЕСКАЯ ОШИБКА. Капча недопустима "
            "при работе с сервисным ключом сообщества. Убедитесь, что используется "
            "сервисный токен (Настройки сообщества → Работа с API), а не "
            "пользовательский, и что IP-адрес сервера не заблокирован VK."
        )
        logger.critical(text)
        return text

    if code == 15:
        text = (
            f"{prefix}Access denied (код 15): нет доступа к объекту. Частые причины: "
            "используется пользовательский токен вместо сервисного ключа сообщества; "
            "токен выпущен для другого сообщества; у пользователя нет прав администратора; "
            "не включены соответствующие права ключа (wall/photos/messages)."
        )
        logger.error(text)
        return text

    if code == 5:
        text = (
            f"{prefix}User authorization failed (код 5): токен недействителен, "
            "истёк или отозван. Выпустите новый сервисный ключ доступа сообщества."
        )
        logger.error(text)
        return text

    text = f"{prefix}VK API error (код {code}): {message}"
    logger.error(text)
    return text


def with_retry(
    func: Callable[..., Any],
    *args: Any,
    retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    scope: str = "",
    **kwargs: Any,
) -> Any:
    """
    Выполняет функцию с повторными попытками при ошибке rate-limit (код 6).

    Экспоненциальная задержка: base_delay * 2**attempt секунд.
    Все остальные ошибки VK API логируются через describe_api_error
    и пробрасываются наверх без повтора.

    Args:
        func: Вызываемая функция (например, метод VK API).
        *args: Позиционные аргументы для func.
        retries: Максимальное число повторных попыток.
        base_delay: Базовая задержка в секундах.
        scope: Контекст вызова для логов.
        **kwargs: Именованные аргументы для func.

    Returns:
        Результат выполнения func.

    Raises:
        ApiError: Если ошибка не является rate-limit либо исчерпаны попытки.
    """
    last_error: Optional[ApiError] = None

    for attempt in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except ApiError as e:
            last_error = e
            if getattr(e, "code", 0) == 6 and attempt < retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    f"[{scope or 'vk'}] Rate limit (код 6), попытка {attempt + 1}/{retries}. "
                    f"Повтор через {delay:.1f} сек..."
                )
                time.sleep(delay)
                continue
            # Некритичные для retry ошибки — сразу описываем и пробрасываем.
            describe_api_error(e, scope=scope or "vk")
            raise

    # Теоретически недостижимо, но на случай логической полноты:
    assert last_error is not None
    raise last_error


def owner_id_for_group(group_id: int) -> int:
    """
    Возвращает owner_id для методов VK API, требующих отрицательного ID группы
    (wall.post, фотоальбомы, attachments и т.п.).

    Args:
        group_id: Положительный ID сообщества.

    Returns:
        Отрицательный owner_id сообщества.
    """
    return -abs(int(group_id))


def positive_group_id(group_id: int) -> int:
    """
    Нормализует ID сообщества к положительному виду.

    Внутри системы group_id всегда хранится как положительное число;
    методы вроде groups.getById или VkBotLongPoll требуют именно его.

    Args:
        group_id: ID сообщества (может быть передан отрицательным по ошибке).

    Returns:
        Положительный ID сообщества.
    """
    return abs(int(group_id))
