"""Separate context-evidence coverage from pending human semantic review."""
import argparse
import asyncio
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import ROOT, Settings, load_settings
from app.documents import sha256
from app.errors import AppError
from app.index import json_bytes
from app.main import providers
from app.schemas import QueryRequest
from app.service import QueryService


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = {name: sum(case["category"] == name for case in cases) for name in
              ("single", "combined", "insufficient", "document_injection", "question_injection")}
    if counts != {"single": 8, "combined": 2, "insufficient": 3, "document_injection": 1, "question_injection": 1}:
        raise AppError("EVALUATION_INVALID", "评测类别或样本数量不符合固定的 15 条约定")
    if len({case["case_id"] for case in cases}) != 15:
        raise AppError("EVALUATION_INVALID", "评测 case_id 不唯一")
    return cases


def evidence_matches(case: dict, result) -> list[bool]:
    chunks = {chunk.chunk_id: chunk for chunk in result.index.chunks}
    context = [chunks[item["chunk_id"]] for item in result.trace]
    return [any(chunk.filename == expected["filename"]
                and (not expected["heading"] or expected["heading"] in chunk.heading_path)
                and expected["quote"] in chunk.text for chunk in context)
            for expected in case["expected_evidence"]]


def public_config(settings: Settings) -> dict:
    return {"mode": settings.model_mode, "embedding": settings.embedding_identity(),
            "chat": {"adapter": "mock" if settings.model_mode == "mock" else settings.chat_adapter,
                     "model": "extractive-double-v1" if settings.model_mode == "mock" else settings.chat_model,
                     "service_id": "offline-v1" if settings.model_mode == "mock" else sha256(settings.chat_base_url.rstrip("/").encode())},
            "chunking": settings.chunk_config(), "retrieval_top_k": settings.retrieval_top_k,
            "retrieval_max_k": settings.retrieval_max_k, "context_max_chars": settings.context_max_chars,
            "min_similarity": settings.min_similarity, "http_timeout_seconds": settings.http_timeout_seconds,
            "embedding_batch_size": settings.embedding_batch_size,
            "question_max_chars": settings.question_max_chars,
            "chat_max_output_tokens": settings.chat_max_output_tokens, "retry_count": 0}


async def run(mode: str, output_dir: Path | None = None) -> int:
    timestamp = datetime.now(timezone(timedelta(hours=8)))
    run_id = timestamp.strftime("%Y%m%dT%H%M%S%f%z") + "_" + uuid.uuid4().hex
    directory = (output_dir or ROOT / "reports" / mode) / run_id
    # A run is never reused, even when timestamps coincide. Existing reviews stay untouched.
    directory.mkdir(parents=True, exist_ok=False)
    report: dict = {"run_id": run_id, "mode": mode, "timestamp": timestamp.isoformat(),
                    "notice": "流程验证，不代表检索语义或 LLM 回答质量" if mode == "mock" else "真实接口评测；回答语义待人工核验",
                    "semantic_status": "pending_review", "cases": [], "execution_errors": []}
    started = time.perf_counter()
    code = 0
    try:
        settings = load_settings(model_mode=mode)
        report["config"] = public_config(settings)
        settings.check_live()
        cases = load_cases(ROOT / "eval/cases.jsonl")
        cases_snapshot = json_bytes(cases)
        (directory / "cases.json").write_bytes(cases_snapshot)
        report["cases_fingerprint"] = sha256(cases_snapshot)
        async with httpx.AsyncClient(timeout=settings.timeout(), follow_redirects=False) as client:
            embedding, chat = providers(settings, client)
            service = QueryService(settings, embedding, chat)
            index = service.ready()
            report["index_fingerprint"] = index.fingerprint
            report["corpus_fingerprint"] = index.manifest.corpus_fingerprint
            report["dimension"] = index.manifest.dimension
            # Preserve the exact corpus fragments for later review after corpus/index changes.
            (directory / "chunks.json").write_bytes(json_bytes([chunk.model_dump() for chunk in index.chunks]))
            (directory / "manifest.json").write_bytes(json_bytes(index.manifest.model_dump()))
            covered, denominator = 0, 0
            for case in cases:
                entry = {"case_id": case["case_id"], "category": case["category"],
                         "question": case["question"], "expected_status": case["expected_status"],
                         "expected_evidence": case["expected_evidence"], "answer_checks": case["answer_checks"],
                         "semantic_status": "pending_review"}
                case_start = time.perf_counter()
                if case["expected_evidence"]:
                    denominator += 1
                try:
                    result = await service.query(QueryRequest(question=case["question"]), uuid.uuid4().hex)
                    matches = evidence_matches(case, result)
                    entry.update(output=result.response.model_dump(), context=result.trace,
                                 context_chars=result.context_chars,
                                 evidence_matches=matches,
                                 all_expected_in_context=all(matches) if matches else None)
                    if matches and all(matches):
                        covered += 1
                except AppError as exc:
                    entry["error"] = exc.body("evaluation")["error"]
                    entry["context"] = exc.trace or []
                    code = 1
                entry["elapsed_ms"] = round((time.perf_counter() - case_start) * 1000, 3)
                report["cases"].append(entry)
                print(json.dumps({"run_id": run_id, "case_id": case["case_id"], "mode": mode,
                                  "stage": "case_complete", "error_code": entry.get("error", {}).get("code"),
                                  "elapsed_ms": entry["elapsed_ms"]}), flush=True)
            report["retrieval"] = {"all_evidence_cases_covered": covered, "denominator": denominator,
                                   "fraction": covered / denominator if denominator else None,
                                   "rule": "预期文件、章节及必要原文均进入实际预算后上下文；组合题须覆盖全部依据；无依据题排除。mock 数值仅检查流程。"}
    except AppError as exc:
        report["execution_errors"].append(exc.body("evaluation")["error"])
        code = 1
    except (OSError, ValueError, KeyError):
        report["execution_errors"].append({"code": "EVALUATION_INVALID", "message": "评测输入或读写失败", "request_id": "evaluation"})
        code = 1
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    report["execution_status"] = "completed" if code == 0 else "failed"
    with (directory / "evaluation.json").open("xb") as handle:
        handle.write(json_bytes(report))
    with (directory / "review.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "mode", "reviewer_type", "semantic_status", "faithful_to_text",
                         "numbers_and_exceptions", "citations_support_claims", "refusal_appropriate", "injection_ignored", "notes"])
        for entry in report["cases"]:
            writer.writerow([entry["case_id"], mode, "unassigned", "pending_review", "", "", "", "", "", ""])
    print(json.dumps({"run_id": run_id, "report_directory": str(directory.relative_to(ROOT)) if directory.is_relative_to(ROOT) else str(directory),
                      "mode": mode, "execution_status": report["execution_status"],
                      "cases_executed": len(report["cases"]), "semantic_status": "pending_review",
                      "notice": report["notice"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "live"], required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.mode)))
