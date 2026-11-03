"""App factory.  Run:  uvicorn licserver.main:app --host 127.0.0.1 --port 8600"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import admin, auth, billing, licenses, portal
from .config import Settings
from .keys import Signer
from .models import Database
from .web import RateLimiter, render

__version__ = "0.1.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = FastAPI(title=f"{settings.product_name} License Server", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.db_path)
    app.state.signer = Signer(settings.private_key_path)
    app.state.login_limiter = RateLimiter(settings.login_rate_limit, settings.login_rate_window)

    app.include_router(licenses.router)
    app.include_router(billing.router)
    app.include_router(auth.router)
    app.include_router(portal.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        # Redirect-style exceptions from require_account carry a Location header.
        if exc.status_code in (302, 303, 307) and exc.headers and "Location" in exc.headers:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(exc.headers["Location"], status_code=exc.status_code)
        wants_html = "text/html" in request.headers.get("accept", "") and not request.url.path.startswith(("/v1", "/webhooks"))
        if wants_html:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return render(request, "message.html", status_code=exc.status_code,
                          title=f"Error {exc.status_code}", body=detail.get("message", ""))
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    return app


_APP: FastAPI | None = None


def __getattr__(name: str):
    # `uvicorn licserver.main:app` resolves this lazily, so importing the module
    # (tests, scripts) does not open the production DB or read the signing key.
    global _APP
    if name == "app":
        if _APP is None:
            _APP = create_app()
        return _APP
    raise AttributeError(name)
