# BusGo RAGAS Report Diagnosis

Report checked: `tests/ragas_all_report.json` and `tests/ragas_all_report.csv`

## High-level result

- The report contains only **10 attempted cases**, not all 20.
- Of those, **8 succeeded** and **2 failed**.
- The two failed cases returned HTTP 503 because the Gemini API quota was exhausted.

Failed cases:

1. `route_chattogram_coxbazar_cheapest`
2. `route_dhaka_mymensingh_cheapest`

Error:

```text
The AI assistant is temporarily unavailable because the Gemini API quota is exhausted.
```

## Why some RAGAS metric columns are blank / NaN

These metrics are NaN in the current report:

- `answer_relevancy`
- `answer_correctness`
- `answer_similarity`

Most likely cause:

- The evaluator model / embedding calls failed during the RAGAS run because of API quota/rate-limit exhaustion.
- RAGAS was run with `raise_exceptions=False`, so failed metric calls became NaN instead of stopping the script.

## Metric averages from successful rows

Available metric averages:

- `faithfulness`: about **0.848**
- `context_precision`: about **0.885**
- `context_recall`: **1.0**
- `context_entity_recall`: about **0.866**

Interpretation:

- Context recall is strong: required information is usually somewhere in retrieved contexts.
- Context precision is lower because the retriever often includes extra unrelated route/provider chunks.
- Faithfulness is mostly good, but a few answers were penalized.

## Main bad-result causes

### 1. API quota exhaustion

Two chatbot calls failed before RAGAS could evaluate them.

This makes the report incomplete and also likely caused NaN scores for some judge-based metrics.

### 2. Retriever returns too many irrelevant chunks

Example: for Dhaka to Rangpur questions, retrieved contexts also include Dhaka to Chattogram, Tangail, Green Line policy, dropping points, etc.

This hurts:

- `context_precision`
- sometimes `context_entity_recall`
- latency

### 3. Some answers are correct but RAGAS faithfulness penalizes wording

Example:

`route_dhaka_barishal_cheapest`

Answer is factually correct:

```text
Soudia Non-AC, 380 Taka, 06:00, 13:30, 19:30
```

But faithfulness is only `0.4`.

Likely reason:

- RAGAS statement decomposition judged some generated wording such as “cheapest service” or “their services” strictly.
- This looks like a RAGAS false negative, because the answer is supported by the context.

### 4. Some answers are incomplete

Example:

`route_dhaka_rajshahi_under_500`

Reference expects:

- Soudia Non-AC, 400 Taka
- National Travels Non-AC, 420 Taka

Assistant answered only:

- Soudia Non-AC, 400 Taka

This is a real answer-quality issue. `answer_correctness` would catch this, but that metric is NaN in the current report because evaluator calls failed.

### 5. Some answers include extra information

Example:

`route_dhaka_coxbazar_ac_cheapest`

Question asks cheapest AC option.

Assistant gives:

- Shyamoli AC, 1200 Taka
- Royal Coach AC Volvo, 1400 Taka

It still identifies Shyamoli as cheapest, but the extra Royal Coach detail can reduce precision/correctness scores if the reference only expects the cheapest option.

## What to do next

### Re-run after quota resets

Run fewer metrics at a time:

```powershell
.\venv\Scripts\python.exe run_ragas_eval.py --from-report tests\ragas_report.json --limit 20 --metrics faithfulness,context_precision,context_recall,context_entity_recall --max-contexts 3 --report-json tests\ragas_retrieval_report.json --report-csv tests\ragas_retrieval_report.csv
```

Then:

```powershell
.\venv\Scripts\python.exe run_ragas_eval.py --from-report tests\ragas_report.json --limit 20 --metrics answer_relevancy,answer_correctness,answer_similarity --max-contexts 3 --report-json tests\ragas_answer_report.json --report-csv tests\ragas_answer_report.csv
```

### Improve retrieval

- Return fewer contexts to the LLM/evaluator.
- Prefer exact route summary chunks first.
- Filter out unrelated routes when source and destination are detected.
- Filter out policy/provider chunks for simple route/fare questions unless the user asks policy/contact/refund.

### Improve answer completeness

For queries like “under 500 taka”, the assistant should list all matching services, not only the cheapest one.

### Improve answer focus

For “cheapest” questions, answer the cheapest option first and avoid extra non-cheapest providers unless the user asks for comparison.

