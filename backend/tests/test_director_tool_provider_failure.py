"""Operational inference errors must terminate the native tool loop, without repair."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.director.chat import handle_chat
from app.agents.director.context_io import save_agent_context
from app.agents.director.service import _script_hash
from app.config import settings
from app.core.llm import LLMProviderError
from app.core.projects.layouts import LayoutReference
from app.core.projects.models import AgentContext, Shot
from app.core.projects.store import create_project, save_project, save_shot
from app.core.vram.director_model import ModelSelectionError


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["save_storyboard", "plan_shots", "write_prompt", "accept_ref_frame"])
@pytest.mark.parametrize("error_type", [LLMProviderError, ModelSelectionError])
async def test_native_tool_provider_failure_does_not_start_another_model_turn(
    tmp_projects_dir, tmp_path, monkeypatch, tool_name, error_type
):
    monkeypatch.setattr(settings, "library_root", tmp_path / "library")
    project = create_project("Provider failure", "A visitor enters the room.")
    shot = Shot(
        id="sht_provider_failure", project_id=project.id, scene_id="sc01",
        title="Opening", script_beat="A visitor enters.", duration_s=5,
        layout_refs=[LayoutReference(id="lr_provider_failure", asset_id="lay_provider_failure")],
    )
    save_shot(shot)
    save_project(project.model_copy(update={"shot_ids": [shot.id]}))
    save_agent_context(project.id, AgentContext(project_id=project.id, script_hash=_script_hash(project.script_text)))
    error = error_type("The configured model provider is unavailable.")
    operation = AsyncMock(side_effect=error)
    svc = SimpleNamespace(save_storyboard=operation, plan_project=operation, write_prompts_after_layout=operation)
    arguments = {
        "save_storyboard": {
            "expected_script_hash": _script_hash(project.script_text),
            "shots": [{
                "scene_id": "sc01", "title": "Opening", "script_beat": "A visitor enters.",
                "shot_type": "medium shot", "camera_angle": "eye level",
                "camera_motion": "locked-off", "composition": "visitor centered",
            }],
        },
        "plan_shots": {},
        "write_prompt": {"shot_id": shot.id},
        "accept_ref_frame": {"shot_id": shot.id, "layout_ref_id": "lr_provider_failure"},
    }[tool_name]
    chat_fn = AsyncMock(side_effect=[
        {"content": "", "thinking": "", "tool_calls": [{
            "id": "stable_native_call", "name": tool_name, "arguments": arguments,
        }]},
        AssertionError("Provider failure must not become a model repair turn"),
    ])

    with pytest.raises(error_type) as caught:
        await handle_chat(
            project_id=project.id, message="Please carry out the next production action.",
            svc=svc, chat_fn=chat_fn,
        )

    assert caught.value is error
    operation.assert_awaited_once()
    chat_fn.assert_awaited_once()
