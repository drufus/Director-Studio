"""Runtime Director model selection (no restart)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.vram import director_model as dm


@pytest.fixture(autouse=True)
def _isolate_model_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dm, "_override", None)
    monkeypatch.setattr(dm, "_persist_path", lambda: tmp_path / "director_model.json")
    yield
    monkeypatch.setattr(dm, "_override", None)


def test_missing_model_configuration_stays_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dm.settings, "director_plan_model", "")
    assert dm.get_director_model() == ""
    st = dm.model_status()
    assert st["source"] == "env"
    assert st["model"] == ""


def test_set_persists_and_reads_back():
    name = dm.set_director_model("ornith:35b", persist=True)
    assert name == "ornith:35b"
    assert dm.get_director_model() == "ornith:35b"
    # Simulate process restart: clear in-memory override, keep file
    dm._override = None
    assert dm.get_director_model() == "ornith:35b"
    assert dm.model_status()["source"] == "persisted"


def test_runtime_override_beats_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dm.settings, "director_plan_model", "env-model:latest")
    dm.set_director_model("ornith:35b", persist=False)
    assert dm.get_director_model() == "ornith:35b"
    assert dm.model_status()["source"] == "runtime"


def test_provider_scoped_persistence_survives_switches(monkeypatch):
    monkeypatch.setattr(dm.settings, "llm_provider", "ollama")
    dm.set_director_model("local:tag")
    monkeypatch.setattr(dm.settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(dm.settings, "llm_model", "remote-env")
    assert dm.get_director_model() == "remote-env"
    dm.set_director_model("remote-picker")
    dm._override = None
    assert dm.get_director_model() == "remote-picker"
    monkeypatch.setattr(dm.settings, "llm_provider", "ollama")
    assert dm.get_director_model() == "local:tag"


def test_legacy_ollama_selection_is_ignored_and_reported_for_remote(monkeypatch):
    dm._persist_path().write_text('{"model":"old:ollama"}')
    monkeypatch.setattr(dm.settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(dm.settings, "llm_model", "remote-explicit")
    status = dm.model_status()
    assert status["model"] == "remote-explicit"
    assert status["source"] == "env"
    assert "Legacy Ollama" in status["warning"]
    monkeypatch.setattr(dm.settings, "llm_provider", "ollama")
    assert dm.get_director_model() == "old:ollama"


@pytest.mark.parametrize("value", ['invalid json', '[]', '{"model":34}', '{"version":2,"selections":{"other":"model"}}'])
def test_malformed_selection_never_falls_back(monkeypatch, value):
    dm._persist_path().write_text(value)
    monkeypatch.setattr(dm.settings, "llm_model", "env-model")
    with pytest.raises(dm.ModelSelectionError, match="malformed"):
        dm.get_director_model()


def test_blank_selection_stays_blank_and_clear_removes_only_active_provider(monkeypatch):
    monkeypatch.setattr(dm.settings, "llm_provider", "ollama")
    dm.set_director_model("local")
    monkeypatch.setattr(dm.settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(dm.settings, "llm_model", "remote-env")
    dm.set_director_model("")
    dm._override = None
    assert dm.get_director_model() == ""
    assert dm.model_status()["source"] == "persisted"
    assert dm.clear_director_model_override(remove_persisted=True) == "remote-env"
    monkeypatch.setattr(dm.settings, "llm_provider", "ollama")
    assert dm.get_director_model() == "local"
