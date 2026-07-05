import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

# Configure standard logging to stdout
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.webhook import router as webhook_router
from app.api.admin_agents import router as admin_agents_router
from app.core.config import settings
from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager
from app.services.reminders import start_reminders_task, stop_reminders_task
from app.services.followup import start_followup_task, stop_followup_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_reminders_task()
    await start_followup_task()
    yield
    await stop_followup_task()
    await stop_reminders_task()
    await tenant_db_manager.shutdown_all_pools()
    await session_manager.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Instrument the app to expose /metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# Include routers
app.include_router(health_router)
app.include_router(knowledge_router)
app.include_router(webhook_router)
app.include_router(admin_agents_router)

# Mount Static Admin Web Panel
static_dir = os.path.join(os.path.dirname(__file__), "static", "admin")
app.mount("/admin/knowledge", StaticFiles(directory=static_dir, html=True), name="admin_knowledge")


@app.get("/")
def read_root():
    """
    Root endpoint welcome message.
    """
    return {"message": f"Welcome to {settings.app_name}"}
