"""Heading/paragraph splitting; overlap is confined to oversized paragraphs."""
import json
import re
from pathlib import Path

from app.documents import Document, sha256
from app.errors import AppError
from app.schemas import Chunk


def chunk_id(filename: str, heading_path: list[str], ordinal: int, text: str) -> str:
    raw = json.dumps([filename, heading_path, ordinal, text], ensure_ascii=False, separators=(",", ":"))
    return "c_" + sha256(raw.encode())[:24]


def split_document(doc: Document, max_chars: int, overlap: int) -> list[Chunk]:
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("invalid chunk limits")
    title = Path(doc.filename).stem
    sections: list[tuple[list[str], str]] = []
    headings: list[tuple[int, str]] = []
    buffer: list[str] = []
    fence: str | None = None

    def flush_section() -> None:
        text = "\n".join(buffer).strip()
        if text:
            sections.append(([name for _, name in headings], text))
        buffer.clear()

    is_markdown = Path(doc.filename).suffix.lower() == ".md"
    found_title = False
    for line in doc.text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line) if is_markdown else None
        if marker:
            char = marker.group(1)[0]
            if fence is None:
                fence = char
            elif fence == char:
                fence = None
        match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if is_markdown and fence is None else None
        if match:
            flush_section()
            level, name = len(match.group(1)), match.group(2)
            if level == 1 and not found_title:
                title, found_title = name, True
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, name))
        else:
            buffer.append(line)
    flush_section()
    chunks: list[Chunk] = []

    def append(path: list[str], text: str) -> None:
        text = text.strip()
        if text:
            ordinal = len(chunks)
            chunks.append(Chunk(chunk_id=chunk_id(doc.filename, path, ordinal, text), filename=doc.filename,
                                title=title, heading_path=path, ordinal=ordinal, text=text,
                                content_hash=sha256(text.encode())))

    for path, text in sections:
        pending = ""
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) > max_chars:
                append(path, pending)
                pending = ""
                start = 0
                while start < len(paragraph):
                    append(path, paragraph[start:start + max_chars])
                    if start + max_chars >= len(paragraph):
                        break
                    start += max_chars - overlap
            elif pending and len(pending) + 2 + len(paragraph) > max_chars:
                append(path, pending)
                pending = paragraph
            else:
                pending = pending + "\n\n" + paragraph if pending else paragraph
        append(path, pending)
    if not chunks:
        raise AppError("CORPUS_INVALID", f"文件没有可用正文片段: {doc.filename}")
    return chunks


def split_documents(docs: list[Document], max_chars: int, overlap: int) -> list[Chunk]:
    return [chunk for doc in docs for chunk in split_document(doc, max_chars, overlap)]


def embedding_text(chunk: Chunk) -> str:
    return f"{chunk.title}\n{' / '.join(chunk.heading_path)}\n{chunk.text}"
