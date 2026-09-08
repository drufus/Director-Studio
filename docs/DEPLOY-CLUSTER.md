# Deploy Director Studio on node #6

This deployment runs one native Python process on `thebeastvi` (`100.124.186.11`), serving the built React interface and API at `http://100.124.186.11:8790`. Open that address from the Mac while connected to Tailscale. The service binds to that exact Tailscale address; it does not require a public listener, proxy, or container.

The only configured render worker is `beastviii=http://100.93.117.98:8188`. The registry supports N workers, but no second worker is planned. The retired node #3 `:8189` instance must remain unconfigured. Node #3 `100.88.79.40:8188` is forbidden, including for health probes.

Complete the worker-bound 56-frame SPARK profile test and activation before deployment acceptance. The operator authorized deployment of the branch while retaining PR #4 review. Deploy a pinned branch revision and keep PR #4 unmerged; deployment does not imply that its review is complete.

## Layout and prerequisites

The 2026-09-08 preflight found Python 3.12.3 and venv support on node #6, writable `/opt/kasari` directories, and port 8790 unused. Node/npm and FFmpeg were absent from the SSH command PATH. Build the frontend on the Mac and ship `frontend/dist`; create a fresh Linux venv rather than copying the Mac venv or reusing another service's environment.

| Purpose | Path |
| --- | --- |
| Application | `/opt/kasari/director-studio` |
| Dedicated Python environment | `/opt/kasari/director-studio/backend/.venv` |
| Private runtime configuration | `/opt/kasari/config/director-studio.env`, mode `0600` |
| Durable jobs, library, projects, and workflow profiles | `/opt/kasari/data/director-studio` |
| Durable stdout and stderr | `/opt/kasari/logs/director-studio.log` |
| Installed service | `/etc/systemd/system/kasari-director-studio.service` |

The service runs as `damienrufus:damienrufus`, matching the cluster's native API service. The unit reads its env file through systemd and calls the existing foreground `start.sh`, which checks the venv and built UI before starting uvicorn with one process. There is no additional run wrapper and no startup removal of data or logs.

An administrator must install the missing media binaries on node #6:

```sh
sudo apt-get update
sudo apt-get install --yes --no-install-recommends ffmpeg
```

The preflight confirmed that general `sudo` requires authentication. Run privileged commands using the normal administrator login; the application user cannot complete unattended system-unit installation with the current grants. Do not work around that boundary through another service or Docker.

## Build and stage a pinned branch revision

On the Mac, run this from the repository. Replace the revision placeholder with the branch commit that includes the deployment files. Building from its archive makes the source and generated UI correspond to the same revision and excludes ignored credentials, runtime data, and local environments.

```sh
set -eu
DEPLOY_REV='<branch-commit-sha>'
DEPLOY_REV="$(git rev-parse --verify "${DEPLOY_REV}^{commit}")"
DEPLOY_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/director-release.XXXXXX")"
mkdir "$DEPLOY_STAGE/source"
git archive --format=tar --output="$DEPLOY_STAGE/source.tar" "$DEPLOY_REV"
tar -xf "$DEPLOY_STAGE/source.tar" -C "$DEPLOY_STAGE/source"
(
    cd "$DEPLOY_STAGE/source/frontend"
    npm ci
    npm run build
)
printf '%s\n' "$DEPLOY_REV" > "$DEPLOY_STAGE/source/RELEASE"
tar --exclude='./frontend/node_modules' -czf "$DEPLOY_STAGE/director-studio.tar.gz" -C "$DEPLOY_STAGE/source" .
scp "$DEPLOY_STAGE/director-studio.tar.gz" beast6:director-studio.tar.gz
```

On node #6, as `damienrufus`, stage the first installation:

```sh
set -eu
umask 077
install -d -m 0755 /opt/kasari/director-studio
install -d -m 0700 /opt/kasari/data/director-studio
tar --extract --gzip --file "$HOME/director-studio.tar.gz" --directory /opt/kasari/director-studio --no-same-owner
/usr/bin/python3 -m venv /opt/kasari/director-studio/backend/.venv
/opt/kasari/director-studio/backend/.venv/bin/python -m pip install --upgrade pip
/opt/kasari/director-studio/backend/.venv/bin/python -m pip install -r /opt/kasari/director-studio/backend/requirements.txt
touch /opt/kasari/logs/director-studio.log
chmod 0600 /opt/kasari/logs/director-studio.log
/usr/bin/ffmpeg -version
/usr/bin/ffprobe -version
```

