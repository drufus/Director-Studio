from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from ...config import settings


class LLMProviderError(RuntimeError):
    """Public inference failure containing no request credentials or response body."""


def public_llm_error(exc: Exception) -> str:
    """Expose only errors whose text was deliberately constructed for callers."""
    from ..vram.director_model import ModelSelectionError
    if isinstance(exc, (LLMProviderError, ModelSelectionError)):
        return str(exc)
    return f"Director inference failed ({type(exc).__name__})."


class InferenceClient(Protocol):
    async def health(self) -> bool: ...
    async def list_models(self) -> list[str]: ...
    async def generate(self, model: str, prompt: str, **kwargs: Any) -> str: ...
    async def chat(self, model: str, prompt: str, **kwargs: Any) -> str: ...
    async def chat_response(
        self, model: str, *, messages: Sequence[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]: ...
    def generate_stream(
        self, model: str, prompt: str, **kwargs: Any
    ) -> AsyncIterator[dict[str, str]]: ...


def configured_capabilities(model: str) -> dict[str, Any]:
    verified = {name.strip() for name in settings.llm_vision_models.split(",") if name.strip()}
    vision = bool(model and model in verified)
    return {
        "vision": vision,
        "vision_reason": (
            "Image support explicitly verified by the operator."
            if vision else
            "Image support has not been verified for the selected model. "
            "Visual direction and layout analysis are unavailable; verify the model "
            "and add its exact ID to DS_LLM_VISION_MODELS."
        ),
    }


class LLMProvider(Protocol):
    """Director-facing model selection, capabilities, and inference boundary."""

    provider_id: str
    client: InferenceClient

    async def list_models(self) -> list[str]: ...
    def model_status(self) -> dict: ...
    def select_model(self, model: str, *, persist: bool) -> str: ...
    def capabilities(self, model: str) -> dict[str, Any]: ...
