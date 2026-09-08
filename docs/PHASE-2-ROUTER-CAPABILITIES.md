# Phase 2 router capability findings

Authenticated live probes performed on 2026-09-08 UTC (2026-09-07 in Phoenix), before implementing the OpenAI-compatible adapter. This document records observations, not a persisted model selection.

## Result and design decision

The currently advertised **`kasari-flash`** supports the tested combination of image input, strict JSON-schema output, native tool calls, tool-result replay, and streaming. It is a suitable candidate for an **explicit** initial Director model selection covering visual features. It is a different model ID from `kasari-v4flash`, which is absent from the current catalog; the application must never substitute one name for the other.

**`kasari-brain`** passed text, strict JSON schema, tools, replay, and streaming, but its configured route rejects multimodal requests with HTTP 400. If selected, visual-direction and layout-analysis features must be explicitly disabled with a clear model-specific UI message. Do not remove images and retry the request as text.

`kasari-voice` and `kasari-voice-long` are advertised but failed inference authorization, so their capabilities remain unverified. Catalog presence is not proof of inference availability. No model was automatically selected or persisted by these probes.

## Access and safety

- Endpoint: `http://100.124.186.11:4000/v1`, accessed from deployment host `thebeastvi` through the existing SSH alias `beast6`.
- SSH used `BatchMode=yes`, `StrictHostKeyChecking=yes`, and the existing configured deploy identity. No host-verification bypass or credential changes occurred.
- Credential source: `LITELLM_MASTER_KEY` in `/opt/kasari/config/litellm.env`. The value was read privately into the remote Python process and supplied only in the Authorization header to the specified LiteLLM endpoint.
- No key value was printed, written to a probe artifact, passed as a command argument, or added to this document. Only sanitized status and capability summaries were retained; raw router bodies, request headers, and generated reasoning were not retained.
- Probe images were synthetic PNGs containing colors only. No user image, project asset, ComfyUI endpoint, worker configuration, production service, or model selection was changed.
- Probes ran sequentially per model, with at most two diagnostic requests in flight across models. Requests had finite timeouts and small output budgets of 128–512 tokens. There were no alternate-model, alternate-protocol, or image-dropping retries. Explicit diagnostic repeats are described below.

## Authoritative catalog

Authenticated `GET /v1/models` returned HTTP 200 and these five IDs:

| Model ID | Catalog status and test scope |
| --- | --- |
| `kasari-brain` | Advertised; all requested chat capabilities probed |
| `kasari-embed` | Advertised; excluded from these Director chat-candidate probes |
| `kasari-flash` | Newly discovered chat candidate; probed under this exact ID |
| `kasari-voice` | Advertised; inference failed as detailed below |
| `kasari-voice-long` | Advertised; inference failed as detailed below |

The brief's `kasari-coder`, `kasari-judgment`, and `kasari-v4flash` IDs were absent. No inference request was sent for these missing IDs and no replacement was selected. The picker must use the live catalog, retain an explicitly blank selection, and distinguish a missing selected model from a failed catalog request.

## Observed capabilities

“Passed” below means the specified bounded live probe succeeded; it is not a guarantee for every schema, prompt, or future router configuration.

| Model | Image content parts | Strict JSON schema | Native tools and unchanged-ID replay | Streaming |
| --- | --- | --- | --- | --- |
| `kasari-brain` | Rejected, HTTP 400; configured route reports unsupported multimodal model | Passed adversarial constraint probe | Passed; valid function arguments, nonempty ID, result replay accepted | Text and native tool deltas passed |
| `kasari-flash` | Passed HTTP 200; correct color and spatial-layout answers | Passed adversarial constraint probe and combined image/schema probe | Passed, including replay using only standard message fields | Text and native tool deltas passed |
| `kasari-voice` | HTTP 429 after the authorization failure; unverified | HTTP 401; unverified | HTTP 429; no valid call to replay | Not probed after inference failures |
| `kasari-voice-long` | HTTP 429 after the authorization failure; unverified | HTTP 401; unverified | HTTP 429; no valid call to replay | Not probed after inference failures |
| `kasari-coder`, `kasari-judgment`, `kasari-v4flash` | Absent from catalog | Absent from catalog | Absent from catalog | Absent from catalog |

The same router credential succeeded for `kasari-brain` and `kasari-flash`. The voice-route 401s therefore represent a route-specific authorization problem observed through the router; this probe did not independently inspect the upstream credential configuration. The later 429s were recorded without assuming their cause or treating them as evidence that images/tools are unsupported. Router-wide health and per-model inference health need separate reporting.

### Schema enforcement

