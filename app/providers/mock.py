"""Deterministic process doubles, never a semantic quality benchmark."""
import hashlib
import json
import re


def bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text.lower())
    return {text[i:i + 2] for i in range(max(1, len(text) - 1))} if text else set()


class MockEmbedding:
    dimension = 384

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        result = []
        for text in texts:
            vector = [0.0] * self.dimension
            for gram in bigrams(text):
                digest = hashlib.sha256(gram.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
            result.append(vector)
        return result


class MockChat:
    """Extract a supplied passage based on literal overlap; no eval fixtures are read.

    This is deliberately not an LLM, and can give poor or unsupported answers.
    The overlap rule here is a chat test-double behavior, not a retrieval stage.
    """
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        question = json.loads(messages[1]["content"])["question"]
        blocks = [json.loads(line) for line in messages[2]["content"].splitlines()[1:-1]]
        overlap = [(len(bigrams(question) & bigrams(block["text"])), block) for block in blocks]
        overlap.sort(key=lambda pair: -pair[0])
        if not overlap or overlap[0][0] < 3:
            answer = {"status": "insufficient_evidence", "claims": [],
                      "reason": "[mock] 未找到足够字面匹配的资料；这不是模型语义判断。"}
        else:
            block = overlap[0][1]
            answer = {"status": "answered", "claims": [
                {"text": "[mock 原文摘录，非语义回答] " + block["text"][:1200],
                 "source_ids": [block["source_id"]]}], "reason": ""}
        return json.dumps(answer, ensure_ascii=False)
