"""FastAPI app factory for the dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pulse.web import fmt
from pulse.web.security import JobSlots, refuse_cross_site
from pulse.web.settings import WebSettings
from pulse.web.views import bugs, investigations, launch, messages, overview, pain, queue, reports, runs

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: WebSettings) -> FastAPI:
    app = FastAPI(title="Discord Pulse", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.job_slots = JobSlots()
    app.middleware("http")(refuse_cross_site)
    if settings.allowed_hosts is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        age=fmt.age, utc=fmt.utc, signed=fmt.signed, minutes=fmt.minutes,
        avatar_color=fmt.avatar_color, kind_label=fmt.kind_label,
    )
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (overview, pain, bugs, queue, messages, reports, launch, investigations, runs):
        app.include_router(module.router)
    return app