Requests used standard `response_format`:

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "director_probe",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {
        "kind": {"type": "string", "enum": ["director_probe"]},
        "count": {"type": "integer", "enum": [7]}
      },
      "required": ["kind", "count"],
      "additionalProperties": false
    }
  }
}
```

The user message deliberately requested a conflicting plain prose sentence and no JSON. Both successful models returned valid objects meeting the required keys, enum values, integer type, and no-extra-properties constraint with `finish_reason=stop`. This verifies enforcement for the tested schema, rather than merely accepting the parameter. It does not certify the full JSON Schema vocabulary or Director's complete planning schemas; those need adapter integration tests.

`kasari-brain` used 22 completion tokens; `kasari-flash` used 148 and returned a separate `reasoning_content` field. No reasoning-control parameter was required.

### Vision and completion limits

The initial image request used standard message content parts: a text instruction and an `image_url` containing a synthetic 64×64 solid-red PNG as a data URL. `kasari-flash` returned the correct color with HTTP 200 and `finish_reason=stop`; `kasari-brain` rejected this same image request. Diagnostic repeats of the brain rejection only classified the sanitized error; they did not remove the image or change the model/protocol.

A second flash image was 192×96 pixels, green on the left and magenta on the right. The prompt supplied no expected colors and requested a schema-constrained `{left, right}` answer. Its **256-token** attempt returned HTTP 200 but exhausted the entire budget in reasoning, with `finish_reason=length` and empty final content. That is an incomplete generation, not successful analysis or malformed final JSON to pretend was complete.

An explicitly recorded diagnostic using the same model, image, prompt, schema, and protocol with a **512-token** cap completed correctly, using 446 completion tokens. Both spatial colors and the output schema validated. Application budgets must account for reasoning tokens; an HTTP 200 alone must never qualify as a successful planning/vision response.

These checks demonstrate image acceptance and basic visual correctness. They do not certify production storyboard or shot-layout judgment quality.

### Native tools and streaming

The tool probe required one harmless `director_probe_sum(left=6, right=7)` function call using standard `tools` and `tool_choice="required"`. Both successful models returned the expected function, valid JSON arguments, and one nonempty call ID. The follow-up sent a `role="tool"` message with the **original ID unchanged** and the result `13`; both models completed with the correct numeric response.

“Stable ID” means preserving and accepting an ID across its tool-result exchange, not requiring independent generations to reuse an ID. Flash also passed a separate replay containing only standard assistant fields (`role`, `content`, `tool_calls`) and standard tool-call fields (`id`, `type`, `function`). Its response's `reasoning_content` was deliberately omitted from that replay, proving that it is unnecessary for this tested exchange.

SSE probes for both models returned text deltas and `[DONE]` with `finish_reason=stop`. Separate native-tool SSE probes reconstructed the correct function and JSON arguments, maintained a nonempty stable ID per call index across deltas, and ended with `finish_reason=tool_calls` and `[DONE]`. No interrupted-stream recovery or application-level tool loop was tested.

## Request fields and adapter implications

Successful requests required only standard fields: `model`, `messages`, `max_tokens`, `temperature`, `stream`, `response_format`, `tools`, and `tool_choice`. Vision used standard `image_url` content parts and tool results used standard `tool_call_id`. **No nonstandard top-level fields and no `extra_body` values were needed.** The initial tool replay copied response messages; the separate standard-fields-only flash replay removed any ambiguity about its additional reasoning field.

If later adapter requirements introduce backend-specific fields such as `chat_template_kwargs` or a provider-specific thinking control, they must be explicitly configured and passed through the SDK's `extra_body`, then verified separately. Do not carry Ollama's `options`, `format`, `keep_alive`, or top-level `images` request shapes into this provider. A response may contain `reasoning_content`; its presence must not cause empty final `content` to be treated as a completed answer.

The Phase 2 design should now:

1. Use the exact selected ID and provider-scoped persistence; never auto-persist the first catalog entry, alias a missing ID, or choose flash when brain fails.
2. Gate visual features by explicit, verified capability for the selected model and show a clear reason when disabled. Brain's text planning remains usable while its visual features are disabled. Unknown capabilities must not be inferred from model names.
3. Keep native tools and standard tool-ID replay, without XML-protocol fallback. Preserve partial stream errors instead of restarting a nonstream request.
4. Validate complete response shape, finish reason, and output contract. Preserve the authorized deterministic reparse and single same-model JSON repair paths for completed malformed output; do not use them to hide unavailable models, image rejection, authorization failures, or truncated generations.
5. Test real Director planning schemas, streaming integration, tool loops, and visual prompts before claiming application-level completion. No adapter or application code was written for this reconnaissance.

## Remaining operational limits

- Repair the voice routes' inference authorization outside this task if those models are intended for Director use; their catalog visibility alone is insufficient.
- `kasari-flash` is the currently demonstrated full-feature candidate. An explicit configuration or picker selection is still required; this document changes neither.
- The live router can change. Do not bake this catalog snapshot into a fallback list or treat these observations as permanent capabilities for a reused model alias.
- These probes did not submit renders, import workflows, or access any ComfyUI port. They do not establish Phase 3–5 readiness.

## Implemented-adapter checks

After implementation, the actual `OpenAICompatibleClient` was tested from macOS against the same authenticated endpoint. The key stayed in a captured SSH pipe and process memory. No production configuration or model selection was persisted.

With the explicitly chosen `kasari-flash` and a 4096-token budget, the adapter passed catalog discovery, text completion, adversarial strict schema, the green/magenta image with schema, a native function call, standard tool-result replay with the original ID, and a complete SSE response. The application's unit/integration tests additionally check interrupted streams, rejected/unfinished completions, unchanged tool IDs across the Director loop, and image/capability failures without retries.

A real `DirectorService.plan_project` request with the shipped Director skill and planning guide, a synthetic kitchen scene, and an isolated empty asset library exhausted the 4096-token cap (`finish_reason=length`). The adapter raised the specific truncation error and did not attempt JSON repair or write a partial shot plan. The explicit same-model, same-prompt diagnostic at 8192 tokens completed with three persisted shots, each containing the required shot type, camera angle, and composition. The model stayed pinned to `kasari-flash`; no render was submitted. This was an operator diagnostic, not runtime automatic retry behavior. Use `DS_DIRECTOR_NUM_PREDICT=8192` for this verified cluster planning configuration; larger scripts may still need an explicitly reviewed budget. The application default remains 4096 for existing installations.

## Fresh vision-route discovery — 2026-09-08 03:41 UTC

A new authenticated catalog request, following the user's vision-routing update, returned **HTTP 200** from `http://100.124.186.11:4000/v1/models`. The exact advertised IDs were:

