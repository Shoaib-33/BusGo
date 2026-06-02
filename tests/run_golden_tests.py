import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:8004"
DATASET_PATH = Path("tests/golden_dataset.json")
REPORT_PATH = Path("tests/golden_report.json")


def norm(value):
    return str(value or "").lower()


def contains_all(text, items):
    text_lower = norm(text)
    return [item for item in items or [] if norm(item) not in text_lower]


def contains_any(text, items):
    if not items:
        return True
    text_lower = norm(text)
    return any(norm(item) in text_lower for item in items)


def post_json(base_url, path, payload, timeout):
    started = time.perf_counter()
    try:
        response = requests.post(f"{base_url}{path}", json=payload, timeout=timeout)
    except requests.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return 0, {"message": f"Connection error: {exc}", "error_type": "connection"}, elapsed_ms
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}
    return response.status_code, data, elapsed_ms


def get_json(base_url, path, params, timeout):
    started = time.perf_counter()
    try:
        response = requests.get(f"{base_url}{path}", params=params, timeout=timeout)
    except requests.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return 0, {"message": f"Connection error: {exc}", "error_type": "connection"}, elapsed_ms
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}
    return response.status_code, data, elapsed_ms


def delete_booking(base_url, booking_id, timeout):
    try:
        requests.delete(f"{base_url}/bookings/{booking_id}", params={"permanent": "true"}, timeout=timeout)
    except requests.RequestException:
        pass


def booking_ids_from_text(text):
    import re

    return re.findall(r"BK\d{5}", text or "")


def is_infrastructure_error(text):
    text_lower = norm(text)
    markers = [
        "temporarily unavailable",
        "quota is exhausted",
        "resourceexhausted",
        "429",
        "google_api_key",
        "api key",
        "connection error",
        "could not process",
    ]
    return any(marker in text_lower for marker in markers)


def get_path(data, path):
    current = data
    parts = path.replace("]", "").replace("[", ".").split(".")
    for part in parts:
        if part == "":
            continue
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def check_expected_text(expected, text, failures):
    missing = contains_all(text, expected.get("must_contain", []))
    if missing:
        failures.append(f"Missing expected text: {missing}")

    forbidden_present = [
        item for item in expected.get("must_not_contain", [])
        if norm(item) in norm(text)
    ]
    if forbidden_present:
        failures.append(f"Forbidden text present: {forbidden_present}")

    any_items = expected.get("must_contain_any", [])
    if any_items and not contains_any(text, any_items):
        failures.append(f"None of must_contain_any appeared: {any_items}")


def check_seat_payload(expected, payload, failures):
    required = expected.get("response_json_must_include", [])
    for key in required:
        if key not in payload:
            failures.append(f"Response JSON missing key: {key}")

    wanted = expected.get("seat_selection_must_contain")
    if not wanted:
        return
    seat_selection = payload.get("seat_selection") or {}
    for key, value in wanted.items():
        if seat_selection.get(key) != value:
            failures.append(
                f"seat_selection.{key} expected {value!r}, got {seat_selection.get(key)!r}"
            )


def run_api_case(case, base_url, timeout):
    status, data, elapsed_ms = get_json(base_url, case["endpoint"], case.get("params", {}), timeout)
    failures = []
    expected = case.get("expected", {})

    if status == 0:
        failures.append(f"Connection error: {data.get('message')}")
    elif status >= 400:
        failures.append(f"HTTP {status}: {data}")

    for key, value in expected.get("json_must_equal", {}).items():
        if data.get(key) != value:
            failures.append(f"JSON key {key} expected {value!r}, got {data.get(key)!r}")

    for path, value in expected.get("json_must_contain_path_values", {}).items():
        try:
            actual = get_path(data, path)
        except Exception as exc:
            failures.append(f"Could not read JSON path {path}: {exc}")
            continue
        if actual != value:
            failures.append(f"JSON path {path} expected {value!r}, got {actual!r}")

    return {
        "id": case["id"],
        "category": case["category"],
        "type": case["type"],
        "passed": not failures,
        "failures": failures,
        "latency_ms": elapsed_ms,
        "steps": [{"kind": "GET", "latency_ms": elapsed_ms, "status": status}],
    }


