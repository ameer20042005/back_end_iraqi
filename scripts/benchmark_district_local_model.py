"""Measure the real correction service against a loopback model server.

Run: python -m scripts.benchmark_district_local_model district-correction-all-cases.json
Writes complete results and timings, and prints completed batches as they arrive.
Catalog import/model discovery are measured separately from correction latency.
"""

import argparse
import asyncio
import contextvars
import hashlib
import json
import statistics
import tempfile
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.features.district_correction import llm as llm_module
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.models import CorrectionRequest

CURRENT_BATCH = contextvars.ContextVar("benchmark_batch", default=None)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def benchmark(args):
    base = args.base_url.rstrip("/")
    if urlparse(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This benchmark only connects to a local model server.")
    source = args.input.read_bytes()
    request = CorrectionRequest.model_validate_json(source.decode("utf-8-sig"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model = args.model
    model_info = None
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(base.removesuffix("/v1") + "/api/v0/models")
        if response.is_success:
            model_info = next((item for item in response.json().get("data", [])
                               if item.get("state") == "loaded" and item.get("type") != "embeddings"
                               and (not model or item["id"] == model)), None)
            model = model or (model_info["id"] if model_info else None)
        if not model:
            raise RuntimeError("No loaded model found; use --model to specify one.")

    configuration = replace(settings, district_llm_base_url=base, district_llm_model=model,
                            district_llm_timeout_seconds=args.timeout)
    batches = []
    started = None
    trace_path = args.output_dir / "batches.json"

    class ProfilingHTTPClient(httpx.AsyncClient):
        async def post(self, *positional, **kwargs):
            body = kwargs.get("json", {})
            attempt = {"started_after_seconds": round(time.perf_counter() - started, 3),
                       "structured_output": "response_format" in body}
            before = time.perf_counter()
            try:
                response = await super().post(*positional, **kwargs)
                attempt["http_status"] = response.status_code
                try:
                    payload = response.json()
                    attempt["usage"] = payload.get("usage", {})
                    attempt["finish_reason"] = payload.get("choices", [{}])[0].get("finish_reason")
                    if response.is_error:
                        attempt["server_error"] = payload.get("error")
                except (ValueError, IndexError, AttributeError):
                    pass
                return response
            except httpx.HTTPError as exc:
                attempt["transport_error"] = type(exc).__name__
                raise
            finally:
                attempt["duration_seconds"] = round(time.perf_counter() - before, 3)
                record = CURRENT_BATCH.get()
                if record is not None:
                    record["http_attempts"].append(attempt)

    # Instrument only this process; request bodies and production source stay intact.
    original_httpx = llm_module.httpx
    llm_module.httpx = SimpleNamespace(AsyncClient=ProfilingHTTPClient,
                                     TimeoutException=httpx.TimeoutException, HTTPError=httpx.HTTPError)

    class ProfilingLLM(llm_module.LLMClient):
        async def resolve(self, company, code, cases, names, hints=None):
            before = time.perf_counter()
            record = {"batch": len(batches) + 1, "state": code,
                      "case_sequences": [case.excelSequence for case in cases],
                      "case_count": len(cases), "candidate_count": len(names),
                      "started_after_seconds": round(before - started, 3), "http_attempts": []}
            batches.append(record)
            token = CURRENT_BATCH.set(record)
            try:
                answers = await super().resolve(company, code, cases, names, hints)
                record["answer_count"] = len(answers)
                record["outcome"] = "parsed"
                return answers
            except llm_module.LLMError as exc:
                record["outcome"] = str(exc)
                raise
            finally:
                record["duration_seconds"] = round(time.perf_counter() - before, 3)
                CURRENT_BATCH.reset(token)
                write_json(trace_path, batches)
                print(json.dumps({"event": "batch_completed", "batch": record["batch"], "state": code,
                                  "cases": len(cases), "seconds": record["duration_seconds"],
                                  "outcome": record.get("outcome")}), flush=True)

    class ProfilingService(CorrectionService):
        def __init__(self, *positional, **kwargs):
            super().__init__(*positional, **kwargs)
            self.match_seconds = 0
            self.candidate_seconds = []

        def _match_all(self, *positional):
            before = time.perf_counter()
            result = super()._match_all(*positional)
            self.match_seconds = time.perf_counter() - before
            return result

        def _candidates(self, *positional):
            before = time.perf_counter()
            result = super()._candidates(*positional)
            self.candidate_seconds.append(time.perf_counter() - before)
            return result

    try:
        with tempfile.TemporaryDirectory() as directory:
            before = time.perf_counter()
            catalog_path = Path(directory) / "catalog.sqlite3"
            import_catalog(configuration.district_source_dir, catalog_path)
            catalog = Catalog(catalog_path)
            setup_seconds = time.perf_counter() - before
            limit = args.max_cases if args.max_cases is not None else len(request.cases)
            service = ProfilingService(catalog, ProfilingLLM(configuration), limit, args.concurrency)
            report = {"input_file": str(args.input.resolve()), "input_sha256": hashlib.sha256(source).hexdigest(),
                      "started_utc": datetime.now(timezone.utc).isoformat(),
                      "model": model, "model_info": model_info, "base_url": base,
                      "case_count": len(request.cases), "company": request.companyName,
                      "settings": {"max_llm_cases": limit, "concurrency": args.concurrency,
                                   "timeout_seconds": args.timeout, "chunk_size": 20},
                      "catalog_setup_seconds": round(setup_seconds, 3),
                      "timing_scope": "CorrectionService including real model calls; excludes catalog setup and HTTP API routing",
                      "expected_answers_available": False, "batches": batches}
            write_json(args.output_dir / "report.json", report)
            print(json.dumps({"event": "started", "model": model, "cases": len(request.cases),
                              "max_llm_cases": limit, "concurrency": args.concurrency}), flush=True)
            started = time.perf_counter()
            response, metrics = await service.correct(request)
            elapsed = time.perf_counter() - started
            timings = [batch["duration_seconds"] for batch in batches]
            errors = Counter(row.errorCode for row in response.cases if row.errorCode)
            server_usage = [attempt.get("usage", {}) for batch in batches for attempt in batch["http_attempts"]]
            report.update({"completed_utc": datetime.now(timezone.utc).isoformat(),
                           "total_seconds": round(elapsed, 3),
                           "cases_per_second": round(len(request.cases) / elapsed, 3),
                           "amortized_seconds_per_input_case": round(elapsed / len(request.cases), 3),
                           "local_match_seconds": round(service.match_seconds, 3),
                           "candidate_cpu_seconds_sum": round(sum(service.candidate_seconds), 3),
                           "metrics": metrics, "statuses": dict(Counter(row.status for row in response.cases)),
                           "errors": dict(errors), "batch_count": len(batches),
                           "batch_median_seconds": round(statistics.median(timings), 3) if timings else None,
                           "batch_max_seconds": max(timings, default=None),
                           "token_usage": {field: sum(usage.get(field, 0) for usage in server_usage)
                                           for field in ("prompt_tokens", "completion_tokens", "total_tokens")},
                           "reasoning_tokens": sum((usage.get("completion_tokens_details") or {})
                                                   .get("reasoning_tokens", 0) for usage in server_usage),
                           "http_requests": sum(len(batch["http_attempts"]) for batch in batches),
                           "response_order_preserved": [row.excelSequence for row in response.cases] ==
                                                       [row.excelSequence for row in request.cases]})
            write_json(args.output_dir / "results.json", response.model_dump(mode="json"))
            write_json(args.output_dir / "report.json", report)
            print(json.dumps({key: value for key, value in report.items()
                              if key not in ("batches", "model_info", "input_sha256")}, ensure_ascii=True, indent=2), flush=True)
    finally:
        llm_module.httpx = original_httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--concurrency", type=int, default=settings.district_llm_concurrency)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results/district-local"))
    args = parser.parse_args()
    if args.concurrency < 1 or args.timeout <= 0:
        parser.error("Concurrency and timeout must be positive.")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