The full `scripts/setup.sh` also builds the frontend and therefore requires Node/npm on the target. The explicit deployment steps above install only the target Python dependencies and use the Mac-built static UI.

Copy the accepted Mac calibration data root into `/opt/kasari/data/director-studio` while both app instances are stopped. Preserve the complete corresponding data tree so job evidence, asset files, rendered video, profile snapshots, and active selection remain together. Do not copy a venv, API credentials, or a Windows data tree. A fresh empty data root does not inherit the Mac's active profile or model-picker selection.

Copied job output slots contain absolute Mac paths. With `DS_DATA_DIR` pointing to the copied node #6 directory, load each copied job, retain its output labels, call `app.core.jobs.store.enrich_job_urls(job, labels=existing_labels)`, then `save_job(job)` to rebuild slots from the actual copied output files. Check that expected output files exist before saving. This preserves stable `/api/files/jobs/...` URLs; do not rewrite workflow graph content, snapshot hashes, worker proofs, or worker identity. This is a relocation of the accepted POSIX calibration data, not a general legacy-data migration.

## Private runtime configuration

The following is a redacted configuration example, not a file to run directly. `DS_LLM_API_KEY` is loaded privately from the existing host-side `/opt/kasari/config/litellm.env` `LITELLM_MASTER_KEY`. Do not print, log, place in command arguments, or commit that value.

```dotenv
DS_HOST=100.124.186.11
DS_PORT=8790
DS_RELOAD=false
DS_DATA_DIR=/opt/kasari/data/director-studio
DS_LLM_PROVIDER=openai_compatible
DS_LLM_BASE_URL=http://100.124.186.11:4000/v1
DS_LLM_API_KEY=<REDACTED>
DS_LLM_MODEL=kasari-flash
DS_LLM_VISION_MODELS=<EXACT_IDS_VERIFIED_BY_AUTHENTICATED_VISION_PROBE>
DS_DIRECTOR_NUM_PREDICT=8192
DS_VRAM_POLICY=independent
DS_H3_PROVIDER=local
DS_COMFY_WORKERS=beastviii=http://100.93.117.98:8188
DS_COMFY_MIN_FREE_VRAM_GIB=1
DS_COMFY_MEMORY_THRESHOLD_PROVISIONAL=true
DS_COMFY_MEMORY_SAMPLE_INTERVAL_SEC=1
DS_COMFY_MEMORY_REQUEST_TIMEOUT_SEC=30
DS_JOB_TIMEOUT_SEC=7200
```

The `1 GiB` VRAM threshold is deliberately provisional for the authorized 56-frame calibration test only; it is not a production requirement. Do not submit production H3 jobs using it. The operator will choose the final threshold from the test's sampled peak plus margin. Admission uses `/system_stats` `vram_free` alone. `ram_free` remains informational because these readings describe the same unified pool and diverge substantially; do not add them, average them, or impose a RAM admission threshold.

The job record retains submission and execution memory samples, the minimum free VRAM, sampled peak used VRAM, change from the submission baseline, and telemetry failures. Sampling measures the entire worker pool, including other workloads, and may miss instantaneous peaks. Once the operator supplies the production threshold, update `DS_COMFY_MIN_FREE_VRAM_GIB`, set `DS_COMFY_MEMORY_THRESHOLD_PROVISIONAL=false`, and restart the service. Do not silently select a threshold or substitute a worker.

The sampler waits one second after each completed request before the next execution sample; actual cadence includes the HTTP request duration. Each `/system_stats` sampling request has a 30-second timeout. The job retains request start times and durations, so a slow worker is visible as a longer sampling interval. A timeout or failed measurement remains a recorded telemetry error, and the calibration cannot claim complete peak evidence. Submission and final measurements are also retained; this is periodic sampling, not an instantaneous allocator peak.

