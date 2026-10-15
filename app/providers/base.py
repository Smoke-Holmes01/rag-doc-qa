from typing import Protocol


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class ChatProvider(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str: ...
