"""Metadata evidence tracks executable dependencies without file-upload churn."""

from copy import deepcopy

import pytest

from app.core.comfy.client import workflow_metadata_sha256


GRAPH = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "reference.png"}},
    "2": {"class_type": "LoadAudio", "inputs": {"audio": "reference.wav"}},
    "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "h3.safetensors"}},
}
METADATA = {
    "LoadImage": {"input": {"required": {"image": [["old.png"], {"image_upload": True}]}}, "output": ["IMAGE", "MASK"], "output_node": False},
    "LoadAudio": {"input": {"required": {"audio": [["old.wav"], {"audio_upload": True}]}}, "output": ["AUDIO"], "output_node": False},
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["h3.safetensors"], {}]}}, "output": ["MODEL", "CLIP", "VAE"], "output_node": False},
}


@pytest.mark.parametrize("class_name,field", [("LoadImage", "image"), ("LoadAudio", "audio")])
def test_uploaded_filename_catalog_changes_do_not_invalidate_worker_proof(class_name, field):
    changed = deepcopy(METADATA)
    changed[class_name]["input"]["required"][field][0].append("new-reference-file")
    before = deepcopy(METADATA)
    assert workflow_metadata_sha256(GRAPH, METADATA) == workflow_metadata_sha256(GRAPH, changed)
    assert METADATA == before


def test_model_catalog_change_invalidates_dependency_identity():
    changed = deepcopy(METADATA)
    changed["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0] = ["different-h3.safetensors"]
    assert workflow_metadata_sha256(GRAPH, METADATA) != workflow_metadata_sha256(GRAPH, changed)


def test_missing_class_or_changed_output_contract_invalidates_dependency_identity():
    absent = deepcopy(METADATA)
    del absent["LoadAudio"]
    changed = deepcopy(METADATA)
    changed["LoadAudio"]["output"] = ["STRING"]
    identity = workflow_metadata_sha256(GRAPH, METADATA)
    assert identity != workflow_metadata_sha256(GRAPH, absent)
    assert identity != workflow_metadata_sha256(GRAPH, changed)


def test_unrelated_class_or_display_name_change_does_not_invalidate_identity():
    changed = deepcopy(METADATA)
    changed["UnusedClass"] = {"input": {}, "output": ["IMAGE"]}
    changed["LoadImage"]["display_name"] = "Translated image loader"
    assert workflow_metadata_sha256(GRAPH, METADATA) == workflow_metadata_sha256(GRAPH, changed)


def test_metadata_identity_is_independent_of_graph_and_dictionary_order():
    reversed_graph = dict(reversed(list(GRAPH.items())))
    reversed_metadata = dict(reversed(list(METADATA.items())))
    assert workflow_metadata_sha256(GRAPH, METADATA) == workflow_metadata_sha256(reversed_graph, reversed_metadata)