Set `DS_LLM_VISION_MODELS` only to exact catalog IDs that passed the authenticated image-parts and JSON-schema probe on this router. Do not infer capability from names or substitute an unadvertised vision route. The initial Director model is explicitly `kasari-flash`; a provider-scoped picker selection in the durable data directory takes precedence, and model selection source is recorded in inference usage logs. `DS_VRAM_POLICY=independent` keeps Director chat available during remote renders.

Create the private env file with an editor on node #6 using mode `0600`, then have a host-side Python process read the existing router key and write the `DS_LLM_API_KEY` entry in memory. Do not source the file as shell code, display its contents, or copy the key through a terminal. Validate its permissions without reading values:

```sh
stat -c '%a %U:%G %n' /opt/kasari/config/director-studio.env
```

Expected: `600 damienrufus:damienrufus /opt/kasari/config/director-studio.env`. Keep `backend/.env` out of the deployment archive; the external systemd env file is the deployment configuration source.

## Install and start the service

These are the final administrator commands after staging, private configuration, and media prerequisites are complete:

```sh
sudo install -o root -g root -m 0644 /opt/kasari/director-studio/deploy/director-studio.service /etc/systemd/system/kasari-director-studio.service
sudo systemd-analyze verify /etc/systemd/system/kasari-director-studio.service
sudo systemctl daemon-reload
sudo systemctl enable --now kasari-director-studio.service
systemctl is-active kasari-director-studio.service
curl --fail --show-error http://100.124.186.11:8790/api/health/live
curl --fail --show-error http://100.124.186.11:8790/api/health | /usr/bin/python3 -c 'import json, sys; result = json.load(sys.stdin); assert result["ok"], "Readiness failed: inspect worker and model status in the UI"; print("Worker and selected model catalog checks passed.")'
```

The env file must exist and both `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` must be executable. A missing prerequisite fails startup visibly. If the Tailscale address is temporarily absent, binding fails and `Restart=on-failure` retries after five seconds. Do not change the bind to `0.0.0.0` to mask a Tailscale problem.

From the Mac, open `http://100.124.186.11:8790` and verify that the UI loads, the worker panel lists only beastviii, and the active H3 profile carries successful test evidence for its exact worker ID and URL. Verify the model picker with the authenticated router configuration, then run a real Director planning pass. `/api/health/live` checks only this API process. `/api/health` contacts configured workers and the authenticated model catalog, and requires the selected model to appear in that catalog. Check its JSON `ok` value; a returned HTTP 200 alone is insufficient. Neither endpoint runs actual inference or establishes custom workflow, media tool, or completed-render acceptance.

Run the authenticated `/v1/models` and image/schema capability probes from node #6 after deployment, loading the key from its mode-600 env file inside the probe process. Report exact roster IDs and allowlisted capability results only. Record the real `kasari-flash` planning call's 8192-token cap, selection source, reasoning tokens, payload tokens, and whether the request sends `chat_template_kwargs.enable_thinking=false` through the supported extra-body mechanism. A missing server token breakdown is unknown, not zero or an estimate. Do not log raw request headers, credentials, or full env data.

## Logs, data, and updates

```sh
systemctl status --no-pager kasari-director-studio.service
journalctl -u kasari-director-studio.service --since today --no-pager
tail -n 100 /opt/kasari/logs/director-studio.log
```

Application stdout and stderr append to `/opt/kasari/logs/director-studio.log`; systemd startup and restart events remain in the journal. Do not inspect service environment dumps or enable shell tracing when credentials are loaded. Jobs, render outputs, library assets, projects, model selection, and workflow profiles persist under `/opt/kasari/data/director-studio`.

`Restart=on-failure` combined with container-style teardown can destroy a previous runtime's logs. This unit has no teardown command and writes to durable host paths outside the application directory. A later packaging layer must preserve these paths and retain prior output before removing a container or release directory. This unit does not truncate logs on startup; arrange log retention with the cluster's normal log-management policy.

For updates, stop the service through the administrator's normal mechanism, back up the durable data root and private env file without displaying secrets, stage the selected pinned source and matching frontend build, update only this app's venv, then restart and repeat health/UI checks. Preserve the previous release archive for code rollback. Never delete the data or log directories as part of a restart or deployment. Do not run multiple Director API processes against the same data root; job ownership and execution are coordinated in one process.
