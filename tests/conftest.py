"""All tests isolate configuration and forbid real HTTP/socket connections."""
import asyncio
import ipaddress
import shutil
import socket

import httpx
import pytest

from app.config import ROOT, Settings
from app.index import build_index
from app.providers.mock import MockEmbedding


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)

    def denied(*args, **kwargs):
        raise AssertionError("offline tests must not make real network calls")

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def connect(sock, address):
            # Windows asyncio implements socketpair using a loopback connection.
            # HTTP transports stay forbidden even for loopback/proxy addresses.
            if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
                return original(sock, address)
            raise AssertionError("offline tests forbid external socket connections")
        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(original_connect_ex))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.fixture
def settings(tmp_path):
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "data/docs", docs)
    return Settings(_env_file=None, docs_dir=docs, index_dir=tmp_path / "indexes")


@pytest.fixture
def built(settings):
    return asyncio.run(build_index(settings, MockEmbedding()))


@pytest.fixture
def live_settings(settings):
    return settings.model_copy(update={
        "model_mode": "live", "chat_adapter": "siliconflow", "embedding_adapter": "siliconflow",
        "chat_base_url": "https://chat.example.invalid/v1", "embedding_base_url": "https://embed.example.invalid/v1",
        "chat_api_key": __import__("pydantic").SecretStr("test-only-secret"),
        "embedding_api_key": __import__("pydantic").SecretStr("test-only-secret"),
        "chat_model": "test-chat", "embedding_model": "test-embedding"})
