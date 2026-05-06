"""Dependencies для Redis (FastAPI + TaskIQ).

Импортируй get_redis в API-эндпоинтах или TaskIQ-задачах:
    redis: Redis = Depends(get_redis)        # FastAPI
    redis: Redis = TaskiqDepends(get_redis)  # TaskIQ
"""

from redis.asyncio import ConnectionPool, Redis
from starlette.requests import Request
from taskiq import TaskiqDepends


async def get_redis_pool(
    request: Request = TaskiqDepends(),
) -> ConnectionPool:
    """Пул соединений — для случаев, когда нужен явно свой клиент."""
    return request.app.state.redis_pool


async def get_redis(
    request: Request = TaskiqDepends(),
) -> Redis:
    """Готовый Redis-клиент, шарится приложением. Используй это по умолчанию."""
    return request.app.state.redis
