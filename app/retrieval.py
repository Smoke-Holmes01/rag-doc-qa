"""Exact cosine ranking, stable ties, and whole-chunk context budgeting."""
import json
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from app.errors import AppError
from app.schemas import Chunk

CONTEXT_PREFIX = "BEGIN_UNTRUSTED_SOURCES\n"
CONTEXT_SUFFIX = "END_UNTRUSTED_SOURCES"
CONTEXT_WRAPPER_CHARS = len(CONTEXT_PREFIX) + len(CONTEXT_SUFFIX)


def normalize(values: object, expected_rows: int | None = None,
              expected_dim: int | None = None) -> NDArray[np.float32]:
    try:
        raw = np.asarray(values)
        if raw.dtype.kind not in "fiu":
            raise ValueError
        matrix = raw.astype(np.float32)
        if (matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]
                or (expected_rows is not None and matrix.shape[0] != expected_rows)
                or (expected_dim is not None and matrix.shape[1] != expected_dim)
                or not np.isfinite(matrix).all()):
            raise ValueError
        # float64 norms avoid overflow from finite float32 inputs.
        norms = np.linalg.norm(matrix.astype(np.float64), axis=1, keepdims=True)
        if not np.isfinite(norms).all() or np.any(norms <= 0):
            raise ValueError
        return (matrix / norms).astype(np.float32)
    except (ValueError, TypeError, OverflowError):
        raise AppError("MODEL_RESPONSE_INVALID", "嵌入数量、维度或数值无效", 502) from None


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float

    def block(self) -> str:
        # JSON escapes embedded delimiters; the system rule remains a separate message.
        return json.dumps({"source_id": self.chunk.chunk_id, "filename": self.chunk.filename,
                           "heading_path": self.chunk.heading_path, "text": self.chunk.text},
                          ensure_ascii=False, separators=(",", ":")) + "\n"


def select_context(matrix: NDArray[np.float32], query: NDArray[np.float32], chunks: list[Chunk],
                   top_k: int, budget: int, minimum: float) -> list[Hit]:
    scores = matrix @ query[0]
    order = np.argsort(-scores, kind="stable")
    candidates: list[Hit] = []
    seen: set[str] = set()
    for index in order:
        chunk = chunks[int(index)]
        score = float(scores[index])
        if score < minimum or not chunk.text.strip() or chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        candidates.append(Hit(chunk, score))
        if len(candidates) == top_k:
            break
    chosen: list[Hit] = []
    used = CONTEXT_WRAPPER_CHARS
    for hit in candidates:
        size = len(hit.block())
        if used + size <= budget:
            chosen.append(hit)
            used += size
    return chosen
