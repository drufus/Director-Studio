from __future__ import annotations

from ..vram.director_model import model_status, set_director_model
from ..vram.ollama_client import OllamaClient
from .provider import configured_capabilities


class OllamaLLMProvider:
    provider_id = "ollama"

    def __init__(self, client: OllamaClient | None = None) -> None:
        self.client = client or OllamaClient()

    async def list_models(self) -> list[str]:
        return await self.client.list_models()

    def model_status(self) -> dict:
        return model_status(provider=self.provider_id)

    def select_model(self, model: str, *, persist: bool) -> str:
        return set_director_model(model, persist=persist, provider=self.provider_id)

    def capabilities(self, model: str) -> dict:
        return configured_capabilities(model)
