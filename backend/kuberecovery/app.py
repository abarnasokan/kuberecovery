"""Local HTTP API. Live mode requires a token and explicit scoped credentials."""

import hmac
import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters import DemoAdapter, KubectlAdapter
from .domain import RecoveryError
from .service import RecoveryService
from .store import Store

ROOT = Path(__file__).resolve().parents[2]


class IncidentRequest(BaseModel):
    request_key: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class ApprovalRequest(BaseModel):
    plan_digest: str = Field(min_length=64, max_length=64)


class ScenarioRequest(BaseModel):
    scenario: Literal["readiness_probe", "image_pull", "crash_loop"]


def create_app(service=None, token=None):
    if service is None:
        mode = os.getenv("KUBERECOVERY_MODE", "demo")
        if mode not in {"demo", "live"}:
            raise ValueError("KUBERECOVERY_MODE must be demo or live.")
        data_dir = Path(os.getenv("KUBERECOVERY_DATA_DIR", str(ROOT / ".data"))) / mode
        token = os.getenv("KUBERECOVERY_TOKEN")
        if mode == "live" and (not token or len(token) < 24):
            raise ValueError("Live mode requires KUBERECOVERY_TOKEN with at least 24 characters.")
        adapter = (
            DemoAdapter(data_dir / "cluster.json")
            if mode == "demo"
            else KubectlAdapter(
                os.getenv("KUBERECOVERY_CONTEXT"), os.getenv("KUBERECOVERY_KUBECONFIG")
            )
        )
        if mode == "live":
            adapter.validate_identity()
        service = RecoveryService(
            adapter, Store(data_dir / "incidents.db", recover_interrupted=True), mode
        )
    if service.mode == "live" and (not token or len(token) < 24):
        raise ValueError("Live mode requires a strong API token.")

    app = FastAPI(title="KubeRecovery", version="0.1.0")
    app.state.service = service
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.exception_handler(RecoveryError)
    async def recovery_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    def guard(request: Request, authorization: Optional[str] = Header(default=None)):
        if token:
            actual = (authorization or "").removeprefix("Bearer ")
            if not hmac.compare_digest(actual.encode(), token.encode()):
                raise RecoveryError("Enter the local operator API token.", 401)
        if request.method != "GET":
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if (origin and origin != expected) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                raise RecoveryError("Cross-origin requests are refused.", 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise RecoveryError("Mutations require application/json.", 415)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": service.mode}

    @app.get("/api/overview", dependencies=[Depends(guard)])
    def overview():
        return service.overview()

    @app.post("/api/checkpoints", dependencies=[Depends(guard)])
    def checkpoint():
        return service.checkpoint()

    @app.post("/api/demo/failures", dependencies=[Depends(guard)])
    def inject(body: ScenarioRequest):
        return service.inject(body.scenario)

    @app.post("/api/incidents", dependencies=[Depends(guard)])
    def incident(body: IncidentRequest):
        return service.create(body.request_key)

    @app.post("/api/incidents/{identifier}/approve", dependencies=[Depends(guard)])
    def approve(identifier: str, body: ApprovalRequest):
        return service.approve(identifier, body.plan_digest)

    @app.post("/api/incidents/{identifier}/execute", dependencies=[Depends(guard)])
    def execute(identifier: str):
        return service.execute(identifier)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    app.mount("/static", StaticFiles(directory=str(ROOT / "web")), name="static")
    return app
