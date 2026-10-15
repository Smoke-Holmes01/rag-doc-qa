"""Read only the configured, flat UTF-8 corpus directory."""
import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from app.errors import AppError


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Document:
    filename: str
    text: str
    source_hash: str


def read_documents(root: Path) -> list[Document]:
    root = root.resolve()
    if not root.is_dir():
        raise AppError("CORPUS_INVALID", "语料目录不存在或不是目录")
    docs = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name.startswith((".", "~")) or name.endswith(("~", ".tmp", ".bak")):
            continue
        if entry.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            if getattr(entry.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2):
                continue
            resolved = entry.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError
            raw = resolved.read_bytes()
            text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
        except (OSError, UnicodeError, ValueError):
            raise AppError("CORPUS_INVALID", f"语料文件不可读取、非 UTF-8 或路径越界: {name}") from None
        if not text:
            raise AppError("CORPUS_INVALID", f"语料文件为空: {name}")
        docs.append(Document(name, text, sha256(raw)))
    if not docs:
        raise AppError("CORPUS_INVALID", "语料为空：需要非隐藏的 Markdown/TXT 文件")
    return docs
