import logging
import logging.config
from pathlib import Path
from dotenv import load_dotenv

from security import (
    csrf_origin_middleware,
    security_headers_middleware,
    tighten_env_file_mode,
)

_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_PATH)
tighten_env_file_mode(_ENV_PATH)

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
        "file": {
            "class": "security.PrivateRotatingFileHandler",
            "filename": "/tmp/fathom.log",
            "maxBytes": 5_000_000,
            "backupCount": 2,
            "formatter": "default",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
    "loggers": {
        "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
    },
})

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db, init_ref_cache_db
from routers import tanks, test_results, events, inhabitants, equipment, purchases, issues, goals, observations, chat, import_data, timeline, schedules, plants_hardscape, reference_info, today, home_water, cultures

app = FastAPI(
    title="Fathom",
    description="Smart aquarium tracking",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.middleware("http")(csrf_origin_middleware)
app.middleware("http")(security_headers_middleware)

BASE_DIR = Path(__file__).parent


class NoCacheStaticFiles(StaticFiles):
    """No build step means no versioned/hashed filenames for static assets, so a
    browser's heuristic cache can sit on a stale style.css/app.js for a long time
    after a deploy with no way to know it changed. Force revalidation on every
    request (still cheap: unchanged files get a 304 via ETag/Last-Modified)."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(tanks.router)
app.include_router(test_results.router)
app.include_router(events.router)
app.include_router(inhabitants.router)
app.include_router(equipment.router)
app.include_router(purchases.router)
app.include_router(issues.router)
app.include_router(goals.router)
app.include_router(observations.router)
app.include_router(chat.router)
app.include_router(chat.culture_router)
app.include_router(import_data.router)
app.include_router(timeline.router)
app.include_router(schedules.router)
app.include_router(plants_hardscape.router)
app.include_router(reference_info.router)
app.include_router(today.router)
app.include_router(home_water.router)
app.include_router(cultures.router)


@app.on_event("startup")
async def startup():
    init_db()
    init_ref_cache_db()


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/today")
