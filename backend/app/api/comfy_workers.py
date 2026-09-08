"""Read-only render fleet status; configuration is supplied through the env file."""
from fastapi import APIRouter, HTTPException

from ..core.comfy.client import ComfyError
from ..core.comfy.workers import get_worker_registry

router = APIRouter(prefix="/comfy", tags=["render-workers"])


@router.get("/workers")
async def render_workers() -> dict:
    try:
        return {"workers": await get_worker_registry().status()}
    except ComfyError as exc:
        raise HTTPException(503, str(exc)) from None