- `kasari-brain`
- `kasari-embed`
- `kasari-flash`
- `kasari-voice`
- `kasari-voice-long`

**Neither `kasari-vision` nor `kasari-vision-deep` was advertised by this endpoint.** No inference request was sent to either absent ID, and no alias was substituted. This is a fresh discovery result, not an inference that those names cannot exist elsewhere or cannot be added to this router.

The prepared probe uses an **8,192-token completion budget** for each served candidate and checks adversarial strict JSON-schema enforcement, a synthetic two-color image through standard `image_url` content parts plus `response_format`, and native tools with unchanged-ID replay only after both required checks pass. Those inference checks did not run because their exact candidate IDs were absent. Consequently there are no new prompt/completion/reasoning token measurements or vision capability results for these two names. Prior `kasari-flash` observations remain specific to that exact route and do not establish either requested vision route's capability.

Credential handling remained private: the existing key was read from the host-side `/opt/kasari/config/litellm.env` through verified SSH to `beast6` into captured pipe/process memory. Only sanitized catalog/probe summaries were emitted. No key, raw response body, or model reasoning was printed or saved; no production settings, model selection, remote service, Comfy endpoint, or backend direct IP was modified or contacted.

## Instrumented 8192-token Director planning pass

A subsequent explicitly requested measurement ran the same real Director planning path against the live router with `DS_DIRECTOR_NUM_PREDICT=8192`. Selection was configured in the isolated smoke environment, with no model-picker persistence or production data changes.

| Recorded field | Value |
| --- | --- |
| Exact requested/catalog model | `kasari-flash` |
| Selection source | `env` |
| Completion cap | 8192 |
| Finish reason | `stop` |
| Prompt tokens | 3349 |
| Completion tokens | 1678 |
| Reasoning tokens | 937 |
| Payload tokens | 741 |
| Total tokens | 5027 |
| Persisted result in isolated data | 3 shots with required camera fields |

Reasoning tokens came from the router's `usage.completion_tokens_details.reasoning_tokens`. Payload tokens are completion minus reasoning, with both counters present and consistent. No reasoning text was retained or logged. This is a measurement of that run, not a guaranteed budget for every screenplay. The earlier 4096-token truncation remains recorded rather than being overwritten by the successful diagnostic.

The implemented stream-accounting path also passed a fresh live check using the standard `stream_options.include_usage=true`. LiteLLM emits an empty-delta choice with usage after the finish event; the parser explicitly accepts that accounting-only shape while rejecting additional content/tools or changed termination. The measured short stream used `kasari-flash`, source `explicit_request`, cap 8192, prompt 59, completion 29 (reasoning 25, payload 4), and `finish_reason=stop`.
