"""Сервис поиска изображений в интернете (web search).

Два режима работы (реальные API требуют ключей):
- Режим A (SerpApi Google Images): используется, если задан ключ SERPAPI_KEY.
  Запрос: https://serpapi.com/search.json?engine=google_images&q={query}&num={limit}&api_key={key}
  Из ответа извлекается поле ``image.original`` из массива ``images_results``.
- Режим B (Fallback Mock): если ключа нет — возвращаются демонстрационные URL
  вида https://picsum.photos/seed/{query}/{width}/{height}, чтобы UI работал
  без внешних зависимостей.

Все сетевые вызовы асинхронные (httpx.AsyncClient) — event loop не блокируется.
"""

import logging
import os
from typing import List
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"


def _get_serpapi_key() -> str:
    """Возвращает ключ SerpApi из окружения (пустая строка, если не задан)."""
    return (os.getenv("SERPAPI_KEY") or "").strip()


def _mock_image_urls(query: str, limit: int, width: int = 800, height: int = 600) -> List[str]:
    """Режим B (Fallback Mock): детерминированные демо-URL picsum.photos."""
    safe_query = quote_plus(query.strip() or "content")
    return [
        f"https://picsum.photos/seed/{safe_query}-{i + 1}/{width}/{height}"
        for i in range(max(1, limit))
    ]


def _parse_serpapi_results(payload: dict, limit: int) -> List[str]:
    """Извлекает список прямых URL картинок из ответа SerpApi (image.original)."""
    urls: List[str] = []
    for item in payload.get("images_results", []) or []:
        image = item.get("image") or {}
        url = (image.get("original") or "").strip()
        if url:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


async def search_images_web(query: str, limit: int = 5) -> List[str]:
    """Асинхронно ищет изображения в интернете.

    Args:
        query: поисковый запрос.
        limit: максимальное количество результатов.

    Returns:
        Список URL найденных изображений (мокет-режим, если SERPAPI_KEY не задан
        или запрос к SerpApi завершился ошибкой — UI не должен падать).
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        limit = max(1, min(int(limit), 30))
    except (TypeError, ValueError):
        limit = 5

    api_key = _get_serpapi_key()
    if not api_key:
        # Режим B: нет ключа — mock для демонстрации работы UI.
        logger.debug("SERPAPI_KEY не задан — возврат mock-результатов picsum.photos")
        return _mock_image_urls(query, limit)

    # Режим A: SerpApi Google Images.
    params = {
        "engine": "google_images",
        "q": query,
        "num": limit,
        "api_key": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(SERPAPI_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        # FIX: ошибка внешнего API не должна ронять эндпоинт — fallback на mock.
        logger.warning(f"SerpApi недоступен ({type(e).__name__}: {e}) — fallback на mock-результаты")
        return _mock_image_urls(query, limit)

    if payload.get("error"):
        logger.warning(f"SerpApi вернул ошибку: {payload['error']} — fallback на mock-результаты")
        return _mock_image_urls(query, limit)

    urls = _parse_serpapi_results(payload, limit)
    if not urls:
        logger.info("SerpApi не вернул результатов — fallback на mock")
        return _mock_image_urls(query, limit)
    return urls


def search_images_sync(query: str, limit: int = 5) -> List[str]:
    """Синхронная обёртка поиска изображений для фоновых потоков генерации.

    Предназначена для вызова ИЗ ОТДЕЛЬНОГО ПОТОКА (threadpool-роут FastAPI
    или threading.Thread фоновой генерации) — здесь можно безопасно крутить
    свой event loop. НИКОГДА не вызывать напрямую из async-контекста
    (используйте ``await search_images_web(...)``).
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(search_images_web(query, limit))
    # Мы внутри работающего event loop — создаём отдельный поток с новым loop,
    # чтобы не словать "asyncio.run() cannot be called from a running event loop".
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, search_images_web(query, limit)).result()
