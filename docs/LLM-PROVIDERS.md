# Director LLM providers

Director supports local Ollama and OpenAI-compatible chat completions, including the existing LiteLLM router. This phase enables remote planning and independent chat. Remote Comfy transport, worker scheduling, H3 evidence, and systemd deployment belong to later PRs; the legacy Comfy transport remains until Phase 3.

## Cluster configuration

Place settings in the private runtime env file (macOS development uses `backend/.env`). Do not print the file or copy the real key into documentation. A redacted example:

```dotenv
DS_LLM_PROVIDER=openai_compatible
DS_LLM_BASE_URL=http://100.124.186.11:4000/v1
DS_LLM_API_KEY=<redacted>
DS_LLM_MODEL=kasari-flash
DS_LLM_VISION_MODELS=kasari-flash
DS_DIRECTOR_NUM_PREDICT=8192
DS_VRAM_POLICY=independent
```

The model ID above is an explicit operator choice, not an application default. The current endpoint passed image, strict JSON-schema, native tool-result replay, and SSE probes with `kasari-flash`. `kasari-brain` passed text/schema/tools but rejected images. See [dated capability evidence](PHASE-2-ROUTER-CAPABILITIES.md). Reverify capabilities when changing a route or endpoint. `DS_LLM_VISION_MODELS` is a comma-separated allowlist of exact verified IDs; empty disables image features. Text planning and chat remain available with a text-only model. The application does not install inference servers.

`DS_LLM_BASE_URL` must include the API prefix (normally `/v1`), with no embedded credentials, query, or fragment. Credentials are held as a secret configuration value and sent in the Authorization header. HTTP errors report the provider, model, HTTP status and a sanitized cause; response bodies and request credentials are not logged.

`DS_DIRECTOR_NUM_PREDICT` controls the standard `max_tokens` request field (default 4096). Reasoning uses the same completion budget on this router. The real three-shot Director planning smoke exhausted 4096 and passed an explicit same-model diagnostic at 8192, which is therefore set in the cluster example above. A length-limited or empty final response fails explicitly; it is not treated as a valid plan or retried with a larger limit automatically.

## Model selection

`GET /api/director/model` returns the active provider, selected model, source, catalog reachability, available models, error, and vision capability. `PUT /api/director/model` validates the exact ID against the live catalog before changing it. Failed discovery is visible in the picker. No first catalog entry is selected or persisted automatically.

Selection order within the configured provider is runtime override, persisted selection, then `DS_LLM_MODEL`. The Ollama provider also accepts the legacy `DS_DIRECTOR_PLAN_MODEL` environment value. Persistence uses:

```json
{"version": 2, "selections": {"openai_compatible": "kasari-flash"}}
```

An old `{"model":"..."}` file is interpreted as an Ollama selection only, with a warning when ignored by the new provider. An explicitly blank persisted choice stays blank. Corrupt/unreadable state fails visibly; repair that file explicitly. Switching models in a running conversation does not change the provider/model already captured for that conversation's native tool loop or bounded repair request.

## Independent GPU policy

OpenAI-compatible configuration defaults to `independent`; explicitly requesting `exclusive` with that provider is a configuration error. Local Ollama defaults to `exclusive`, and can explicitly opt into independent mode.

Independent mode retains generation reservations for status but disables reservation admission checks in the chat API and LLM session, publishes `chat_locked=false`, and keeps text chat controls available during queued, uploading, running, and saving render jobs. It creates no Ollama residency client, performs no warm/unload calls, and never automatically calls Comfy `/free` for a Director conversation. Genuine active-chat protection within a project remains. An explicit manual free request is still an explicit Comfy operation.

The exclusive local path retains GPU ownership and reservation blocking, tested separately. `DS_LLM_KEEP_LOADED` applies to that local residency policy only. `/api/director/wake` and `python -m app.scripts.wake_agent` verify the configured provider/model and optionally reload context; they do not control remote model residency.

`GET /api/health/live` checks only the application. `GET /api/health` reports configured LLM/model and Comfy readiness; failures identify the dependency. Provider model discovery is also available independently of Comfy readiness through the picker endpoint.

## Inference and structured output

Requests use only standard chat-completions fields: `model`, `messages`, `max_tokens`, `temperature` when requested, `stream`, `stream_options.include_usage` for streaming accounting, `response_format`, and `tools`. Images use `image_url` message parts. Tool calls preserve provider-issued IDs, JSON argument objects, and matching `tool_call_id` results across the Director tool loop. No `extra_body` fields are currently needed. A future nonstandard field must be explicitly supported and probed through the router's extra-body convention; Ollama GPU/context/residency options are not sent to LiteLLM.

Existing JSON object mode and Pydantic schemas map to `response_format` JSON object and strict JSON-schema modes. The existing single-tool structured request for actor/GPT layout operations stays an upfront protocol choice. Preserved validation paths are:

- Shot planning: deterministic extraction/validation followed by at most one same-model JSON repair.
- Storyboard semantic validation: existing structured verdict parsing and bounded storyboard resubmission budget.
- Six-section H3 prompt writing: deterministic field and picture-binding validation followed by at most one same-model repair.
- Visual briefs: schema validation and the existing bounded generation-prompt reference repair, followed by deterministic prompt validation.

Provider failures, truncation, and refusal stop those paths. A parse repair is not an alternate model/backend choice. Image-to-text retries, XML-error tool-protocol switching, and nonstream regeneration after a failed stream have been removed. An incomplete stream produces an error, even if some text was already displayed.

## Token accounting

The `director_studio.llm.usage` logger writes one sanitized `inference_usage` JSON record per OpenAI-compatible request. It includes exact requested model ID, selection source captured before the request, configured completion cap, finish reason, success/failure, and available server usage counters. The client also exposes the latest record as `last_usage`. Neither prompts, answers, reasoning text, nor credentials enter these records.

Accounting is observed before completion validation, so a `finish_reason=length` response retains its counters while still failing. Streaming requests ask for the standard usage trailer. `reasoning_tokens` uses only the server's `completion_tokens_details.reasoning_tokens`; `payload_tokens` is completion minus reasoning only when both counts are available and consistent. Missing breakdowns are `null`, never guessed from characters or reported as zero. Explicit server zero is retained. Interrupted streams may have unknown final usage.

The measured real 8192-token planning pass used exact model `kasari-flash`, source `env`, 3349 prompt tokens, and 1678 completion tokens (937 reasoning, 741 payload), returning three shots. See the capability report for the earlier truncation and later fresh vision-route roll call. Dedicated vision models running elsewhere in the cluster are not assumed routable unless their exact IDs are exposed by LiteLLM.
