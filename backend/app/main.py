"""Alpha Markets — NFL quantitative market research.

Run locally:  ./run.sh          (from the repository root)
Or directly:  uvicorn app.main:app --reload --port 8000   (from backend/)

The API layer is deliberately thin. Every endpoint delegates to app.engine, which is the
same code path the background jobs and the backtester use — so the dashboard, the recorded
track record and the backtest can never disagree about what the model said.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import auth
from app.api import routes_research, routes_system, routes_trading
from app.config import settings
from app.core.errors import AlphaError
from app.core.logging import configure, get_logger
from app.jobs import scheduler
from app.tracking import store

configure(settings.LOG_LEVEL)
log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Endpoints reachable without an account. All research is readable by anyone — projections,
# prices, strategies and backtests are the product, not a paywalled extra. Anything that
# touches money or a specific user's data requires a session, because it is meaningless
# without an identity to attach it to.
PUBLIC_PATHS = {
    "/api/health", "/api/login", "/api/signup", "/api/logout", "/api/me",
    "/api/status/jobs",
}
PUBLIC_PREFIXES = ("/api/slate", "/api/games", "/api/predictions", "/api/markets",
                   "/api/market-movers", "/api/strategies", "/api/model",
                   "/api/parlays", "/api/opportunities", "/api/backtest")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    auth.migrate_legacy_users()
    scheduler.start()
    log.info("%s %s ready (season %s)", settings.APP_NAME, settings.VERSION,
             settings.SEASON)
    yield
    await scheduler.stop()


app = FastAPI(title=settings.APP_NAME, version=settings.VERSION,
              description=settings.APP_TAGLINE, lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.middleware("http")
async def identify_user(request: Request, call_next):
    """Attach the session user, and gate anything that is not public research."""
    request.state.user = auth.read_token(request.cookies.get(auth.COOKIE_NAME))
    path = request.url.path
    if path.startswith("/api/") and not request.state.user:
        public = (path in PUBLIC_PATHS
                  or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES))
        if not public:
            return JSONResponse(
                {"error": "unauthorized",
                 "message": "Log in to use portfolio, bankroll and trading features."},
                status_code=401)
    return await call_next(request)


@app.exception_handler(AlphaError)
async def handle_alpha_error(request: Request, exc: AlphaError):
    """Typed errors become clean JSON with a real status code, not a 500 and a stack trace."""
    if exc.status_code >= 500:
        log.error("%s on %s: %s", exc.code, request.url.path, exc.message)
    return JSONResponse(exc.to_dict(), status_code=exc.status_code)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        {"error": "internal_error",
         "message": "Something went wrong handling that request."}, status_code=500)


app.include_router(routes_system.router)
app.include_router(routes_research.router)
app.include_router(routes_trading.router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """Single-page app: unknown non-API paths render the shell and route client-side."""
    if path.startswith("api/"):
        return JSONResponse({"error": "not_found", "message": f"no endpoint /{path}"},
                            status_code=404)
    candidate = STATIC_DIR / path
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
