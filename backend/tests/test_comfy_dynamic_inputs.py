"""Read-only validation of Comfy V3 schemas used by the remote H3 graph.

Fixtures retain the public /object_info shapes from ComfyUI v0.30.2:
ComfyMathExpression's named Autogrow and SaveVideo's nested DynamicCombo.
No ComfyUI server, local install, or cached metadata is needed by these tests.
"""
from __future__ import annotations

import httpx
import pytest
from app.core.comfy.client import ComfyClient, ComfyError


@pytest.fixture
def client():
    def no_network(request):
        pytest.fail(f"Preloaded schema validation must not send {request.method} {request.url}")

    return ComfyClient(
        "http://worker.test:8188",
        worker_id="render-test",
        transport=httpx.MockTransport(no_network),
    )


def autogrow(*, names=None, prefix=None, minimum=1, optional_template=False):
    template = {
        "input": {
            "optional" if optional_template else "required": {
                "value": ["FLOAT,INT,BOOLEAN", {}],
            },
        },
        "min": minimum,
    }
    if names is not None:
        template["names"] = names
    else:
        template.update(prefix=prefix, max=3)
    return ["COMFY_AUTOGROW_V3", {"template": template}]


async def validate(client, inputs, required, *, optional=None, class_name="ComfyMathExpression"):
    return await client.validate_workflow(
        {
            "1": {"class_type": class_name, "inputs": inputs},
            "2": {"class_type": "PrimitiveInt", "inputs": {"value": 4}},
        },
        object_info={
            class_name: {"input": {"required": required, "optional": optional or {}}},
            "PrimitiveInt": {"input": {"required": {"value": ["INT", {}]}}, "output": ["INT"]},
        },
    )


@pytest.mark.asyncio
async def test_named_autogrow_accepts_flattened_math_links_without_parent_input(client):
    result = await validate(
        client,
        {"expression": "a + b", "values.a": ["2", 0], "values.b": ["2", 0]},
        {"expression": ["STRING", {}], "values": autogrow(names=["a", "b", "c"])},
    )
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_named_autogrow_requires_first_minimum_name_not_any_name(client):
    with pytest.raises(ComfyError, match=r"values\.a"):
        await validate(
            client,
            {"expression": "b", "values.b": ["2", 0]},
            {"expression": ["STRING", {}], "values": autogrow(names=["a", "b", "c"])},
        )


@pytest.mark.asyncio
async def test_zero_minimum_prefix_autogrow_allows_no_parent_or_children(client):
    result = await validate(client, {}, {"references": autogrow(prefix="image", minimum=0)})
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_prefix_autogrow_requires_each_minimum_child(client):
    with pytest.raises(ComfyError, match=r"references\.image0"):
        await validate(
            client,
            {"references.image1": ["2", 0]},
            {"references": autogrow(prefix="image", minimum=2)},
        )


@pytest.mark.asyncio
async def test_optional_autogrow_template_does_not_require_minimum_children(client):
    result = await validate(
        client, {}, {"references": autogrow(prefix="image", minimum=2, optional_template=True)},
    )
    assert result["valid"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("format_value", ["mp4", "webm"])
async def test_v3_combo_accepts_options_in_spec_metadata(client, format_value):
    result = await validate(
        client, {"format": format_value}, {"format": ["COMBO", {"options": ["mp4", "webm"]}]},
    )
    assert result["valid"] is True


@pytest.mark.asyncio
async def test_v3_combo_rejects_value_absent_from_worker_options(client):
    with pytest.raises(ComfyError, match="format.*avi"):
        await validate(
            client, {"format": "avi"}, {"format": ["COMBO", {"options": ["mp4", "webm"]}]},
        )


def video_codec():
    return [
        "COMFY_DYNAMICCOMBO_V3",
        {"options": [
            {"key": "auto", "inputs": {"required": {}}},
            {"key": "h264", "inputs": {
                "required": {},
                "optional": {"encoding": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {"options": [
                        {"key": "auto", "inputs": {"required": {}}},
                        {"key": "re-encode", "inputs": {"required": {"crf": ["FLOAT", {"default": 23.0}]}}},
                    ]},
                ]},
            }},
        ]},
    ]


@pytest.mark.asyncio
async def test_nested_selected_dynamic_combo_requires_flattened_child_despite_default(client):
    with pytest.raises(ComfyError, match=r"codec\.encoding\.crf"):
        await validate(
            client,
            {"codec": "h264", "codec.encoding": "re-encode"},
            {"codec": video_codec()},
            class_name="SaveVideo",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("codec_inputs", [
    {"codec": "auto"},
    {"codec": "h264"},
    {"codec": "h264", "codec.encoding": "auto"},
    {"codec": "h264", "codec.encoding": "re-encode", "codec.encoding.crf": 23.0},
])
async def test_dynamic_combo_requires_only_inputs_of_selected_options(client, codec_inputs):
    result = await validate(client, codec_inputs, {"codec": video_codec()}, class_name="SaveVideo")
    assert result["valid"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("codec_inputs", "error"), [
    ({"codec": "av1"}, "codec.*av1"),
    ({"codec": "h264", "codec.encoding": "lossless"}, r"codec\.encoding.*lossless"),
])
async def test_dynamic_combo_rejects_unknown_outer_or_nested_option(client, codec_inputs, error):
    with pytest.raises(ComfyError, match=error):
        await validate(client, codec_inputs, {"codec": video_codec()}, class_name="SaveVideo")
