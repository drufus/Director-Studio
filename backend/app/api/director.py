"""Director agent control endpoints (wake / VRAM)."""

from __future__ import annotations

from dataclasses import asdict

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..agents.director.context_io import load_agent_context
from ..config import settings
from ..core.comfy.client import ComfyError
from ..core.comfy.workers import get_worker_registry
from ..core.llm import LLMProvider, LLMProviderError, get_llm_provider, public_llm_error
from ..core.vram import get_director_model, get_orchestrator
from ..core.vram.director_model import ModelSelectionError
from ..core.vram.orchestrator import GPUBusyError

router = APIRouter(tags=["director"])


class WakeBody(BaseModel):
    project_id: str | None = None
    keep: bool = Field(
        default=False,
        description="If true, leave LLM loaded; otherwise release after warm.",
    )
    reload_context: bool = Field(
        default=True,
        description="If project_id set, load agent context and ping the model with it.",
    )


class WakeResponse(BaseModel):
    ok: bool
    model: str
    project_id: str | None = None
    last_phase: str | None = None
    agent_reply: str | None = None
    llm_released: bool = True


class DirectorModelBody(BaseModel):
    model: str = Field(..., min_length=1, description="Exact model ID from the configured provider")
    persist: bool = Field(
        default=True,
        description="Write choice to data/director_model.json (survives process restart).",
    )


def _public_error(error: Exception, operation: str) -> str:
    """Only dedicated public exceptions may supply unredacted error messages."""
    if isinstance(error, (LLMProviderError, ModelSelectionError)):
        return public_llm_error(error)
    if isinstance(error, (GPUBusyError, ComfyError)):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return f"{operation} failed (HTTP {error.response.status_code})."
    if isinstance(error, httpx.TimeoutException):
        return f"{operation} timed out."
    if isinstance(error, httpx.ConnectError):
        return f"{operation} failed to connect."
    # Raw HTTP exceptions can embed request URLs, credentials, or response bodies.
    return f"{operation} failed ({type(error).__name__})."


def _model_response(
    provider: LLMProvider, available: list[str], *, catalog_error: str | None = None
) -> dict:
    selection_error = None
    try:
        status = provider.model_status()
    except Exception as exc:
        selection_error = _public_error(exc, "Director model selection read")
        status = {"model": "", "source": "error"}
    model = str(status.get("model") or "").strip()
    if not selection_error and not catalog_error and model and model not in available:
        selection_error = (
            f"Selected Director model {model!r} is not served by provider "
            f"{provider.provider_id!r}. Select an available model."
        )
    return {
        **status,
        "provider": provider.provider_id,
        "reachable": catalog_error is None,
        "available": available,
        "error": "; ".join(filter(None, [selection_error, catalog_error])) or None,
        "capabilities": provider.capabilities(model),
    }


@router.get("/director/model")
async def get_model(provider: LLMProvider = Depends(get_llm_provider)) -> dict:
    """Current selection and catalog; discovery never selects or persists a model."""
    try:
        available = await provider.list_models()
    except Exception as exc:
        return _model_response(
            provider, [], catalog_error=_public_error(exc, "Director model discovery")
        )
    return _model_response(provider, available)


@router.put("/director/model")
async def put_model(
    body: DirectorModelBody,
    provider: LLMProvider = Depends(get_llm_provider),
) -> dict:
    """Hot-switch Director plan model without restarting the backend."""
    name = body.model.strip()
    if not name:
        raise HTTPException(400, "Director model ID must not be blank.")
    try:
        available = await provider.list_models()
    except Exception as exc:
        raise HTTPException(503, _public_error(exc, "Director model discovery")) from None
    if name not in available:
        raise HTTPException(
            400,
            f"Director model {name!r} is not served by provider {provider.provider_id!r}.",
        )
    try:
        provider.select_model(name, persist=body.persist)
    except Exception as exc:
        raise HTTPException(400, _public_error(exc, "Director model selection")) from None
    return {
        "ok": True,
        **_model_response(provider, available),
    }


