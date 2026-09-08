from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX launcher contract")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "Director Studio with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "backend" / ".venv" / "bin").mkdir(parents=True)
    (root / "frontend").mkdir()
    shutil.copy2(REPO_ROOT / "start.sh", root / "start.sh")
    shutil.copy2(REPO_ROOT / "scripts" / "setup.sh", root / "scripts" / "setup.sh")
    (root / "data").mkdir()
    (root / "data" / "preserved.json").write_text('{"retained": true}\n')
    (root / ".run").mkdir()
    (root / ".run" / "backend.out.log").write_text("prior durable output\n")
    return root


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["DS_PYTHON_EXE"] = sys.executable
    return env


def _assert_preserved(root: Path) -> None:
    assert (root / "data" / "preserved.json").read_text() == '{"retained": true}\n'
    assert (root / ".run" / "backend.out.log").read_text() == "prior durable output\n"
    assert not (root / "backend" / "env-was-executed").exists()


@pytest.mark.parametrize("termination", ["raise SystemExit(7)", "os.kill(os.getpid(), 15)"])
def test_start_runs_foreground_from_other_directory_and_preserves_state(
    tmp_path: Path, termination: str
) -> None:
    root = _workspace(tmp_path)
    (root / "backend" / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (root / "frontend" / "dist").mkdir()
    (root / "frontend" / "dist" / "index.html").write_text("built interface")
    package = root / "backend" / "app"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "main.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path('invocation.json').write_text(json.dumps({\n"
        "    'cwd': os.getcwd(), 'venv': os.environ['VIRTUAL_ENV'],\n"
        "    'path': os.environ['PATH'].split(os.pathsep)[0],\n"
        "    'unbuffered': os.environ['PYTHONUNBUFFERED'],\n"
        "    'port': os.environ['DS_PORT'],\n"
        "}))\n"
        f"{termination}\n"
    )
    env_file = root / "backend" / ".env"
    config = "DS_PORT=9999\nDS_UNUSED=$(touch env-was-executed)\n"
    env_file.write_text(config)
    env = _env()
    env["DS_PORT"] = "9876"

    result = subprocess.run(
        [str(root / "start.sh")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=15, check=False,
    )

    assert result.returncode == (7 if termination.startswith("raise") else -signal.SIGTERM)
    invocation = json.loads((root / "backend" / "invocation.json").read_text())
    assert invocation == {
        "cwd": str(root / "backend"),
        "venv": str(root / "backend" / ".venv"),
        "path": str(root / "backend" / ".venv" / "bin"),
        "unbuffered": "1",
        "port": "9876",
    }
    assert env_file.read_text() == config
    _assert_preserved(root)


def test_start_reports_missing_build_without_starting_backend(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "backend" / ".venv" / "bin" / "python").symlink_to(sys.executable)

    result = subprocess.run(
        [str(root / "start.sh")], cwd=tmp_path, env=_env(),
        capture_output=True, text=True, timeout=15, check=False,
    )

    assert result.returncode == 1
    assert "browser interface has not been built" in result.stderr
    assert "scripts/setup.sh" in result.stderr
    _assert_preserved(root)


@pytest.mark.parametrize("existing_env", [False, True])
def test_setup_preserves_data_and_config_and_seeds_missing_pip(
    tmp_path: Path, existing_env: bool
) -> None:
    root = _workspace(tmp_path)
    backend = root / "backend"
    template = "DS_HOST=127.0.0.1\n"
    (backend / ".env.example").write_text(template)
    (backend / "requirements.txt").write_text("runtime-dependency\n")
    (backend / "requirements-dev.txt").write_text("-r requirements.txt\ntest-dependency\n")
    env_file = backend / ".env"
    config = "DS_PORT=9999\nDS_UNUSED=$(touch env-was-executed)\n"
    if existing_env:
        env_file.write_text(config)
        env_file.chmod(0o640)
    tools = root / "fake tools"
    tools.mkdir()
    calls = root / "calls.jsonl"
    stub = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).stem\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['TEST_CALLS']).open('a') as stream:\n"
        "    stream.write(json.dumps([name, args, os.getcwd()]) + '\\n')\n"
        "if name == 'python':\n"
        "    if args == ['-m', 'pip', '--version']: sys.exit(1)\n"
        "    if args[:1] == ['-c']: exec(args[1])\n"
        "if name == 'npm' and args == ['run', 'build']:\n"
        "    Path('dist').mkdir(exist_ok=True)\n"
        "    Path('dist/index.html').write_text('built interface')\n"
    )
    for tool in [backend / ".venv" / "bin" / "python", *(
        tools / name for name in ("node", "npm", "ffmpeg", "ffprobe")
    )]:
        tool.with_suffix(".py").write_text(stub)
        tool.write_text('#!/bin/sh\nexec "$DS_PYTHON_EXE" "$0.py" "$@"\n')
        tool.chmod(0o755)
    env = _env()
    env["PATH"] = f"{tools}{os.pathsep}{env['PATH']}"
    env["TEST_CALLS"] = str(calls)

    result = subprocess.run(
        [str(root / "scripts" / "setup.sh"), "--dev"], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=15, check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    python_args = [args for name, args, _cwd in commands if name == "python"]
    assert ["-m", "ensurepip", "--upgrade"] in python_args
    assert ["-m", "pip", "install", "-r", str(backend / "requirements-dev.txt")] in python_args
    assert [(args, cwd) for name, args, cwd in commands if name == "npm"] == [
        (["ci"], str(root / "frontend")),
        (["run", "build"], str(root / "frontend")),
    ]
    assert (root / "frontend" / "dist" / "index.html").is_file()
    assert env_file.read_text() == (config if existing_env else template)
    assert env_file.stat().st_mode & 0o777 == (0o640 if existing_env else 0o600)
    _assert_preserved(root)