def run_chat_case(case, base_url, timeout, cleanup):
    session_id = "golden-" + uuid.uuid4().hex
    failures = []
    infra_error = None
    steps = []
    final_payload = {}
    final_text = ""
    created_booking_ids = []

    for index, message in enumerate(case.get("messages", []), start=1):
        status, payload, elapsed_ms = post_json(
            base_url,
            "/query/smart",
            {"query": message, "session_id": session_id},
            timeout,
        )
        text = payload.get("message", "")
        final_payload = payload
        final_text = text
        created_booking_ids.extend(booking_ids_from_text(text))
        steps.append({
            "kind": "chat",
            "message_index": index,
            "latency_ms": elapsed_ms,
            "status": status,
            "message": message,
            "response_preview": text[:300],
        })
        if status == 0:
            infra_error = payload.get("message", "Connection error")
            failures.append(infra_error)
            break
        if status >= 400:
            failures.append(f"Message {index} HTTP {status}: {payload}")
            break
        if is_infrastructure_error(text):
            infra_error = text
            failures.append(f"Infrastructure/model error: {text}")
            break

    expected = case.get("expected", {})

    if infra_error:
        pass
    elif "before_seat_confirm" in expected:
        before = expected["before_seat_confirm"]
        check_expected_text(before, final_text, failures)
        check_seat_payload(before, final_payload, failures)
        selected_seats = expected.get("after_seat_confirm", {}).get("selected_seats")
        if selected_seats:
            status, payload, elapsed_ms = post_json(
                base_url,
                "/chat/confirm-seat-booking",
                {"session_id": session_id, "selected_seats": selected_seats},
                timeout,
            )
            text = payload.get("message", "")
            final_payload = payload
            final_text = text
            created_booking_ids.extend(booking_ids_from_text(text))
            steps.append({
                "kind": "seat_confirm",
                "latency_ms": elapsed_ms,
                "status": status,
                "selected_seats": selected_seats,
                "response_preview": text[:300],
            })
            if status >= 400:
                failures.append(f"Seat confirmation HTTP {status}: {payload}")
            check_expected_text(expected["after_seat_confirm"], text, failures)
    else:
        check_expected_text(expected, final_text, failures)

    if not infra_error and expected.get("booking_should_be_created") is False and created_booking_ids:
        failures.append(f"Booking was created unexpectedly: {created_booking_ids}")

    if cleanup:
        for booking_id in sorted(set(created_booking_ids)):
            delete_booking(base_url, booking_id, timeout)

    total_latency = round(sum(step["latency_ms"] for step in steps), 2)
    return {
        "id": case["id"],
        "category": case["category"],
        "type": case["type"],
        "passed": not failures,
        "error_type": "infrastructure" if infra_error else None,
        "failures": failures,
        "latency_ms": total_latency,
        "steps": steps,
        "created_booking_ids": sorted(set(created_booking_ids)),
    }


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def main():
    parser = argparse.ArgumentParser(description="Run BusGo golden evaluation tests.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--category", action="append", help="Run only this category. Can be passed multiple times.")
    parser.add_argument("--case", action="append", help="Run only this case id. Can be passed multiple times.")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--no-cleanup", action="store_true", help="Keep bookings created during evaluation.")
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    cases = dataset["cases"]
    if args.category:
        categories = set(args.category)
        cases = [case for case in cases if case.get("category") in categories]
    if args.case:
        ids = set(args.case)
        cases = [case for case in cases if case.get("id") in ids]

    results = []
    print(f"Running {len(cases)} golden case(s) against {args.base_url}\n")

    for case in cases:
        started = time.perf_counter()
        if case.get("type") == "api":
            result = run_api_case(case, args.base_url, args.timeout)
        else:
            result = run_chat_case(case, args.base_url, args.timeout, cleanup=not args.no_cleanup)
        wall_ms = round((time.perf_counter() - started) * 1000, 2)
        result["wall_latency_ms"] = wall_ms
        results.append(result)

        marker = "PASS" if result["passed"] else "FAIL"
        marker = "ERROR" if result.get("error_type") == "infrastructure" else marker
        print(f"[{marker}] {case['id']} ({case['category']}) {result['latency_ms']} ms")
        for failure in result["failures"]:
            print(f"  - {failure}")
        if result["failures"] and result.get("steps"):
            preview = result["steps"][-1].get("response_preview")
            if preview:
                print(f"  Response preview: {preview}")

    latencies = [result["latency_ms"] for result in results]
    passed = sum(1 for result in results if result["passed"])
    errored = sum(1 for result in results if result.get("error_type") == "infrastructure")
    failed = len(results) - passed - errored
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "pass_rate": round((passed / len(results)) * 100, 2) if results else 0,
        "latency_ms": {
            "avg": round(statistics.mean(latencies), 2) if latencies else None,
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p90": percentile(latencies, 90),
            "max": max(latencies) if latencies else None,
        },
    }

    report = {
        "base_url": args.base_url,
        "dataset": args.dataset,
        "summary": summary,
        "results": results,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nSummary")
    print(json.dumps(summary, indent=2))
    print(f"\nReport written to {args.report}")

    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
