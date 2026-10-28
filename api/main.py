"""FastAPI app for the owner dashboard.

    uvicorn api.main:app --port 8000            # API (+ built dashboard at / if dashboard/dist exists)
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import agent.agent  # noqa: F401  LiveKit registers its Google plugin at import; it must happen on the main thread
from agent.tools import build_gate
from api.auth import OwnerAuth
from api.owner_tools import build_owner_tools
from api.routes import build_router
from api.simulate import Simulator
from booking.confirm import ConfirmationStore
from booking.db import default_db_path
from booking.service import BookingService

DIST = Path(__file__).resolve().parents[1] / "dashboard" / "dist"


def create_app(
    db_path: Path | str | None = None,
    owner_password: str | None = None,
    clock: Callable[[], datetime] | None = None,
    serve_dashboard: bool = True,
) -> FastAPI:
    db_path = db_path or default_db_path()
    service = BookingService(db_path, clock=clock)
    gate = build_gate(db_path, service=service, confirmations=ConfirmationStore())
    gate.tools.update(build_owner_tools(db_path, service, gate.tools))
    auth = OwnerAuth(owner_password if owner_password is not None else os.environ.get("OWNER_PASSWORD"))

    app = FastAPI(title="VoiceGate API", version="0.3.0")
    app.include_router(build_router(auth, gate, Simulator(db_path, service, gate)))
    if serve_dashboard and DIST.is_dir():
        app.mount("/", StaticFiles(directory=DIST, html=True), name="dashboard")
    return app


load_dotenv()
app = create_app()
