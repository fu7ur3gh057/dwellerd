"""DB session provider — same function works for TaskIQ tasks and FastAPI
handlers because the engine lives on the module-level broker singleton."""

from typing import AsyncIterator

from sqlmodel.ext.asyncio.session import AsyncSession

from services.taskiq.broker import broker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession bound to the shared engine. Use as:

        # TaskIQ task
        @broker.task
        async def t(session: AsyncSession = TaskiqDepends(get_session)): ...

        # FastAPI handler
        @router.get("/...")
        async def h(session: AsyncSession = Depends(get_session)): ...
    """
    session_maker = broker.state.db_session_maker
    async with session_maker() as session:
        yield session
