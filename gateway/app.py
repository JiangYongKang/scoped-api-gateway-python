"""FastAPI application factory."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gateway.deps import build_services
from gateway.models import VersionStatus
from gateway.routes import install_routes
from gateway.versioning import DEFAULT_VERSION, VERSIONS


def create_app(services: dict | None = None) -> FastAPI:
    app = FastAPI(
        title="Scoped API Gateway (admission layer)",
        version="1.0.0",
        description="Version negotiation, credential/quota control and "
                    "idempotent writes with an auditable decision trail.")
    app.state.services = services or build_services()

    @app.get("/", include_in_schema=False)
    def index():
        return {"service": app.title, "default_version": DEFAULT_VERSION,
                "docs": "/docs"}

    @app.get("/versions")
    def versions():
        """Public, unauthenticated version policy discovery."""
        now = app.state.services["clock"].now()
        payload = {
            "default_version": DEFAULT_VERSION,
            "selection_header": "X-Api-Version",
            "versions": [
                {
                    "version": v.version,
                    "status": v.status.value,
                    "sunset": v.sunset.date().isoformat() if v.sunset else None,
                    "retirement": (v.retirement.date().isoformat()
                                   if v.retirement else None),
                    "successor": v.successor,
                    "still_available": v.status is not VersionStatus.RETIRED,
                }
                for v in VERSIONS.values()
            ],
            "server_time": now.isoformat(),
        }
        return JSONResponse(payload)

    install_routes(app)
    return app
