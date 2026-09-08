from fastapi import APIRouter

from ..config import settings
from ..core.comfy.workers import get_worker_registry
from ..core.llm import get_llm_provider, public_llm_error
from ..core.schemas import HealthResponse
from .director import _public_error

router = APIRouter(tags=["system"])


@router.get("/health/live")
async def liveness() -> dict[str, bool]:
    """Check this service without contacting external inference providers."""
    return {"ok": True}


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    comfy_ok = False
    comfy_error = None
    details: dict = {}
    try:
        workers = await get_worker_registry().status()
        details = {"workers": workers}
        comfy_ok = bool(workers) and all(w["status"] == "up" for w in workers)
        if not workers:
            comfy_error = "No render workers configured. Set DS_COMFY_WORKERS explicitly."
        elif not comfy_ok:
            comfy_error = "; ".join(f"{w['id']}: {w['error']}" for w in workers if w["status"] != "up")
    except Exception as exc:
        comfy_error = _public_error(exc, "Comfy health check")

    llm_reachable = False
    llm_model_available = False
    llm_model = None
    llm_error = None
    try:
        provider = get_llm_provider()
        available = await provider.list_models()
        llm_reachable = True
        llm_model = provider.model_status().get("model") or ""
        llm_model_available = bool(llm_model and llm_model in available)
        if not llm_model:
            llm_error = "No Director model selected. Select a model in the Director picker."
        elif not llm_model_available:
            llm_error = (
                f"Selected Director model {llm_model!r} is not served by provider "
                f"{provider.provider_id!r}."
            )
    except Exception as exc:
        llm_error = public_llm_error(exc)

    return HealthResponse(
        ok=comfy_ok and llm_model_available,
        comfy_reachable=comfy_ok,
        comfy_error=comfy_error,
        details={
            "workers": details.get("workers", []),
            "llm_provider": settings.llm_provider,
            "llm_reachable": llm_reachable,
            "llm_model": llm_model,
            "llm_model_available": llm_model_available,
            "llm_error": llm_error,
        },
    )
