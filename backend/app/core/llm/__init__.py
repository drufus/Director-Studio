from ...config import settings
from .ollama import OllamaLLMProvider
from .openai_compatible import OpenAICompatibleClient, OpenAICompatibleLLMProvider
from .provider import InferenceClient, LLMProvider, LLMProviderError, public_llm_error


def get_llm_provider() -> LLMProvider:
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLLMProvider()
    if settings.llm_provider == "ollama":
        return OllamaLLMProvider()
    raise LLMProviderError("Unknown Director LLM provider.")


__all__ = ["InferenceClient", "LLMProvider", "LLMProviderError", "OllamaLLMProvider",
           "OpenAICompatibleClient", "OpenAICompatibleLLMProvider", "get_llm_provider", "public_llm_error"]
