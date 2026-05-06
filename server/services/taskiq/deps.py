"""TaskIQ dependency providers.

Tasks (and FastAPI handlers, when reused) get the runtime context — config,
notifiers, check handlers, alert state — via TaskiqDepends. The actual
context is populated once in services.taskiq.lifetime.init_broker and lives
on `broker.state.app_ctx` for the life of the process.
"""

from taskiq import TaskiqDepends, TaskiqState

from services.taskiq.context import AppContext


async def get_app_context(
    state: TaskiqState = TaskiqDepends(),
) -> AppContext:
    return state.app_ctx
