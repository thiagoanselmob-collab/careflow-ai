import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.webhook import router as webhook_router
from app.core.config import settings
from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await tenant_db_manager.shutdown_all_pools()
    await session_manager.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Include routers
app.include_router(health_router)
app.include_router(knowledge_router)
app.include_router(webhook_router)

# Mount Static Admin Web Panel
static_dir = os.path.join(os.path.dirname(__file__), "static", "admin")
app.mount("/admin/knowledge", StaticFiles(directory=static_dir, html=True), name="admin_knowledge")


@app.get("/")
def read_root():
    """
    Root endpoint welcome message.
    """
    return {"message": f"Welcome to {settings.app_name}"}
