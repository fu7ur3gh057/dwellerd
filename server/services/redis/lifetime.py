"""Lifetime Redis — пул + готовый клиент в app.state.

Пока не подключён в основной поток: используется только когда явно понадобится
swap'ить InMemoryBroker на Redis или хранить state между процессами. До этого
момента файл — заготовка с правильной формой; settings/REDIS_URL подключим
вместе с фактической интеграцией.
"""

from fastapi import FastAPI
from redis.asyncio import ConnectionPool, Redis


def init_redis(app: FastAPI, redis_url: str) -> None:
    """Создать пул и долгоживущий Redis-клиент (шарится между всеми компонентами)."""
    app.state.redis_pool = ConnectionPool.from_url(
        redis_url,
        decode_responses=True,
    )
    app.state.redis = Redis(connection_pool=app.state.redis_pool)


async def shutdown_redis(app: FastAPI) -> None:
    """Закрыть клиент и пул."""
    await app.state.redis.aclose()
    await app.state.redis_pool.disconnect()
