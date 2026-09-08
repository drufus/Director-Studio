# Node #6 deployment acceptance — 2026-09-08

This record distinguishes the staged application from the installed system service. PR #4 remains unmerged and under operator review; Phase 5 is stacked on it.

## Staged application

Source revision `3c182ef6e2694765498dd8d4ff2b79894765e3a5` was exported with `git archive`, and its frontend was installed and built on the Mac from that clean archive. The matching source and static build were copied to `/opt/kasari/director-studio` on ARM64 node #6. A dedicated Python 3.12 venv uses the repository requirements; no other service environment was changed.

The mode-600 `/opt/kasari/config/director-studio.env` was created by a host-side process that read `LITELLM_MASTER_KEY` from the existing private router env file and wrote the Director key directly. The key never crossed SSH stdout or appeared in command arguments. Logs append to `/opt/kasari/logs/director-studio.log`; data is external at `/opt/kasari/data/director-studio`.

All 32 copied calibration files matched their original SHA-256 digests before rebasing the successful job's output slot to the Linux path. Three historical jobs, the reference asset and project, immutable graphs/proofs, and the active SPARK profile were retained. A separate diagnostic video preserves the first render whose telemetry was incomplete. The active profile remains `custom-09fd6877d333efa09c0d79aa7a9eed1c-5864722557769b5c`, with only `beastviii=http://100.93.117.98:8188` eligible. No retired or forbidden endpoint was contacted.

The staged foreground app bound exactly to `100.124.186.11:8790`, using the same launcher and external env intended for systemd. A Mac-side request returned `/api/health` HTTP 200 with `ok=true`, one up worker, and an available `kasari-flash` model. The built UI loaded over Tailscale and its model picker displayed the authenticated roster. The copied test video also played from the node #6 file endpoint at 864×480 for 2.333333 seconds, with no browser playback error. This does not by itself establish a running system service.

The unit passed `systemd-analyze verify` on node #6. Actual installation awaits administrator commands: general `sudo` requires authentication and `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` are absent. See [the deployment guide](DEPLOY-CLUSTER.md) for exact installation, env, listener, log, and data paths. No privilege workaround or changes to unrelated services were attempted.

## Authenticated router and vision evidence

The probe ran **inside node #6** at 15:30 UTC against `http://100.124.186.11:4000/v1`, reading the credential from the private Director env. HTTP clients disabled ambient proxy use and redirects. Output contains allowlisted observations only, with no headers or raw error bodies.

Authenticated `GET /v1/models` returned HTTP 200 with these exact IDs:

- `kasari-brain`
- `kasari-embed`
- `kasari-flash`
- `kasari-voice`
- `kasari-voice-long`

Neither `kasari-vision` nor `kasari-vision-deep` was advertised. Their separately running cluster backends therefore have no verified route through this LiteLLM roster. They were not called by an unadvertised name, and the router configuration was not changed. This is a routing gap; it does not establish the capabilities of those physical model servers.

The explicitly selected, advertised `kasari-flash` route passed both fresh probes at an 8192-token cap:

| Probe | Result | Completion / reasoning / payload tokens |
| --- | --- | --- |
| Adversarial plain-prose instruction with strict `response_format.json_schema` requiring a fixed object | HTTP 200, exact required object, finish `stop` | 96 / 73 / 23 |
| Four-color image sent as an `image_url` content part, with strict quadrant schema | HTTP 200, all four colors correct, valid schema, finish `stop` | 352 / 321 / 31 |

The first probe's expected object was specified only by the schema, independently of its conflicting prose request. These results demonstrate schema behavior on the tested requests, not a guarantee for arbitrary schemas. Vision stays enabled for exact verified model `kasari-flash`; there is no disabled-vision fallback or implicit switch to another host/model. Durable sanitized evidence is `/opt/kasari/data/director-studio/diagnostics/router-roster-vision.json`.

## Planning diagnostic

A real node #6 request to `POST /api/projects/prj_211dcfcc0179/plan` returned HTTP 200 and exactly three shots with populated camera fields in 134.776 seconds. The diagnostic project is **Node six planning diagnostic**. Exact selected model: `kasari-flash`; selection source: `env`.

| Metric | Observed value |
| --- | --- |
| Completion cap | 8192 |
| Prompt tokens | 3334 |
| Completion tokens | 2568 |
| Reasoning tokens | 1837 |
| Payload tokens | 731 |
| Total tokens | 5902 |
| Finish / provider status | `stop` / `succeeded` |

This was one successful inference request, with no repair or increased-budget retry. The router supplied the reasoning breakdown; payload is completion minus reasoning. Sanitized evidence is `/opt/kasari/data/director-studio/diagnostics/planning-8192.json`, and the normal service usage log records the same metrics.

The current adapter's `_body` sends standard chat-completions fields; this planning request uses `model`, `messages`, `max_tokens`, and `stream`. It does **not** send `chat_template_kwargs`, `extra_body`, or `enable_thinking=false`; reasoning remains enabled by the route's defaults. No behavior change is implied by this diagnostic.

## Verification

The Phase 4 memory revision `6259bc9` passed native CI on macOS, Linux x64, and Linux ARM64: [run 34244988456](https://github.com/drufus/Director-Studio/actions/runs/34244988456). It contains 1,234 backend tests with the existing 16 Windows-only cases deselected and 236 frontend tests. The clean deployment archive's frontend build also passed.

A host-side exact-credential scan checked 313 deployed application, documentation, static UI, data, and log files and found zero occurrences of the runtime key. The expected private env file was excluded from that scan. The staged diff also passed the credential-pattern and whitespace checks.

## Remaining acceptance

- Administrator installation of FFmpeg and the system unit; systemd start/restart verification.
- Operator-selected production VRAM threshold and a production H3 job. The provisional 1 GiB threshold authorizes only 56-frame calibration tests.
- Dedicated vision routes require explicit LiteLLM routing if they are wanted; the existing verified flash vision path works.

The successful 56-frame test, activation, and cold/warm memory measurements are recorded in [H3 worker profiles](H3-WORKER-PROFILES.md). The earlier two-worker live requirement is superseded: no second worker is planned, while the registry retains N-worker support.