@router.post("/director/wake", response_model=WakeResponse)
async def wake_director(body: WakeBody | None = None) -> WakeResponse:
    """
    Check the selected model and optionally reload a project's agent context.

    Warming and release apply only when the configured policy shares one GPU.
    """
    body = body or WakeBody()
    orch = get_orchestrator()
    model = get_director_model()
    agent_reply: str | None = None
    last_phase: str | None = None

    try:
        async with orch.llm_session(release_on_exit=not body.keep):
            await orch.ensure_llm_ready()
            if body.project_id and body.reload_context:
                ctx = load_agent_context(body.project_id)
                if ctx is None:
                    raise HTTPException(404, f"no agent context for {body.project_id}")
                last_phase = ctx.last_phase
                import json

                summary = json.dumps(
                    {
                        "project_id": ctx.project_id,
                        "last_phase": ctx.last_phase,
                        "shot_ids": [s.get("id") for s in ctx.shot_summaries[:12]],
                    },
                    ensure_ascii=False,
                )
                agent_reply = await get_llm_provider().client.generate(
                    model,
                    "You are the Director Studio agent. "
                    "Acknowledge context reload in one short sentence.\n"
                    f"CONTEXT:\n{summary}\n",
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, _public_error(exc, "Director wake")) from None

    return WakeResponse(
        ok=True,
        model=model,
        project_id=body.project_id,
        last_phase=last_phase,
        agent_reply=(agent_reply or "").strip() or None,
        llm_released=orch.shared_gpu_enabled and not body.keep,
    )


@router.get("/director/vram")
async def vram_status() -> dict:
    orch = get_orchestrator()
    model = get_director_model()
    reservations = await orch.generation_reservations()
    ollama_vram: int | None = None
    ollama_loaded: list[dict] = []
    vram_error = None
    if orch.shared_gpu_enabled:
        try:
            ollama_loaded = await orch.ollama.loaded_models()
            ollama_vram = await orch.ollama.model_vram_bytes(model)
        except Exception as exc:
            vram_error = _public_error(exc, "Ollama VRAM status")
        if ollama_vram is not None:
            # Re-sync shared GPU residency after an external unload or restart.
            orch._llm_ready = ollama_vram > 0
    return {
        "owner": orch.owner,
        "comfy_pipeline": orch.comfy_pipeline,
        "policy": orch.policy,
        "shared_gpu_enabled": orch.shared_gpu_enabled,
        "models": list(orch.models),
        "model": model,
        "llm_ready": orch._llm_ready,
        "llm_keep_loaded": orch.shared_gpu_enabled and settings.llm_keep_loaded,
        "ollama_size_vram": ollama_vram,
        "ollama_on_gpu": ollama_vram > 0 if ollama_vram is not None else None,
        "error": vram_error,
        "ollama_ps": [
            {
                "name": m.get("name"),
                "size": m.get("size"),
                "size_vram": m.get("size_vram"),
            }
            for m in ollama_loaded
        ],
        "queue_waiters": getattr(orch, "_waiters", 0),
        "chat_locked": orch.shared_gpu_enabled and bool(reservations),
        "generation_count": len(reservations),
        "generation_jobs": [asdict(item) for item in reservations],
        "acquire_timeout_sec": orch.acquire_timeout_sec,
        "last_comfy_free": getattr(orch, "last_comfy_free", None),
        "last_comfy_free_error": getattr(orch, "last_comfy_free_error", None),
    }


@router.post("/director/free-comfy")
async def free_comfy_models(worker_id: str | None = None) -> dict:
    """Explicit user action to unload ComfyUI models, independent of auto policy."""
    orch = get_orchestrator()
    try:
        if orch.shared_gpu_enabled:
            stats = await orch.release_comfy_models(require_ok=True)
        else:
            if not worker_id:
                raise HTTPException(422, "Select an explicit worker_id to release remote ComfyUI models")
            stats = await get_worker_registry().client_for(worker_id).free_memory(unload_models=True, free_memory=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, _public_error(exc, "Comfy model release")) from None
    return {"ok": True, "stats": stats}
