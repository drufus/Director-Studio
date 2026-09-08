from __future__ import annotations

import copy
import json
import logging
import uuid
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import httpx

from ...config import settings

logger = logging.getLogger("director_studio.ollama")

# Offload as many layers as possible to GPU (Ollama ignores excess).
def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _merge_thinking(data: dict[str, Any]) -> str:
    thinking = data.get("thinking") or data.get("reasoning") or ""
    msg = data.get("message") or {}
    text = str(data.get("response") or msg.get("content") or "")
    if thinking and "<think>" not in text.lower():
        return f"<think>{thinking}</think>\n{text}"
    if msg.get("thinking") and "<think>" not in text.lower():
        return f"<think>{msg.get('thinking')}</think>\n{text}"
    return text


def _failure(cause: str):
    from ..llm.provider import LLMProviderError
    return LLMProviderError(f"ollama: {cause}")


def _require_verified_vision(model: str) -> None:
    from ..llm.provider import configured_capabilities
    if not configured_capabilities(model)["vision"]:
        raise _failure("image support is not explicitly verified; configure DS_LLM_VISION_MODELS")


def _check_completion(data: dict[str, Any], *, allow_empty: bool = False) -> None:
    if data.get("done") is not True or data.get("done_reason") not in {"stop", "tool_calls", "load", "unload"}:
        raise _failure("incomplete or rejected completion")
    message = data.get("message") or {}
    content = data.get("response") or message.get("content") or ""
    if not allow_empty and not content.strip() and not message.get("tool_calls"):
        raise _failure("empty final completion")


