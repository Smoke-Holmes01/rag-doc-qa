"""Retrieval, generation, strict validation, and program-owned citations."""
import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.index import Index, load_index
from app.prompts import messages
from app.providers.base import ChatProvider, EmbeddingProvider
from app.retrieval import CONTEXT_WRAPPER_CHARS, normalize, select_context
from app.schemas import Citation, ModelAnswer, QueryRequest, QueryResponse

logger = logging.getLogger("rag")


@dataclass
class QueryResult:
    response: QueryResponse
    trace: list[dict[str, object]]
    index: Index
    elapsed_ms: float

    @property
    def context_chars(self) -> int:
        return sum(int(item["chars"]) for item in self.trace) + CONTEXT_WRAPPER_CHARS if self.trace else 0


class QueryService:
    def __init__(self, settings: Settings, embedding: EmbeddingProvider, chat: ChatProvider) -> None:
        self.settings, self.embedding, self.chat = settings, embedding, chat
        self.index: Index | None = None

    def ready(self) -> Index:
        # Small corpus: recheck on every entry, including cached index artifacts.
        self.index = load_index(self.settings)
        return self.index

    async def query(self, request: QueryRequest, request_id: str) -> QueryResult:
        started = time.perf_counter()
        settings = self.settings
        if len(request.question) > settings.question_max_chars:
            raise AppError("INVALID_REQUEST", "问题超过 QUESTION_MAX_CHARS", 422)
        top_k = settings.retrieval_top_k if request.top_k is None else request.top_k
        if top_k > settings.retrieval_max_k:
            raise AppError("INVALID_REQUEST", "top_k 超过 RETRIEVAL_MAX_K", 422)
        index = self.ready()
        raw = await self.embedding.embed([request.question])
        query = normalize(raw, 1, index.manifest.dimension)
        hits = select_context(index.vectors, query, index.chunks, top_k,
                              settings.context_max_chars, settings.min_similarity)
        trace = [{"chunk_id": hit.chunk.chunk_id, "filename": hit.chunk.filename,
                  "heading_path": hit.chunk.heading_path, "score": hit.score,
                  "chars": len(hit.block())} for hit in hits]
        logger.info("request_id=%s stage=context mode=%s chunks=%s chars=%s",
                    request_id, settings.model_mode, [hit.chunk.chunk_id for hit in hits],
                    [item["chars"] for item in trace])
        if not hits:
            answer = ModelAnswer(status="insufficient_evidence", claims=[], reason="资料不足：没有预算内的有效检索依据。")
        else:
            try:
                raw_answer = await self.chat.complete(messages(request.question, hits))
            except AppError as exc:
                exc.trace = trace
                raise
            try:
                if len(raw_answer) > 40000:
                    raise ValueError
                answer = ModelAnswer.model_validate_json(raw_answer)
                allowed = {hit.chunk.chunk_id for hit in hits}
                if any(source not in allowed for claim in answer.claims for source in claim.source_ids):
                    raise ValueError
            except (ValidationError, ValueError, TypeError):
                raise AppError("MODEL_RESPONSE_INVALID", "模型回答结构或来源 ID 无效", 502, trace=trace) from None
        cited = dict.fromkeys(source for claim in answer.claims for source in claim.source_ids)
        by_id = {hit.chunk.chunk_id: hit for hit in hits}
        citations = []
        for source in cited:
            hit = by_id[source]
            citations.append(Citation(chunk_id=source, filename=hit.chunk.filename, title=hit.chunk.title,
                                      heading_path=hit.chunk.heading_path, text=hit.chunk.text, score=hit.score))
        response = QueryResponse(request_id=request_id, mode=settings.model_mode, status=answer.status,
                                 answer="\n".join(claim.text for claim in answer.claims) if answer.claims else answer.reason,
                                 claims=answer.claims, citations=citations)
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("request_id=%s stage=complete mode=%s chunks=%s elapsed_ms=%.2f",
                    request_id, settings.model_mode, list(by_id), elapsed)
        return QueryResult(response, trace, index, elapsed)
