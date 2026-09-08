"""Provider and visual-input failures must reach the API as failed turns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agents.director import chat_orchestrator, vision
from app.agents.director.chat import handle_chat
from app.core.llm import LLMProviderError
from app.core.projects.models import Shot, ShotStatus
from app.core.projects.store import create_project, save_shot
from app.core.vram.director_model import ModelSelectionError


@pytest.fixture
def project_shot(tmp_projects_dir):
    project = create_project("Provider failures", "A director enters an empty studio.")
    shot = Shot(
        id="shot-test",
        project_id=project.id,
        title="Entering the studio",
        scene_id="studio",
        duration_s=6,
        script_beat=project.script_text,
        status=ShotStatus.needs_review,
    )
    save_shot(shot)
    return project, shot


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [LLMProviderError, ModelSelectionError])
@pytest.mark.parametrize("intent", ["plan", "write_prompt", "ref_frame", "ref_frame_all"])
async def test_shortcut_provider_errors_propagate(project_shot, error_type, intent):
    project, shot = project_shot
    failure = error_type("Selected provider is unavailable.")
    service = SimpleNamespace(
        plan_project=AsyncMock(side_effect=failure),
        write_prompts_after_layout=AsyncMock(side_effect=failure),
        queue_ref_frames=AsyncMock(side_effect=failure),
    )
    with pytest.raises(error_type) as error:
        await chat_orchestrator._execute_intent(
            intent=intent,
            params={"shot_id": shot.id},
            project_id=project.id,
            message=intent,
            svc=service,
        )
    assert error.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [LLMProviderError, ModelSelectionError])
async def test_approval_prompt_failure_is_not_reported_as_completed(
    project_shot, monkeypatch, error_type
):
    _project, shot = project_shot
    failure = error_type("Selected provider is unavailable.")
    service = SimpleNamespace(write_prompts_after_layout=AsyncMock(side_effect=failure))
    monkeypatch.setattr(chat_orchestrator, "apply_transition", lambda value, event: value)
    with pytest.raises(error_type) as error:
        await chat_orchestrator._approve_layout_with_prompt(shot, svc=service)
    assert error.value is failure


@pytest.fixture
def visual_turn(project_shot, monkeypatch):
    project, _shot = project_shot
    monkeypatch.setattr(chat_orchestrator, "detect_intent", lambda *args, **kwargs: ("llm", {}))
    monkeypatch.setattr(vision, "wants_vision", lambda _: True)
    monkeypatch.setattr(vision, "layout_reference_ids_from_message", lambda *args: [])
    chat = AsyncMock(return_value="Reviewed the supplied images.")

    async def run():
        return await handle_chat(
            project_id=project.id,
            message="Review these Layouts",
            svc=SimpleNamespace(),
            chat_fn=chat,
        )

    return chat, run


@pytest.mark.asyncio
async def test_visual_packaging_exception_does_not_continue_as_text(
    visual_turn, monkeypatch, caplog
):
    chat, run = visual_turn
    unsafe = "PRIVATE_IMAGE_ERROR_DETAILS"
    monkeypatch.setattr(vision, "collect_vision_attachments", Mock(side_effect=OSError(unsafe)))
    with pytest.raises(LLMProviderError, match=r"image preparation failed \(OSError\)"):
        await run()
    chat.assert_not_called()
    assert unsafe not in caplog.text


@pytest.mark.asyncio
async def test_visual_review_with_no_readable_images_fails_before_inference(
    visual_turn, monkeypatch
):
    chat, run = visual_turn
    monkeypatch.setattr(
        vision, "collect_vision_attachments",
        lambda **kwargs: {"images_b64": [], "note": "No readable layout image was found."},
    )
    with pytest.raises(LLMProviderError, match="No readable layout image"):
        await run()
    chat.assert_not_called()


@pytest.mark.asyncio
async def test_requested_layout_cannot_be_silently_omitted(visual_turn, monkeypatch):
    chat, run = visual_turn
    monkeypatch.setattr(vision, "layout_reference_ids_from_message", lambda *args: ["layout-a", "layout-b"])
    monkeypatch.setattr(
        vision, "collect_vision_attachments",
        lambda **kwargs: {
            "images_b64": ["first-image"],
            "captions": ["Image 1: Layout layout-a"],
            "note": "Attached one image.",
        },
    )
    with pytest.raises(LLMProviderError, match="requested Layouts: layout-b"):
        await run()
    chat.assert_not_called()


@pytest.mark.asyncio
async def test_complete_visual_pack_is_sent_unchanged(visual_turn, monkeypatch):
    chat, run = visual_turn
    monkeypatch.setattr(vision, "layout_reference_ids_from_message", lambda *args: ["layout-a", "layout-b"])
    monkeypatch.setattr(
        vision, "collect_vision_attachments",
        lambda **kwargs: {
            "images_b64": ["first-image", "second-image"],
            "captions": ["Image 1: Layout layout-a", "Image 2: Layout layout-b"],
            "note": "Attached both requested Layouts.",
        },
    )
    result = await run()
    assert result.reply == "Reviewed the supplied images."
    assert chat.call_args.kwargs["images"] == ["first-image", "second-image"]
    assert chat.call_args.kwargs["require_vision"] is True
