from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check():
    """
    Health check endpoint returning static JSON status indicator.
    """
    return {"status": "ok"}
