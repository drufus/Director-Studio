"""Strict OpenAI-compatible HTTP transport. No backend, model, or protocol retries.

Only standard chat-completions fields are sent. The current router requires no
provider-specific extra_body fields; Ollama GPU/residency options are rejected.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ...config import settings
from ..vram.director_model import model_status, set_director_model
from .provider import LLMProviderError, configured_capabilities


logger = logging.getLogger("director_studio.llm.usage")


class OpenAICompatibleClient:
    def __init__(
        self, base_url: str | None = None, *, api_key: SecretStr | str | None = None,
        timeout: float = 600.0, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (settings.llm_base_url if base_url is None else base_url).rstrip("/")
        key = settings.llm_api_key if api_key is None else api_key
        self._api_key = key.get_secret_value() if isinstance(key, SecretStr) else key
        self._timeout = timeout
        self._transport = transport
        self.last_usage: dict[str, Any] | None = None

    @staticmethod
    def _token_count(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _begin_usage(self, model: str, max_tokens: Any, *, stream: bool) -> dict[str, Any]:
        self.last_usage = None
        # Snapshot selection before any network await. Never guess a source when
        # persisted selection cannot be read, and never inspect response text.
        selection = model_status(provider="openai_compatible")
        source = selection["source"] if selection["model"] == model else "explicit_request"
        if source not in {"runtime", "persisted", "env", "explicit_request"}:
            raise self._error(model, "invalid model selection source")
        public_model = model.replace(self._api_key, "[REDACTED]") if self._api_key else model
        metrics = {
            "provider": "openai_compatible", "model": public_model,
            "selection_source": source,
            "operation": "stream_completion" if stream else "chat_completion",
            "max_tokens": self._token_count(max_tokens),
            "finish_reason": None, "status": "pending",
            "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "reasoning_tokens": None, "payload_tokens": None,
        }
        self.last_usage = metrics
        return metrics

    def _observe_usage(self, metrics: dict[str, Any], data: Any) -> None:
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        if isinstance(usage, dict):
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                metrics[field] = self._token_count(usage.get(field))
            details = usage.get("completion_tokens_details")
            reasoning = self._token_count(details.get("reasoning_tokens")) if isinstance(details, dict) else None
            metrics["reasoning_tokens"] = reasoning
            completion = metrics["completion_tokens"]
            # Completion includes reasoning and tool-call output. Missing server
            # breakdown is unknown; text lengths cannot establish token counts.
            metrics["payload_tokens"] = (
                completion - reasoning
                if completion is not None and reasoning is not None and reasoning <= completion else None
            )
        choices = data.get("choices")
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
            reason = choices[0].get("finish_reason")
            if metrics["finish_reason"] is None and isinstance(reason, str) and reason in {"stop", "length", "tool_calls", "content_filter", "function_call"}:
                metrics["finish_reason"] = reason

    def _finish_usage(self, metrics: dict[str, Any], *, succeeded: bool) -> None:
        metrics["status"] = "succeeded" if succeeded else "failed"
        self.last_usage = dict(metrics)
        logger.info("inference_usage %s", json.dumps(metrics, ensure_ascii=False, sort_keys=True))

    def _error(self, model: str, cause: str, status: int | None = None) -> LLMProviderError:
        label = model or "(catalog)"
        if self._api_key:
            label = label.replace(self._api_key, "[REDACTED]")
        suffix = f", HTTP {status}" if status is not None else ""
        if status is not None:
            category = {
                400: "invalid or unsupported request", 401: "authentication rejected",
                403: "access forbidden", 404: "endpoint or model not found",
                408: "upstream request timed out", 413: "request too large",
                422: "request validation failed", 429: "rate limit or quota exceeded",
                500: "upstream server error", 502: "upstream gateway failure",
                503: "upstream service unavailable", 504: "upstream gateway timed out",
            }.get(status, "redirect rejected" if 300 <= status < 400 else "request rejected")
            cause = f"{cause}: {category}"
        return LLMProviderError(f"openai_compatible model {label!r}{suffix}: {cause}")

    def _completion_error(self, model: str, reason: Any, *, stream: bool = False) -> LLMProviderError:
        label = "stream" if stream else "completion"
        cause = {
            "length": f"incomplete {label}: token limit exhausted (finish_reason=length)",
            "content_filter": f"rejected {label}: content filter (finish_reason=content_filter)",
            "function_call": f"unsupported legacy function-call {label}; native tool_calls required",
        }.get(reason if isinstance(reason, str) else "", f"incomplete or rejected {label}: missing or unsupported finish reason")
        return self._error(model, cause)

    def _http(self, model: str, *, timeout: float | None = None) -> httpx.AsyncClient:
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme in {"http", "https"} and url.hostname and
                     not url.username and not url.password and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise self._error(model, "DS_LLM_BASE_URL must be an HTTP(S) URL without credentials, query, or fragment")
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return httpx.AsyncClient(
            timeout=timeout or self._timeout, headers=headers, transport=self._transport,
            follow_redirects=False,
        )

    async def _request(self, method: str, path: str, *, model: str = "", body: dict | None = None) -> Any:
        try:
            async with self._http(model, timeout=5.0 if path == "/models" else None) as client:
                response = await client.request(method, self.base_url + path, json=body)
                if not 200 <= response.status_code < 300:
                    raise self._error(model, "HTTP request rejected", response.status_code)
                try:
                    return response.json()
                except (ValueError, UnicodeError):
                    raise self._error(model, "invalid JSON response") from None
        except httpx.TimeoutException:
            raise self._error(model, "request timed out") from None
        except (httpx.HTTPError, OSError):
            raise self._error(model, "transport connection failed") from None

    async def health(self) -> bool:
        await self.list_models()
        return True

    async def list_models(self) -> list[str]:
        response = await self._request("GET", "/models")
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise self._error("", "invalid model catalog")
        names: list[str] = []
        for item in response["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise self._error("", "invalid model catalog entry")
            if item["id"] not in names:
                names.append(item["id"])
        return names

    def _image_url(self, image: str, model: str) -> str:
        # App callers supply raw base64 (JPEG and PNG are both used). Preserve
        # explicit data/HTTP URLs instead of guessing their contents.
        if not isinstance(image, str) or not image:
            raise self._error(model, "invalid image input")
        if image.startswith(("data:image/", "https://", "http://")):
            return image
        try:
            prefix = base64.b64decode(image, validate=True)
        except (ValueError, binascii.Error):
            raise self._error(model, "invalid base64 image input") from None
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif prefix.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif prefix.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise self._error(model, "unsupported image format")
        return f"data:{mime};base64,{image}"

    def _messages(self, model: str, messages: Sequence[dict[str, Any]], require_vision: bool) -> list[dict]:
        normalized: list[dict] = []
        has_images = False
        pending_calls: set[str] = set()
        for message in messages:
            role = message.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise self._error(model, "invalid message role")
            content = message.get("content")
            result: dict[str, Any] = {"role": role, "content": content}
            if message.get("images"):
                if role != "user" or not isinstance(content, (str, type(None))):
                    raise self._error(model, "invalid image message")
                result["content"] = [{"type": "text", "text": content or ""}] + [
                    {"type": "image_url", "image_url": {"url": self._image_url(image, model)}}
                    for image in message["images"]
                ]
                has_images = True
            elif isinstance(content, list):
                has_images = has_images or any(part.get("type") == "image_url" for part in content if isinstance(part, dict))
            if role == "assistant" and message.get("tool_calls"):
                calls = self._normalize_tool_calls(model, message["tool_calls"], replay=True)
                for call in calls:
                    if call["id"] in pending_calls:
                        raise self._error(model, "duplicate tool call ID in replay")
                    pending_calls.add(call["id"])
                result["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                }} for call in calls]
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending_calls:
                    raise self._error(model, "missing or unmatched tool call ID in replay")
                result["tool_call_id"] = call_id
                pending_calls.remove(call_id)
            normalized.append(result)
        if pending_calls:
            raise self._error(model, "tool call replay is missing a result")
        if (has_images or require_vision) and not configured_capabilities(model)["vision"]:
            raise self._error(model, "image support is not explicitly verified; configure DS_LLM_VISION_MODELS")
        return normalized

    def _normalize_tool_calls(self, model: str, calls: Any, *, replay: bool = False) -> list[dict]:
        if not isinstance(calls, list):
            raise self._error(model, "invalid tool calls")
        result: list[dict] = []
        seen: set[str] = set()
        for call in calls:
            if not isinstance(call, dict):
                raise self._error(model, "invalid tool call")
            call_id = call.get("id")
            function = call.get("function", call if replay else None)
            if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
                raise self._error(model, "missing or duplicate tool call ID")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"].strip():
                raise self._error(model, "invalid tool function name")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    raise self._error(model, "invalid tool arguments JSON") from None
            if not isinstance(arguments, dict):
                raise self._error(model, "tool arguments must be a JSON object")
            seen.add(call_id)
            result.append({"id": call_id, "name": function["name"], "arguments": arguments})
        return result

    def _body(self, model: str, messages: Sequence[dict], *, tools: Sequence[dict] | None = None,
              format: dict | str | None = None, options: dict | None = None, require_vision: bool = False) -> dict:
        if not model.strip():
            raise self._error(model, "no model selected")
        body: dict[str, Any] = {"model": model, "messages": self._messages(model, messages, require_vision),
                                "max_tokens": settings.director_num_predict, "stream": False}
        for name, value in (options or {}).items():
            if name not in {"temperature", "num_predict", "max_tokens"}:
                raise self._error(model, "unsupported inference option; only temperature and token limit are supported")
            body["max_tokens" if name == "num_predict" else name] = value
        if tools:
            body["tools"] = list(tools)
        if format == "json":
            body["response_format"] = {"type": "json_object"}
        elif isinstance(format, dict):
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "director_response", "strict": True, "schema": format,
            }}
        elif format is not None:
            raise self._error(model, "unsupported structured output format")
        return body

    def _result(self, model: str, data: Any) -> dict:
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
            raise self._error(model, "invalid completion choices")
        choice = data["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise self._error(model, "invalid completion message")
        reason = choice.get("finish_reason")
        if reason not in {"stop", "tool_calls"}:
            raise self._completion_error(model, reason)
        message = choice["message"]
        if message.get("refusal"):
            raise self._error(model, "completion refused")
        content = message.get("content")
        content = "" if content is None else content
        thinking = message.get("reasoning_content", message.get("reasoning"))
        thinking = "" if thinking is None else thinking
        if not isinstance(content, str) or not isinstance(thinking, str):
            raise self._error(model, "invalid completion content")
        raw_calls = message.get("tool_calls")
        calls = self._normalize_tool_calls(model, [] if raw_calls is None else raw_calls)
        if (reason == "tool_calls") != bool(calls):
            raise self._error(model, "inconsistent tool completion finish reason")
        if not content.strip() and not calls:
            raise self._error(model, "empty final completion")
        return {"content": content, "thinking": thinking, "tool_calls": calls, "done_reason": reason}

    async def chat_response(self, model: str, *, messages: Sequence[dict[str, Any]], tools: Sequence[dict] | None = None,
                            format: dict | str | None = None, require_vision: bool = False,
                            options: dict | None = None) -> dict:
        body = self._body(model, messages, tools=tools, format=format, options=options, require_vision=require_vision)
        metrics = self._begin_usage(model, body["max_tokens"], stream=False)
        succeeded = False
        try:
            data = await self._request("POST", "/chat/completions", model=model, body=body)
            self._observe_usage(metrics, data)
            result = self._result(model, data)
            succeeded = True
            return result
        finally:
            self._finish_usage(metrics, succeeded=succeeded)

    @staticmethod
    def _prompt_messages(prompt: str, system: str | None, images: Sequence[str] | None) -> list[dict]:
        messages = [{"role": "system", "content": system}] if system else []
        user: dict = {"role": "user", "content": prompt}
        if images:
            user["images"] = list(images)
        return [*messages, user]

    async def chat(self, model: str, prompt: str, *, system: str | None = None, images: Sequence[str] | None = None,
                   format: dict | str | None = None, require_vision: bool = False, options: dict | None = None) -> str:
        result = await self.chat_response(model, messages=self._prompt_messages(prompt, system, images),
                                          format=format, require_vision=require_vision, options=options)
        return result["content"]

    async def generate(self, model: str, prompt: str, *, images: Sequence[str] | None = None,
                       options: dict | None = None, system: str | None = None) -> str:
        return await self.chat(model, prompt, images=images, options=options, system=system)

    async def generate_stream(self, model: str, prompt: str, *, images: Sequence[str] | None = None,
                              system: str | None = None, options: dict | None = None) -> AsyncIterator[dict[str, str]]:
        body = self._body(model, self._prompt_messages(prompt, system, images), options=options)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        metrics = self._begin_usage(model, body["max_tokens"], stream=True)
        succeeded = False
        completion_error: LLMProviderError | None = None
        finished = False
        done = False
        has_content = False
        event_data: list[str] = []

        def parse_event(payload: str) -> list[dict[str, str]]:
            nonlocal finished, done, has_content, completion_error
            if payload == "[DONE]":
                if done:
                    raise self._error(model, "duplicate stream completion marker")
                if not finished:
                    raise self._error(model, "stream ended before finish reason")
                done = True
                return []
            if done:
                raise self._error(model, "stream data after completion")
            try:
                data = json.loads(payload)
            except ValueError:
                raise self._error(model, "invalid stream JSON") from None
            self._observe_usage(metrics, data)
            if not isinstance(data, dict) or "error" in data or not isinstance(data.get("choices"), list):
                raise self._error(model, "invalid stream event")
            if not data["choices"] and isinstance(data.get("usage"), dict):
                return []
            if len(data["choices"]) != 1 or not isinstance(data["choices"][0], dict):
                raise self._error(model, "invalid stream choices")
            choice = data["choices"][0]
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("refusal"):
                raise self._error(model, "unexpected stream tool call or refusal")
            if finished:
                # LiteLLM's verified accounting footer retains one empty choice:
                # {delta: {}, finish_reason: null, index: 0}, plus usage. It
                # contains no new model output and cannot change termination.
                index = choice.get("index", 0)
                if (
                    isinstance(data.get("usage"), dict)
                    and delta == {}
                    and choice.get("finish_reason") is None
                    and type(index) is int and index == 0
                ):
                    return []
                raise self._error(model, "stream content after finish reason")
            events: list[dict[str, str]] = []
            for field, kind in (("reasoning_content", "think"), ("content", "token")):
                text = delta.get(field)
                if text is not None and not isinstance(text, str):
                    raise self._error(model, "invalid stream content")
                if text:
                    events.append({"kind": kind, "text": text})
                    has_content = has_content or (kind == "token" and bool(text.strip()))
            reason = choice.get("finish_reason")
            if reason is not None:
                if reason != "stop":
                    # A final usage-only event can follow a length finish. Read
                    # that accounting trailer, then raise the same failure.
                    completion_error = self._completion_error(model, reason, stream=True)
                finished = True
            return events

        try:
            async with self._http(model) as client:
                async with client.stream("POST", self.base_url + "/chat/completions", json=body) as response:
                    if not 200 <= response.status_code < 300:
                        raise self._error(model, "HTTP stream request rejected", response.status_code)
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            event_data.append(line[5:].lstrip(" "))
                        elif not line and event_data:
                            for event in parse_event("\n".join(event_data)):
                                yield event
                            event_data.clear()
                    if event_data:
                        for event in parse_event("\n".join(event_data)):
                            yield event
            if not done or not finished:
                raise self._error(model, "stream disconnected before complete termination")
            if completion_error is not None:
                raise completion_error
            if not has_content:
                raise self._error(model, "empty final stream completion")
            succeeded = True
        except httpx.TimeoutException:
            raise self._error(model, "stream timed out") from None
        except (httpx.HTTPError, OSError):
            raise self._error(model, "stream transport disconnected") from None
        finally:
            self._finish_usage(metrics, succeeded=succeeded)


class OpenAICompatibleLLMProvider:
    provider_id = "openai_compatible"

    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.client = client or OpenAICompatibleClient()

    async def list_models(self) -> list[str]:
        return await self.client.list_models()

    def model_status(self) -> dict:
        return model_status(provider=self.provider_id)

    def select_model(self, model: str, *, persist: bool) -> str:
        return set_director_model(model, persist=persist, provider=self.provider_id)

    def capabilities(self, model: str) -> dict[str, Any]:
        return configured_capabilities(model)
