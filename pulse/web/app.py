"""FastAPI app factory for the dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pulse.web import fmt
from pulse.web.settings import WebSettings
from pulse.web.views import overview

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: WebSettings) -> FastAPI:
    app = FastAPI(title="Discord Pulse", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        age=fmt.age, utc=fmt.utc, signed=fmt.signed, minutes=fmt.minutes,
        avatar_color=fmt.avatar_color, kind_label=fmt.kind_label,
    )
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (overview,):
        app.include_router(module.router)
    return app
