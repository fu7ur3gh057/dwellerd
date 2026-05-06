"""Top-level API router. Public endpoints (auth, status) live on the bare
router; everything else is gated behind `require_auth` via a sub-router.
"""

from fastapi import APIRouter, Depends

from web.apis.alerts.routes import router as alerts_router
from web.apis.auth.routes import router as auth_router
from web.apis.checks.routes import router as checks_router
from web.apis.config.routes import router as config_router
from web.apis.deps import require_admin, require_auth
from web.apis.docker.routes import router as docker_router
from web.apis.logs.routes import router as logs_router
from web.apis.notifiers.routes import router as notifiers_router
from web.apis.reports.routes import router as reports_router
from web.apis.settings.routes import router as settings_router
from web.apis.status.routes import router as status_router
from web.apis.system.routes import router as system_router
from web.apis.terminal.routes import router as terminal_router
from web.apis.users.routes import router as users_router

api_router: APIRouter = APIRouter()

# Public — no auth needed
api_router.include_router(status_router)
api_router.include_router(auth_router, prefix="/auth")

# Auth required — any logged-in user (admin / staff / viewer).
protected = APIRouter(dependencies=[Depends(require_auth)])
protected.include_router(system_router, prefix="/system")
protected.include_router(checks_router, prefix="/checks")
protected.include_router(alerts_router, prefix="/alerts")
protected.include_router(reports_router, prefix="/reports")
protected.include_router(logs_router, prefix="/logs")
protected.include_router(notifiers_router, prefix="/notifiers")
protected.include_router(config_router, prefix="/config")

# Admin-only — privileged surface area. Staff / viewer get 403 here.
admin = APIRouter(dependencies=[Depends(require_admin)])
admin.include_router(docker_router, prefix="/docker")
admin.include_router(terminal_router, prefix="/terminal")
admin.include_router(users_router, prefix="/users")
admin.include_router(settings_router, prefix="/settings")

api_router.include_router(protected)
api_router.include_router(admin)
