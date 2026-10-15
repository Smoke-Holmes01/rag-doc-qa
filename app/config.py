"""Project-root configuration, with explicit mode separation."""
import hashlib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import AppError

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="forbid")
    model_mode: Literal["mock", "live"] = "mock"
    docs_dir: Path = Path("data/docs")
    index_dir: Path = Path("storage/indexes")
    chat_adapter: str = ""
    embedding_adapter: str = ""
    chat_base_url: str = ""
    embedding_base_url: str = ""
    chat_api_key: SecretStr = SecretStr("")
    embedding_api_key: SecretStr = SecretStr("")
    chat_model: str = ""
    embedding_model: str = ""
    http_timeout_seconds: float = Field(default=30, gt=0, le=300)
    embedding_batch_size: int = Field(default=16, gt=0, le=64)
    chunk_max_chars: int = Field(default=800, ge=64, le=16000)
    chunk_overlap_chars: int = Field(default=100, ge=0)
    retrieval_top_k: int = Field(default=4, gt=0)
    retrieval_max_k: int = Field(default=8, gt=0, le=100)
    context_max_chars: int = Field(default=4000, gt=0, le=100000)
    min_similarity: float = Field(default=0, ge=-1, le=1)
    question_max_chars: int = Field(default=1000, gt=0, le=16000)
    chat_max_output_tokens: int = Field(default=1024, gt=0, le=16000)

    @model_validator(mode="after")
    def check_ranges(self) -> "Settings":
        if self.chunk_overlap_chars >= self.chunk_max_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be less than CHUNK_MAX_CHARS")
        if self.retrieval_top_k > self.retrieval_max_k:
            raise ValueError("RETRIEVAL_TOP_K must not exceed RETRIEVAL_MAX_K")
        self.docs_dir = (ROOT / self.docs_dir).resolve()
        self.index_dir = (ROOT / self.index_dir).resolve()
        return self

    def check_live(self) -> None:
        if self.model_mode == "mock":
            return
        names = ("chat_adapter", "embedding_adapter", "chat_base_url", "embedding_base_url",
                 "chat_api_key", "embedding_api_key", "chat_model", "embedding_model")
        invalid = []
        for name in names:
            value = getattr(self, name)
            value = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not value or not value.strip():
                invalid.append(name.upper())
        for name in ("chat_adapter", "embedding_adapter"):
            if getattr(self, name) != "siliconflow" and name.upper() not in invalid:
                invalid.append(name.upper())
        for name in ("chat_base_url", "embedding_base_url"):
            try:
                url = urlsplit(getattr(self, name))
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.query or url.fragment or not url.path.rstrip("/").endswith("/v1")):
                    raise ValueError
                _ = url.port
            except ValueError:
                if name.upper() not in invalid:
                    invalid.append(name.upper())
        if invalid:
            raise AppError("CONFIG_INVALID", "缺失或不合法的配置变量: " + ", ".join(invalid))

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.http_timeout_seconds, connect=min(5, self.http_timeout_seconds))

    def embedding_identity(self) -> dict[str, str]:
        if self.model_mode == "mock":
            return {"adapter": "mock", "model": "hash-bigram-v1", "service_id": "offline-v1"}
        address = self.embedding_base_url.rstrip("/")
        return {"adapter": self.embedding_adapter, "model": self.embedding_model,
                "service_id": hashlib.sha256(address.encode()).hexdigest()}

    def chunk_config(self) -> dict[str, str | int]:
        return {"strategy": "headings-paragraphs-v1", "max_chars": self.chunk_max_chars,
                "overlap_chars": self.chunk_overlap_chars}


def load_settings(**overrides: object) -> Settings:
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        # Do not serialize ValidationError: it includes input values (possibly secrets).
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]
                        and str(error["loc"][0]) in Settings.model_fields})
        suffix = ", ".join(names) if names else "CHUNK_OVERLAP_CHARS / CHUNK_MAX_CHARS / RETRIEVAL_TOP_K / RETRIEVAL_MAX_K"
        raise AppError("CONFIG_INVALID", "配置类型或范围无效，请检查: " + suffix) from None
    except ValueError:
        raise AppError("CONFIG_INVALID", "配置解析失败，请检查 .env.example 中的约束") from None
