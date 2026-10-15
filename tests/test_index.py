import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from app.documents import sha256
from app.errors import AppError
from app.index import build_index, json_bytes, load_index
from app.providers.mock import MockEmbedding


def current_path(settings):
    mode = settings.index_dir / settings.model_mode
    pointer = json.loads((mode / "current.json").read_bytes())
    return mode / "builds" / pointer["build_id"]


def assert_rebuild(settings):
    with pytest.raises(AppError) as error:
        load_index(settings)
    assert error.value.code == "INDEX_REBUILD_REQUIRED"


def test_round_trip(settings, built):
    loaded = load_index(settings)
    np.testing.assert_array_equal(loaded.vectors, built.vectors)
    assert loaded.chunks == built.chunks
    assert loaded.manifest.chunk_count == len(loaded.chunks)
    assert loaded.vectors.flags.writeable is False


def test_missing(settings):
    with pytest.raises(AppError) as error:
        load_index(settings)
    assert error.value.code == "INDEX_NOT_READY"


@pytest.mark.parametrize("change", ["add", "remove", "modify", "empty", "decode"])
def test_corpus_changes(settings, built, change):
    doc = settings.docs_dir / "01_overview.md"
    if change == "add":
        (settings.docs_dir / "extra.txt").write_text("新增正文", encoding="utf-8")
    elif change == "remove":
        doc.unlink()
    elif change == "decode":
        doc.write_bytes(b"\xff")
    else:
        doc.write_text("" if change == "empty" else "改动", encoding="utf-8")
    assert_rebuild(settings)


@pytest.mark.parametrize("filename", ["chunks.json", "vectors.npz", "manifest.json"])
def test_artifact_damage(settings, built, filename):
    (current_path(settings) / filename).write_bytes(b"damaged")
    assert_rebuild(settings)


def test_pointer_traversal(settings, built):
    pointer = settings.index_dir / "mock/current.json"
    pointer.write_text('{"build_id":"../../private","manifest_hash":"x"}')
    assert_rebuild(settings)


def rewrite_manifest(settings, mutate):
    path = current_path(settings)
    manifest = json.loads((path / "manifest.json").read_bytes())
    mutate(manifest, path)
    raw = json_bytes(manifest)
    (path / "manifest.json").write_bytes(raw)
    pointer_path = settings.index_dir / settings.model_mode / "current.json"
    pointer = json.loads(pointer_path.read_bytes())
    pointer["manifest_hash"] = sha256(raw)
    pointer_path.write_bytes(json_bytes(pointer))


@pytest.mark.parametrize("field,value", [("mode", "live"), ("dimension", 17), ("chunk_count", 1),
                                      ("format_version", 9), ("normalization", "none")])
def test_manifest_mismatch(settings, built, field, value):
    rewrite_manifest(settings, lambda manifest, path: manifest.update({field: value}))
    assert_rebuild(settings)


def test_rows_mapping_and_zero_even_with_updated_hash(settings, built):
    def corrupt(manifest, path):
        np.savez_compressed(path / "vectors.npz", vectors=np.zeros((1, 384), dtype=np.float32))
        manifest["artifacts"]["vectors.npz"] = sha256((path / "vectors.npz").read_bytes())
    rewrite_manifest(settings, corrupt)
    assert_rebuild(settings)


def test_chunk_mapping_even_with_updated_hash(settings, built):
    def corrupt(manifest, path):
        chunks = json.loads((path / "chunks.json").read_bytes())
        chunks.reverse()
        (path / "chunks.json").write_bytes(json_bytes(chunks))
        manifest["artifacts"]["chunks.json"] = sha256((path / "chunks.json").read_bytes())
    rewrite_manifest(settings, corrupt)
    assert_rebuild(settings)


def test_config_changes(settings, built):
    assert_rebuild(settings.model_copy(update={"chunk_max_chars": 700}))
    assert_rebuild(settings.model_copy(update={"chunk_overlap_chars": 50}))
    assert load_index(settings.model_copy(update={"chat_model": "different-chat"})).fingerprint == built.fingerprint


def test_live_embedding_identity(live_settings):
    asyncio.run(build_index(live_settings, MockEmbedding()))
    for field, value in [("embedding_model", "different"), ("embedding_base_url", "https://other.invalid/v1")]:
        assert_rebuild(live_settings.model_copy(update={field: value}))
    assert load_index(live_settings.model_copy(update={"chat_model": "different"}))


def test_publish_failure_keeps_previous(settings, built, monkeypatch):
    pointer = settings.index_dir / "mock/current.json"
    previous = pointer.read_bytes()

    def fail(*args):
        raise OSError("controlled publication failure")

    monkeypatch.setattr("app.index.os.replace", fail)
    with pytest.raises(OSError):
        asyncio.run(build_index(settings, MockEmbedding()))
    assert pointer.read_bytes() == previous
    assert load_index(settings).fingerprint == built.fingerprint


def test_provider_and_selfcheck_failure_keep_previous(settings, built):
    pointer = settings.index_dir / "mock/current.json"
    previous = pointer.read_bytes()

    class BadEmbedding:
        async def embed(self, texts):
            return [[0.0]] * len(texts)

    with pytest.raises(AppError):
        asyncio.run(build_index(settings, BadEmbedding()))
    assert pointer.read_bytes() == previous

    class ChangesCorpus(MockEmbedding):
        async def embed(self, texts):
            (settings.docs_dir / "new.txt").write_text("new content")
            return await super().embed(texts)

    with pytest.raises(AppError, match="自检失败"):
        asyncio.run(build_index(settings, ChangesCorpus()))
    assert pointer.read_bytes() == previous
