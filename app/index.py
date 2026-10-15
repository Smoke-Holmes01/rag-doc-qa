"""Immutable builds with an atomically published, mode-local pointer."""
import json
import io
import os
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from pydantic import Field

from app.chunking import embedding_text, split_documents
from app.config import Settings
from app.documents import Document, read_documents, sha256
from app.errors import AppError, rebuild
from app.providers.base import EmbeddingProvider
from app.retrieval import normalize
from app.schemas import Chunk, StrictModel


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode()


def corpus_info(docs: list[Document]) -> tuple[list[dict[str, str]], str]:
    entries = [{"filename": doc.filename, "sha256": doc.source_hash} for doc in docs]
    return entries, sha256(json_bytes(entries))


class Manifest(StrictModel):
    format_version: int
    mode: str
    documents: list[dict[str, str]]
    corpus_fingerprint: str
    chunk_count: int = Field(gt=0)
    embedding: dict[str, str]
    dimension: int = Field(gt=0)
    dtype: str
    normalization: str
    chunking: dict[str, str | int]
    artifacts: dict[str, str]


class Pointer(StrictModel):
    build_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Index:
    vectors: NDArray[np.float32]
    chunks: list[Chunk]
    manifest: Manifest
    fingerprint: str
    build_id: str


def _load_build(settings: Settings, path: Path, pointer: Pointer) -> Index:
    raw = (path / "manifest.json").read_bytes()
    if sha256(raw) != pointer.manifest_hash:
        raise ValueError("manifest hash")
    manifest = Manifest.model_validate_json(raw)
    if (manifest.format_version != 1 or manifest.mode != settings.model_mode
            or manifest.embedding != settings.embedding_identity()
            or manifest.chunking != settings.chunk_config()
            or manifest.dtype != "float32" or manifest.normalization != "l2"
            or set(manifest.artifacts) != {"vectors.npz", "chunks.json"}):
        raise ValueError("incompatible manifest")
    artifacts = {}
    for name, digest in manifest.artifacts.items():
        artifact = path / name
        if artifact.is_symlink() or not artifact.resolve().is_relative_to(path.resolve()):
            raise ValueError("artifact path")
        artifacts[name] = artifact.read_bytes()
        if sha256(artifacts[name]) != digest:
            raise ValueError("artifact hash")
    raw_chunks = json.loads(artifacts["chunks.json"])
    chunks = [Chunk.model_validate(item) for item in raw_chunks]
    docs = read_documents(settings.docs_dir)
    entries, fingerprint = corpus_info(docs)
    if entries != manifest.documents or fingerprint != manifest.corpus_fingerprint:
        raise ValueError("corpus changed")
    expected_chunks = split_documents(docs, settings.chunk_max_chars, settings.chunk_overlap_chars)
    if chunks != expected_chunks or len({c.chunk_id for c in chunks}) != len(chunks):
        raise ValueError("chunk mapping")
    with np.load(io.BytesIO(artifacts["vectors.npz"]), allow_pickle=False) as data:
        if data.files != ["vectors"]:
            raise ValueError("matrix keys")
        vectors = data["vectors"]
    if vectors.dtype != np.float32:
        raise ValueError("matrix dtype")
    normalized = normalize(vectors, manifest.chunk_count, manifest.dimension)
    if len(chunks) != manifest.chunk_count or not np.allclose(vectors, normalized, atol=1e-6):
        raise ValueError("matrix rows or norms")
    vectors.setflags(write=False)
    return Index(vectors, chunks, manifest, pointer.manifest_hash, pointer.build_id)


def load_index(settings: Settings) -> Index:
    settings.check_live()
    mode_dir = settings.index_dir / settings.model_mode
    pointer_path = mode_dir / "current.json"
    if not pointer_path.exists():
        raise AppError("INDEX_NOT_READY", "索引缺失，请先运行离线构建命令")
    try:
        pointer = Pointer.model_validate_json(pointer_path.read_bytes())
        path = mode_dir / "builds" / pointer.build_id
        if path.is_symlink() or not path.resolve().is_relative_to(mode_dir.resolve()):
            raise ValueError("build path")
        return _load_build(settings, path, pointer)
    except (OSError, ValueError, TypeError, KeyError, EOFError, zipfile.BadZipFile, AppError):
        raise rebuild() from None


async def build_index(settings: Settings, provider: EmbeddingProvider) -> Index:
    settings.check_live()
    docs = read_documents(settings.docs_dir)
    chunks = split_documents(docs, settings.chunk_max_chars, settings.chunk_overlap_chars)
    matrices = []
    dimension: int | None = None
    for start in range(0, len(chunks), settings.embedding_batch_size):
        batch = chunks[start:start + settings.embedding_batch_size]
        values = await provider.embed([embedding_text(chunk) for chunk in batch])
        matrix = normalize(values, len(batch), dimension)
        dimension = matrix.shape[1]
        matrices.append(matrix)
    vectors = np.concatenate(matrices)
    mode_dir = settings.index_dir / settings.model_mode
    build_id = uuid.uuid4().hex
    build_path = mode_dir / "builds" / build_id
    build_path.mkdir(parents=True)
    # An unpublished build directory is safe to leave for inspection if writing fails.
    np.savez_compressed(build_path / "vectors.npz", vectors=vectors)
    (build_path / "chunks.json").write_bytes(json_bytes([c.model_dump() for c in chunks]))
    entries, fingerprint = corpus_info(docs)
    manifest = Manifest(format_version=1, mode=settings.model_mode, documents=entries,
                        corpus_fingerprint=fingerprint, chunk_count=len(chunks),
                        embedding=settings.embedding_identity(), dimension=int(vectors.shape[1]),
                        dtype="float32", normalization="l2", chunking=settings.chunk_config(),
                        artifacts={name: sha256((build_path / name).read_bytes())
                                   for name in ("vectors.npz", "chunks.json")})
    raw = json_bytes(manifest.model_dump())
    (build_path / "manifest.json").write_bytes(raw)
    pointer = Pointer(build_id=build_id, manifest_hash=sha256(raw))
    try:
        result = _load_build(settings, build_path, pointer)
    except (OSError, ValueError, TypeError, KeyError, EOFError, zipfile.BadZipFile, AppError):
        raise AppError("INDEX_BUILD_FAILED", "新索引自检失败，旧索引未被替换") from None
    temporary = mode_dir / f".current-{build_id}.tmp"
    temporary.write_bytes(json_bytes(pointer.model_dump()))
    os.replace(temporary, mode_dir / "current.json")
    return result
