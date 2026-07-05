import os
from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(BASE_DIR, "static", "admin")


NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}


@router.get("/login", response_class=FileResponse)
async def get_login():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"), headers=NO_CACHE_HEADERS)


@router.get("/dashboard", response_class=FileResponse)
async def get_dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"), headers=NO_CACHE_HEADERS)


@router.get("/client", response_class=FileResponse)
async def get_client():
    return FileResponse(os.path.join(STATIC_DIR, "client.html"), headers=NO_CACHE_HEADERS)


@router.get("/knowledge", response_class=RedirectResponse)
@router.get("/knowledge/", response_class=RedirectResponse)
async def redirect_knowledge_to_client(organization_id: str = None):
    url = "/admin/client"
    if organization_id:
        url += f"?organization_id={organization_id}&org_id={organization_id}"
    return RedirectResponse(url=url)


@router.get("/", response_class=RedirectResponse)
@router.get("", response_class=RedirectResponse)
async def redirect_to_login():
    return RedirectResponse(url="/admin/login")
