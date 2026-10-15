import numpy as np
import pytest

from app.chunking import split_document
from app.documents import Document, read_documents, sha256
from app.errors import AppError
from app.retrieval import CONTEXT_WRAPPER_CHARS, Hit, normalize, select_context
from app.schemas import Chunk


def chunk(number, text="正文"):
    return Chunk(chunk_id=f"c{number}", filename="test.md", title="标题", heading_path=["章节"],
                 ordinal=number, text=text, content_hash=sha256(text.encode()))


def test_corpus_empty_invalid_and_hidden(tmp_path):
    with pytest.raises(AppError, match="语料为空"):
        read_documents(tmp_path)
    (tmp_path / ".hidden.md").write_text("hidden")
    (tmp_path / "~temp.txt").write_text("temp")
    (tmp_path / "ignore.pdf").write_bytes(b"pdf")
    (tmp_path / "b.txt").write_bytes("中文\r\n\r\n正文".encode())
    (tmp_path / "a.md").write_text("# A\n\n42 ms", encoding="utf-8")
    result = read_documents(tmp_path)
    assert [doc.filename for doc in result] == ["a.md", "b.txt"]
    assert result[1].text == "中文\n\n正文"
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(AppError, match="bad.txt"):
        read_documents(tmp_path)
    (tmp_path / "bad.txt").write_text(" \n ")
    with pytest.raises(AppError, match="bad.txt"):
        read_documents(tmp_path)


def test_escape_rejected_even_without_os_symlink_privilege(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    target = root / "escape.md"
    target.write_text("not used")
    outside = tmp_path / "private.md"
    outside.write_text("private")
    cls = type(root)
    original = cls.resolve

    def resolved(path, *args, **kwargs):
        return outside if path == target else original(path, *args, **kwargs)

    monkeypatch.setattr(cls, "resolve", resolved)
    with pytest.raises(AppError, match="escape.md"):
        read_documents(root)


def test_headings_paragraphs_long_text_and_stability():
    text = "# 标题\n\n## 配置\n\n超时 30 秒。\n\n例外 5 秒。\n\n### 中文\n\n" + "长段数字123单位ms" * 20
    doc = Document("a.md", text, "unused")
    first = split_document(doc, 64, 10)
    assert first == split_document(doc, 64, 10)
    assert first[0].text == "超时 30 秒。\n\n例外 5 秒。"
    assert first[0].heading_path == ["标题", "配置"]
    assert all(len(item.text) <= 64 for item in first)
    assert first[1].text[-10:] == first[2].text[:10]
    assert all(item.heading_path[-1] == "中文" for item in first[1:])
    assert len({item.chunk_id for item in first}) == len(first)
    altered = split_document(Document("b.md", text, "unused"), 64, 10)
    assert altered[0].chunk_id != first[0].chunk_id


def test_txt_fences_and_empty_body():
    txt = split_document(Document("配置.txt", "# 这不是标题\n\n正文", ""), 64, 0)
    assert txt[0].title == "配置" and txt[0].heading_path == []
    fenced = split_document(Document("a.md", "# A\n\n```python\n## 代码\n```\n\n## B\n\n正文", ""), 100, 0)
    assert "## 代码" in fenced[0].text
    assert fenced[-1].heading_path == ["A", "B"]
    with pytest.raises(AppError):
        split_document(Document("empty.md", "# Only a heading", ""), 64, 0)


def test_utf8_bom_and_native_hidden_attribute(tmp_path, monkeypatch):
    from types import SimpleNamespace
    visible = tmp_path / "visible.md"
    visible.write_bytes(b"\xef\xbb\xbf" + "# 标题\n\n正文".encode())
    hidden = tmp_path / "hidden.md"
    hidden.write_text("hidden")
    path_type = type(hidden)
    original = path_type.lstat

    def attributes(path):
        return SimpleNamespace(st_file_attributes=2) if path == hidden else original(path)

    monkeypatch.setattr(path_type, "lstat", attributes)
    docs = read_documents(tmp_path)
    assert [doc.filename for doc in docs] == ["visible.md"]
    assert split_document(docs[0], 64, 0)[0].title == "标题"


@pytest.mark.parametrize("values,rows,dim", [([], None, None), ([[0, 0]], 1, 2),
    ([[float("nan"), 1]], 1, 2), ([[float("inf"), 1]], 1, 2),
    ([[1, 2], [1]], 2, None), ([[1, 2]], 2, None), ([[1, 2]], 1, 3), ([1, 2], 1, 2),
    ([["1", "2"]], 1, 2), ([[True, False]], 1, 2), ([[1j, 1]], 1, 2)])
def test_bad_vectors(values, rows, dim):
    with pytest.raises(AppError) as error:
        normalize(values, rows, dim)
    assert error.value.code == "MODEL_RESPONSE_INVALID"


def test_exact_ranking_ties_and_budget_boundaries():
    chunks = [chunk(0), chunk(1), chunk(2)]
    matrix = normalize([[1, 0], [1, 0], [0, 1]])
    query = normalize([[1, 0]])
    hits = select_context(matrix, query, chunks, 3, 10000, -1)
    assert [hit.chunk.chunk_id for hit in hits] == ["c0", "c1", "c2"]
    assert hits[0].score == 1 and hits[2].score == 0
    size = len(hits[0].block()) + CONTEXT_WRAPPER_CHARS
    assert len(select_context(matrix, query, chunks, 1, size, 0)) == 1
    assert select_context(matrix, query, chunks, 1, size - 1, 0) == []


def test_threshold_dedup_and_small_later_candidate():
    chunks = [chunk(0, "长" * 500), chunk(1), chunk(1), chunk(3, " ")]
    matrix = normalize([[1, 0]] * 4)
    query = normalize([[1, 0]])
    hits = select_context(matrix, query, chunks, 4, 200, 0)
    assert [hit.chunk.chunk_id for hit in hits] == ["c1"]
    assert select_context(matrix, normalize([[-1, 0]]), chunks, 4, 10000, 0) == []


def test_large_finite_norm_does_not_overflow():
    result = normalize([[3e38, 3e38]])
    assert np.isfinite(result).all()
    assert np.isclose(np.linalg.norm(result), 1)


def test_full_context_envelope_fits_the_budget():
    from app.prompts import messages
    items = [chunk(0), chunk(1)]
    matrix = normalize([[1, 0], [0, 1]])
    budget = len(Hit(items[0], 1).block()) + CONTEXT_WRAPPER_CHARS
    chosen = select_context(matrix, normalize([[1, 0]]), items, 2, budget, -1)
    assert len(messages("question", chosen)[2]["content"]) == budget
