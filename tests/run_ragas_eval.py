import argparse
import json
import os
import time
from pathlib import Path

import requests
from datasets import Dataset
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from ragas import evaluate
from ragas.metrics import (
    answer_correctness,
    answer_relevancy,
    answer_similarity,
    context_entity_recall,
    context_precision,
    context_recall,
    faithfulness,
)


DEFAULT_DATASET = Path("tests/ragas_dataset_20.json")
DEFAULT_REPORT_JSON = Path("tests/ragas_report.json")
DEFAULT_REPORT_CSV = Path("tests/ragas_report.csv")


def extract_contexts(payload):
    contexts = payload.get("contexts")
    if contexts:
        return [str(item) for item in contexts if str(item).strip()]

    docs = payload.get("source_documents") or []
    extracted = []
    for doc in docs:
        if isinstance(doc, dict):
            text = doc.get("page_content") or doc.get("content") or doc.get("text")
        else:
            text = getattr(doc, "page_content", None)
        if text:
            extracted.append(str(text))
    return extracted


def call_chatbot(base_url, case, timeout):
    started = time.perf_counter()
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/query/detailed",
            json={
                "query": case["user_input"],
                "session_id": f"ragas-{case['id']}",
            },
            timeout=timeout,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        payload = response.json()
        if not response.ok:
            detail = payload.get("detail") if isinstance(payload, dict) else response.text
            return {
                "id": case["id"],
                "user_input": case["user_input"],
                "response": "",
                "retrieved_contexts": [],
                "reference": case["reference"],
                "latency_ms": elapsed_ms,
                "error": f"HTTP {response.status_code}: {detail}",
            }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "id": case["id"],
            "user_input": case["user_input"],
            "response": "",
            "retrieved_contexts": [],
            "reference": case["reference"],
            "latency_ms": elapsed_ms,
            "error": str(exc),
        }

    return {
        "id": case["id"],
        "user_input": case["user_input"],
        "response": payload.get("answer") or payload.get("message") or "",
        "retrieved_contexts": extract_contexts(payload),
        "reference": case["reference"],
        "latency_ms": elapsed_ms,
    }


def collect_rows(dataset_path, base_url, timeout, limit):
    cases = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    if limit:
        cases = cases[:limit]

    rows = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case['id']}")
        row = call_chatbot(base_url, case, timeout)
        if row.get("error"):
            print(f"  ERROR latency={row['latency_ms']} ms {row['error']}")
        else:
            print(f"  latency={row['latency_ms']} ms contexts={len(row['retrieved_contexts'])}")
        rows.append(row)
    return rows


def build_judge_llm():
    model = os.getenv("RAGAS_GOOGLE_MODEL") or os.getenv("GOOGLE_MODEL") or "gemini-1.5-flash"
    return ChatGoogleGenerativeAI(model=model, temperature=0)


def build_embeddings():
    model = os.getenv("RAGAS_EMBEDDING_MODEL") or "models/text-embedding-004"
    return GoogleGenerativeAIEmbeddings(model=model)


def choose_metrics(metric_names):
    available = {
        "faithfulness": faithfulness,
        "answer_relevancy": answer_relevancy,
        "context_precision": context_precision,
        "context_recall": context_recall,
        "answer_correctness": answer_correctness,
        "answer_similarity": answer_similarity,
        "context_entity_recall": context_entity_recall,
    }
    if metric_names.strip().lower() in {"all", "all_applicable"}:
        return [
            faithfulness,
            answer_relevancy,
            context_precision,
            context_recall,
            answer_correctness,
            answer_similarity,
            context_entity_recall,
        ]
    selected = []
    for name in metric_names.split(","):
        key = name.strip()
        if not key:
            continue
        if key not in available:
            raise ValueError(f"Unknown metric '{key}'. Available: {', '.join(available)}")
        selected.append(available[key])
    return selected


def main():
    parser = argparse.ArgumentParser(description="Run RAGAS evaluation for BusGo chatbot RAG answers.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--base-url", default="http://127.0.0.1:8004")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--from-report", help="Use rows from an existing JSON report instead of calling the chatbot.")
    parser.add_argument("--metrics", default="faithfulness,answer_relevancy,context_precision,context_recall")
    parser.add_argument("--max-contexts", type=int, default=5)
    parser.add_argument("--report-json", default=str(DEFAULT_REPORT_JSON))
    parser.add_argument("--report-csv", default=str(DEFAULT_REPORT_CSV))
    args = parser.parse_args()

    load_dotenv()

    if args.from_report:
        previous = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
        rows = previous.get("rows", [])
        if args.limit:
            rows = rows[:args.limit]
    else:
        rows = collect_rows(args.dataset, args.base_url, args.timeout, args.limit)

    successful_rows = [row for row in rows if not row.get("error")]
    error_rows = [row for row in rows if row.get("error")]

    report = {
        "base_url": args.base_url,
        "dataset": args.dataset,
        "total": len(rows),
        "successful": len(successful_rows),
        "errored": len(error_rows),
        "latency_ms": {
            "avg": round(sum(row["latency_ms"] for row in rows) / len(rows), 2) if rows else 0,
            "max": max((row["latency_ms"] for row in rows), default=0),
        },
        "rows": rows,
    }

    if not args.collect_only and successful_rows:
        dataset = Dataset.from_list([
            {
                "user_input": row["user_input"],
                "response": row["response"],
                "retrieved_contexts": row["retrieved_contexts"][:args.max_contexts],
                "reference": row["reference"],
            }
            for row in successful_rows
        ])
        metrics = choose_metrics(args.metrics)
        result = evaluate(
            dataset,
            metrics=metrics,
            llm=build_judge_llm(),
            embeddings=build_embeddings(),
            raise_exceptions=False,
            batch_size=2,
        )
        scores_df = result.to_pandas()
        scores_df.insert(0, "id", [row["id"] for row in successful_rows])
        scores_df.insert(1, "latency_ms", [row["latency_ms"] for row in successful_rows])

        report["ragas"] = {
            "summary": {
                column: round(float(scores_df[column].mean()), 4)
                for column in scores_df.columns
                if column not in {"id", "user_input", "response", "retrieved_contexts", "reference"}
                and scores_df[column].dtype.kind in "fc"
            },
            "scores": scores_df.to_dict(orient="records"),
        }
        Path(args.report_csv).parent.mkdir(parents=True, exist_ok=True)
        scores_df.to_csv(args.report_csv, index=False, encoding="utf-8")

    Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report_json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nSummary")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2, ensure_ascii=False))
    print(f"\nReport written to {args.report_json}")
    if not args.collect_only:
        print(f"CSV written to {args.report_csv}")


if __name__ == "__main__":
    main()