class OllamaClient:
    """Minimal async client for Ollama health, unload, generate, and vision chat."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 600.0) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        if not _is_loopback(self.base_url):
            pass
        self._timeout = timeout

    def _opts(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        o = {
            "num_gpu": 999,
            "num_ctx": settings.director_num_ctx,
            "num_predict": settings.director_num_predict,
        }
        if extra:
            o.update(extra)
        return o

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                return r.status_code < 400
        except (httpx.HTTPError, OSError):
            return False

    async def list_models(self) -> list[str]:
        """Return installed Ollama model tags in server order."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            names: list[str] = []
            for item in (response.json() or {}).get("models") or []:
                name = item.get("name") or item.get("model")
                if name:
                    names.append(str(name))
            return names

    async def loaded_models(self) -> list[dict[str, Any]]:
        """Return /api/ps models list (includes size_vram)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/ps")
                if r.status_code >= 400:
                    return []
                return list((r.json() or {}).get("models") or [])
        except (httpx.HTTPError, OSError):
            return []

    async def model_vram_bytes(self, model: str) -> int:
        """VRAM bytes currently used by model (0 = CPU/RAM only)."""
        name = (model or "").split(":")[0]
        for m in await self.loaded_models():
            n = str(m.get("name") or m.get("model") or "")
            if n == model or n.startswith(name):
                return int(m.get("size_vram") or 0)
        return 0

    async def unload_models(self, names: Sequence[str] | Iterable[str]) -> None:
        """Best-effort unload: POST /api/generate with keep_alive=0."""
        timeout = httpx.Timeout(10.0, connect=2.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for name in names:
                    if not name:
                        continue
                    try:
                        await client.post(
                            f"{self.base_url}/api/generate",
                            json={
                                "model": name,
                                "prompt": "",
                                "keep_alive": 0,
                            },
                        )
                    except (httpx.HTTPError, OSError):
                        continue
        except (httpx.HTTPError, OSError):
            return

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        images: Sequence[str] | None = None,
        keep_alive: str | int | None = None,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Non-streaming generate. Forces GPU offload via num_gpu when possible."""
        if images:
            _require_verified_vision(model)
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": self._opts(options),
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        elif getattr(settings, "llm_keep_loaded", True):
            body["keep_alive"] = "60m"
        if images:
            body["images"] = list(images)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(f"{self.base_url}/api/generate", json=body)
            if r.status_code >= 400:
                raise _failure(f"generation request rejected, HTTP {r.status_code}")
            data = r.json()
            _check_completion(data, allow_empty=not prompt.strip())
            return _merge_thinking(data)

    async def chat_response(
        self,
        model: str,
        *,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        format: dict[str, Any] | str | None = None,
        keep_alive: str | int | None = None,
        options: dict[str, Any] | None = None,
        require_vision: bool = False,
    ) -> dict[str, Any]:
        """Use Ollama HTTP directly so native call IDs survive normalization."""
        if require_vision or any(message.get("images") for message in messages):
            _require_verified_vision(model)
        request: dict[str, Any] = {
            "model": model,
            "messages": copy.deepcopy(list(messages)),
            "stream": False,
            "options": self._opts(options),
        }
        if tools:
            request["tools"] = list(tools)
        if format is not None:
            request["format"] = format
        if keep_alive is not None:
            request["keep_alive"] = keep_alive
        elif getattr(settings, "llm_keep_loaded", True):
            request["keep_alive"] = "60m"

        # Ollama accepts structured tool arguments; replay preserves call IDs.
        for message in request["messages"]:
            for call in message.get("tool_calls") or []:
                arguments = call.get("function", {}).get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed = json.loads(arguments)
                    except ValueError:
                        raise _failure("invalid tool arguments in replay") from None
                    if not isinstance(parsed, dict):
                        raise _failure("tool replay arguments must be a JSON object")
                    call["function"] = {**call["function"], "arguments": parsed}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                raw_response = await client.post(f"{self.base_url}/api/chat", json=request)
                if not 200 <= raw_response.status_code < 300:
                    raise _failure(f"chat request rejected, HTTP {raw_response.status_code}")
                try:
                    response = raw_response.json()
                except ValueError:
                    raise _failure("invalid chat response JSON") from None
        except httpx.TimeoutException:
            raise _failure("chat request timed out") from None
        except (httpx.HTTPError, OSError):
            raise _failure("chat request transport failed") from None
        if not isinstance(response, dict):
            raise _failure("invalid chat response")

        message = getattr(response, "message", None)
        if message is None and isinstance(response, dict):
            message = response.get("message") or {}

        def _value(obj: Any, key: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        normalized_calls: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for call in list(_value(message, "tool_calls", []) or []):
            function = _value(call, "function", {}) or {}
            name = _value(function, "name", "")
            arguments = _value(function, "arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    raise _failure("invalid tool arguments JSON") from None
            if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
                raise _failure("invalid tool call name or arguments")
            provided_id = _value(call, "id")
            if provided_id is None:
                call_id = f"ollama_{uuid.uuid4().hex}"
            elif not isinstance(provided_id, str) or not provided_id.strip():
                raise _failure("invalid tool call ID")
            else:
                call_id = provided_id
            if call_id in seen_ids:
                raise _failure("duplicate tool call ID")
            seen_ids.add(call_id)
            normalized_calls.append({"id": call_id, "name": name, "arguments": arguments})

        result = {
            "content": str(_value(message, "content", "") or ""),
            "thinking": str(_value(message, "thinking", "") or ""),
            "tool_calls": normalized_calls,
            "done_reason": str(_value(response, "done_reason", "") or ""),
        }
        if _value(response, "done") is not True or result["done_reason"] not in {"stop", "tool_calls"}:
            raise _failure("incomplete or rejected chat completion")
        if not result["content"].strip() and not normalized_calls:
            raise _failure("empty final chat completion")
        return result

    async def chat(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        images: Sequence[str] | None = None,
        keep_alive: str | int | None = None,
        options: dict[str, Any] | None = None,
        require_vision: bool = False,
        format: dict[str, Any] | str | None = None,
    ) -> str:
        """Text convenience wrapper over the normalized chat response."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_message: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user_message["images"] = list(images)
        messages.append(user_message)
        result = await self.chat_response(
            model,
            messages=messages,
            keep_alive=keep_alive,
            options=options,
            require_vision=require_vision,
            format=format,
        )
        text = result["content"]
        thinking = result["thinking"]
        if thinking and "<think>" not in text.lower():
            return f"<think>{thinking}</think>\n{text}"
        return text

    async def generate_stream(
        self,
        model: str,
        prompt: str,
        *,
        images: Sequence[str] | None = None,
        system: str | None = None,
        keep_alive: str | int | None = None,
        options: dict[str, Any] | None = None,
    ):
        """Stream once; preserve image input and surface incomplete streams."""
        if images:
            async for item in self.chat_stream(
                model, prompt, system=system, images=images,
                keep_alive=keep_alive, options=options,
            ):
                yield item
            return

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "options": self._opts(options),
        }
        if system:
            body["system"] = system
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        elif getattr(settings, "llm_keep_loaded", True):
            body["keep_alive"] = "60m"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/generate", json=body
            ) as r:
                if r.status_code >= 400:
                    # read body for better error
                    await r.aread()
                r.raise_for_status()
                completed = False
                has_content = False
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        raise _failure("invalid stream JSON") from None
                    if not isinstance(data, dict) or data.get("error"):
                        raise _failure("invalid or rejected stream event")
                    chunk = data.get("response")
                    think = data.get("thinking") or data.get("reasoning")
                    if think:
                        yield {"kind": "think", "text": str(think)}
                    if chunk:
                        has_content = has_content or bool(str(chunk).strip())
                        yield {"kind": "token", "text": str(chunk)}
                    if data.get("done"):
                        if data.get("done_reason") != "stop":
                            raise _failure("incomplete or rejected stream completion")
                        completed = True
                        break
                if not completed:
                    raise _failure("stream disconnected before complete termination")
                if not has_content:
                    raise _failure("empty final stream completion")

    async def chat_stream(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        images: Sequence[str] | None = None,
        keep_alive: str | int | None = None,
        options: dict[str, Any] | None = None,
    ):
        """Stream /api/chat."""
        if images:
            _require_verified_vision(model)
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_msg: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user_msg["images"] = list(images)
        messages.append(user_msg)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": self._opts(options),
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        elif getattr(settings, "llm_keep_loaded", True):
            body["keep_alive"] = "60m"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=body
            ) as r:
                # Need to raise with body available
                if r.status_code >= 400:
                    await r.aread()
                r.raise_for_status()
                completed = False
                has_content = False
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        raise _failure("invalid stream JSON") from None
                    if not isinstance(data, dict) or data.get("error"):
                        raise _failure("invalid or rejected stream event")
                    msg = data.get("message") or {}
                    think = data.get("thinking") or msg.get("thinking")
                    chunk = msg.get("content") or data.get("response")
                    if think:
                        yield {"kind": "think", "text": str(think)}
                    if chunk:
                        has_content = has_content or bool(str(chunk).strip())
                        yield {"kind": "token", "text": str(chunk)}
                    if data.get("done"):
                        if data.get("done_reason") != "stop":
                            raise _failure("incomplete or rejected stream completion")
                        completed = True
                        break
                if not completed:
                    raise _failure("stream disconnected before complete termination")
                if not has_content:
                    raise _failure("empty final stream completion")
