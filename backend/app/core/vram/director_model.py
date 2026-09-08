"""Provider-scoped runtime selections persisted in data/director_model.json.

Priority within the selected provider: runtime override, persisted selection,
DS_LLM_MODEL, then legacy DS_DIRECTOR_PLAN_MODEL for Ollama only. An explicitly
blank persisted selection stays blank. Legacy unscoped files belong to Ollama.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ...config import settings

_override: dict[str, str] | None = None
_PROVIDERS = {"ollama", "openai_compatible"}


class ModelSelectionError(RuntimeError):
    """Malformed or unreadable selection state must not select another model."""


def _persist_path() -> Path:
    return settings.data_dir / "director_model.json"


def _read_selections() -> tuple[dict[str, str], bool]:
    path = _persist_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    except (OSError, UnicodeError):
        raise ModelSelectionError("Director model selection file cannot be read.") from None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError
        if "version" not in data and set(data) == {"model"}:
            if not isinstance(data["model"], str):
                raise ValueError
            return {"ollama": data["model"].strip()}, True
        if data.get("version") != 2 or not isinstance(data.get("selections"), dict):
            raise ValueError
        selections = data["selections"]
        if any(key not in _PROVIDERS or not isinstance(value, str) for key, value in selections.items()):
            raise ValueError
        return {key: value.strip() for key, value in selections.items()}, False
    except (ValueError, TypeError):
        raise ModelSelectionError("Director model selection file is malformed; repair it explicitly.") from None


def _write_selections(selections: dict[str, str]) -> None:
    path = _persist_path()
    temporary = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"version": 2, "selections": selections}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        raise ModelSelectionError("Director model selection could not be persisted.") from None


def _provider(provider: str | None = None) -> str:
    selected = provider or settings.llm_provider
    if selected not in _PROVIDERS:
        raise ModelSelectionError("Unknown Director LLM provider.")
    return selected


def model_status(*, provider: str | None = None) -> dict[str, Any]:
    selected = _provider(provider)
    selections, legacy = _read_selections()
    override = (_override or {}).get(selected)
    persisted = selections.get(selected)
    env_default = settings.llm_model.strip()
    if not env_default and selected == "ollama":
        env_default = settings.director_plan_model.strip()
    return {
        "provider": selected,
        "model": override if override is not None else persisted if persisted is not None else env_default,
        "override": override,
        "persisted": persisted,
        "env_default": env_default,
        "source": "runtime" if override is not None else "persisted" if persisted is not None else "env",
        "warning": (
            "Legacy Ollama model selection was ignored for the OpenAI-compatible provider."
            if legacy and selected != "ollama" else None
        ),
    }


def get_director_model() -> str:
    return model_status()["model"]


def set_director_model(model: str, *, persist: bool = True, provider: str | None = None) -> str:
    global _override
    selected = _provider(provider)
    name = model.strip()
    selections, _ = _read_selections()
    if persist:
        _write_selections({**selections, selected: name})
    _override = {**(_override or {}), selected: name}
    # Synchronize an existing orchestrator without creating GPU machinery merely
    # to select a remote model. The policy owns all residency operations.
    from . import orchestrator
    if selected == settings.llm_provider and orchestrator._orchestrator is not None:
        orchestrator._orchestrator.models = [name] if name else []
        orchestrator._orchestrator._llm_ready = False
    return name


def clear_director_model_override(*, remove_persisted: bool = False) -> str:
    global _override
    selected = _provider()
    if remove_persisted:
        selections, _ = _read_selections()
        selections.pop(selected, None)
        _write_selections(selections)
    _override = {key: value for key, value in (_override or {}).items() if key != selected}
    return get_director_model()
