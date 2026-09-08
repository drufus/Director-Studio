# Native macOS and Linux setup

This is the native launch/build path for the cluster port. The API and browser UI run on macOS or Linux. Phase 2 adds [LiteLLM planning and independent GPU coordination](LLM-PROVIDERS.md). Remote generation and cluster deployment remain later phases; Comfy still uses the legacy MCP transport. A successful startup check alone does not establish inference readiness.

The [accepted cluster decisions](CLUSTER-PORT-PLAN.md) record the subsequent inference, worker, H3, and chat-admission requirements.

## Install and build

Use Python 3.11+ (3.12 is the CI baseline), Node.js 22.12+ with npm, FFmpeg, and ffprobe. Both media binaries are required for voice reference normalization and tail-frame extraction. Ubuntu/DGX OS needs the Python venv package, normally `python3-venv`. Install native ARM64 dependencies on a Spark; do not copy a Mac virtualenv or `node_modules` to Linux.

From the repository root:

```bash
./scripts/setup.sh --dev
```

Omit `--dev` to install runtime dependencies only. The script installs into `backend/.venv`, installs frontend dependencies with `npm ci`, and runs `npm run build`. It creates `backend/.env` with private permissions when the file is absent. Existing configuration, data and logs are preserved. It never sources dotenv files as shell commands or prints their contents.

Select a Python executable during initial setup with:

```bash
DS_PYTHON_EXE=/path/to/python3.12 ./scripts/setup.sh --dev
```

An existing virtualenv is reused. To change its Python version, create a fresh environment deliberately rather than copying one between machines. Setup does not install Ollama, ComfyUI, CUDA, or model files. The current Python requirements still contain legacy MCP tooling; its removal from the cluster runtime belongs to the direct HTTP transport phase.

## Start and stop

```bash
./start.sh
```

Open `http://127.0.0.1:8790`. The launcher can be called from another working directory, including a checkout path containing spaces. It places the venv on `PATH`, changes to the backend directory, and replaces itself with the Python server process. Ctrl-C stops it; a supervisor can send SIGTERM to the same PID. No listener is killed merely because it occupies a port, and no log directory is deleted or truncated on startup.

`DS_HOST`, `DS_PORT`, and other `DS_*` settings come from `backend/.env` or the process environment. Exported variables take precedence. The service defaults to loopback, one worker, and no reload. An environment setting such as `WEB_CONCURRENCY=4` cannot add application workers. The application job registry and GPU reservations are process-local; replicas are out of scope.

For a custom local port and a separate persistent data directory:

```bash
DS_PORT=8791 DS_DATA_DIR=/absolute/path/to/director-data ./start.sh
```

All default persistent subdirectories derive from `DS_DATA_DIR`. Existing explicit per-directory overrides continue to work. Use an absolute path writable by the service account. The future node #6 service will use a fresh data root, so legacy Windows path migration is deferred.

No detached background process manager or port-killing script is needed. The node #6 systemd unit, Tailscale listener, mode-600 env file under `/opt/kasari/config/`, and durable logging will be delivered in the deployment phase, following existing `kasari-*.service` conventions. It will use `Restart=on-failure` without a teardown that destroys prior logs.

## Develop the UI and API

For backend reload:

```bash
DS_RELOAD=true ./start.sh
```

For frontend hot reload, run in another terminal:

```bash
cd frontend
npm run dev
```

Use `http://127.0.0.1:5173`. Vite proxies `/api` to the backend on `127.0.0.1:8790`. Keep that default backend port when using the existing proxy. The deployed mode serves the built frontend from FastAPI on the API port; it does not run Vite as a service.

## Verify the native path

```bash
(cd backend && .venv/bin/python -m pytest -q)
(cd frontend && npm test && npm run build)
```

`GET /api/health/live` returns `{"ok": true}` when this service is running, without making requests to inference providers. `GET /` serves the built UI. The existing `/api/health` endpoint reports external provider availability and may wait on those providers; it is not used for native launch smoke tests. It reports the configured LLM provider/model separately from Comfy readiness.

The native CI matrix runs Python 3.12 on macOS ARM64, Linux x64, and Linux ARM64, with Node 22 and FFmpeg. It runs the setup script from a clean checkout, the full native backend suite, frontend tests/build, and a real startup/UI/liveness/graceful-shutdown smoke check with an isolated data directory. No inference endpoints, credentials, Windows packaging tools, or Docker daemon are required for those checks.

### Windows-only quarantine

The registered `legacy_windows` marker excludes 16 legacy installer/packaging cases from default native runs:

- `test_comfy_mcp_client.py::test_server_parameters_resolve_installed_windows_entrypoints`: assumes Windows `.exe` entrypoints.
- All five tests in `test_portable_tools_installer.py`: `test_installer_creates_env_with_local_tool_paths`, `test_installer_paths_survive_dotenv_parsing`, `test_installer_replaces_existing_mcp_configuration`, `test_dependency_verification_does_not_start_mcp_server`, and `test_cmd_wrapper_returns_python_installer_failure`. These cover the retired Windows installer and its `Scripts/*.exe` or `ComSpec` assumptions.
- Ten parameterized PowerShell/archive-viewer cases in `test_packaged_runtime_paths.py`: `test_workflow_archive_check_rejects_external_h3_profile_state`, `test_h3_archive_check_accepts_official_only_executable_and_zip`, `test_h3_archive_check_rejects_external_state_in_zip`, `test_h3_archive_check_rejects_external_data_in_executable`, `test_archive_checks_reject_non_exact_official_h3_entry`, and `test_archive_checks_accept_exact_pyinstaller_official_h3_entry`.

Generic Python tests for source/frozen path resolution and the static removed-module assertion still run. The `.ps1`, `.cmd`, PyInstaller spec, and portable-build requirement file remain in the repository but are outside the native CI and cluster build path. No Windows runner is added. The marker records the exclusion explicitly rather than hiding platform failures.
