import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db
from routers import tanks, test_results, events, inhabitants, equipment, purchases, issues, observations, chat, import_data, timeline, schedules, plants_hardscape

app = FastAPI(title="Fathom", description="Smart aquarium tracking")

BASE_DIR = Path(__file__).parent

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(tanks.router)
app.include_router(test_results.router)
app.include_router(events.router)
app.include_router(inhabitants.router)
app.include_router(equipment.router)
app.include_router(purchases.router)
app.include_router(issues.router)
app.include_router(observations.router)
app.include_router(chat.router)
app.include_router(import_data.router)
app.include_router(timeline.router)
app.include_router(schedules.router)
app.include_router(plants_hardscape.router)


@app.on_event("startup")
async def startup():
    init_db()


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/tanks")
