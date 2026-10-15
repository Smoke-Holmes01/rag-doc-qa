import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings
from app.errors import AppError
from app.index import build_index
from app.providers.http_adapters import SiliconFlowChat, SiliconFlowEmbedding
from app.providers.mock import MockEmbedding
from scripts.evaluate import load_cases, run
from app.config import ROOT


def test_settings_bounds_and_secret_redaction():
    for options in [{"chunk_max_chars": 100, "chunk_overlap_chars": 100}, {"retrieval_top_k": 9},
                    {"min_similarity": 1.1}, {"http_timeout_seconds": 0}]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **options)
    with pytest.raises(AppError) as error:
        load_settings(_env_file=None, http_timeout_seconds="do-not-leak")
    assert "do-not-leak" not in error.value.message
    settings = Settings(_env_file=None)
    assert settings.timeout().connect == 5 and settings.timeout().read == 30


@pytest.mark.parametrize("url", ["http://example.invalid/v1", "https://user:secret@example.invalid/v1",
                                "https://example.invalid/v1?key=secret", "https://example.invalid/api"])
def test_live_url_validation(live_settings, url):
    live_settings.embedding_base_url = url
    with pytest.raises(AppError) as error:
        live_settings.check_live()
    assert "EMBEDDING_BASE_URL" in error.value.message
    assert "secret" not in error.value.message


def test_embedding_mapping_and_exact_payload(live_settings):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert str(request.url) == "https://embed.example.invalid/v1/embeddings"
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await SiliconFlowEmbedding(client, live_settings).embed(["a", "b"])
            assert values == [[1, 0], [0, 1]]
        assert client.is_closed
    asyncio.run(check())
    assert seen == [{"model": "test-embedding", "input": ["a", "b"], "encoding_format": "float"}]


@pytest.mark.parametrize("data", [{}, {"data": []}, {"data": [{"index": 2, "embedding": [1]}]},
    {"data": [{"index": True, "embedding": [1]}]}, {"data": [{"index": 0, "embedding": ["1"]}]},
    {"data": [{"index": 0, "embedding": []}]}, {"data": ["bad"]}, {"data": [None]}])
def test_embedding_bad_response(live_settings, data):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
            with pytest.raises(AppError) as error:
                await SiliconFlowEmbedding(client, live_settings).embed(["a"])
            assert error.value.code == "MODEL_RESPONSE_INVALID"
    asyncio.run(check())


def test_duplicate_embedding_index(live_settings):
    async def check():
        data = {"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
            with pytest.raises(AppError):
                await SiliconFlowEmbedding(client, live_settings).embed(["a", "b"])
    asyncio.run(check())


@pytest.mark.parametrize("status,expected,http_status", [(401, "MODEL_UPSTREAM_ERROR", 502),
    (400, "MODEL_UPSTREAM_ERROR", 502), (500, "MODEL_UPSTREAM_ERROR", 502),
    (429, "MODEL_RATE_LIMITED", 503), (504, "MODEL_TIMEOUT", 504)])
def test_upstream_errors_no_leak_or_retry(live_settings, status, expected, http_status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret auth body C:/private/secret")
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AppError) as error:
                await SiliconFlowEmbedding(client, live_settings).embed(["a"])
            assert error.value.code == expected and error.value.status == http_status
            assert "secret" not in error.value.message
        assert len(calls) == 1
    asyncio.run(check())


@pytest.mark.parametrize("exception,expected", [(httpx.ReadTimeout, "MODEL_TIMEOUT"),
    (httpx.ConnectTimeout, "MODEL_TIMEOUT"), (httpx.ConnectError, "MODEL_UPSTREAM_ERROR")])
def test_transport_errors(live_settings, exception, expected):
    def handler(request):
        raise exception("must-not-leak", request=request)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AppError) as error:
                await SiliconFlowEmbedding(client, live_settings).embed(["a"])
            assert error.value.code == expected
            assert "must-not-leak" not in error.value.message
    asyncio.run(check())


@pytest.mark.parametrize("response", [{"choices": []}, {"choices": [None]},
    {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}]},
    {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}])
def test_bad_chat_fields(live_settings, response):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))) as client:
            with pytest.raises(AppError):
                await SiliconFlowChat(client, live_settings).complete([])
    asyncio.run(check())


def test_chat_payload(live_settings):
    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://chat.example.invalid/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == 1024 and not body["stream"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await SiliconFlowChat(client, live_settings).complete([]) == "{}"
    asyncio.run(check())


def test_invalid_http_json(live_settings):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="broken"))) as client:
            with pytest.raises(AppError):
                await SiliconFlowChat(client, live_settings).complete([])
    asyncio.run(check())


def test_eval_cases_and_mock_reports(settings, built, monkeypatch, tmp_path):
    cases = load_cases(ROOT / "eval/cases.jsonl")
    assert len(cases) == 15
    # Explicit CLI mode must override the ambient live setting.
    monkeypatch.setenv("MODEL_MODE", "live")
    monkeypatch.setenv("DOCS_DIR", str(settings.docs_dir))
    monkeypatch.setenv("INDEX_DIR", str(settings.index_dir))
    directory = tmp_path / "report"
    assert asyncio.run(run("mock", directory)) == 0
    result = json.loads((directory / "evaluation.json").read_bytes())
    assert result["mode"] == "mock" and len(result["cases"]) == 15
    assert result["retrieval"]["denominator"] == 12
    assert all(case["semantic_status"] == "pending_review" for case in result["cases"])
    assert (directory / "review.csv").exists()
    assert "准确率" not in json.dumps(result.get("retrieval"), ensure_ascii=False)


def test_eval_live_missing_no_fallback(tmp_path):
    assert asyncio.run(run("live", tmp_path)) == 1
    report = json.loads((tmp_path / "evaluation.json").read_bytes())
    assert report["mode"] == "live" and report["cases"] == []
    assert report["execution_errors"][0]["code"] == "CONFIG_INVALID"
