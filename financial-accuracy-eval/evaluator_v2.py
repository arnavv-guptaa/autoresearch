#!/usr/bin/env python3
"""
Financial Analysis Accuracy Evaluator v2 — Hybrid (Regex + LLM)

Uses regex for ground truth extraction and deterministic verification,
delegates claim extraction and complex verification to an LLM.

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python evaluator_v2.py --sample-file runs/sample_5.json --sample-size 5
    python evaluator_v2.py --compare-models --sample-file runs/sample_5.json  # benchmark models
"""

import argparse
import json
import os
import re
import sys
import time
import datetime
from pathlib import Path

import requests

from evaluator import (
    parse_prompt,
    extract_response_json,
    flatten_json_texts,
    verify_claims,
    _compare,
    RUNS_DIR,
    RESULTS_TSV,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = None

# Models to benchmark (ordered by cost)
CANDIDATE_MODELS = [
    # Free tier
    "nvidia/nemotron-3-super-120b-a12b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "openai/gpt-oss-120b:free",
    "minimax/minimax-m2.5:free",
    # Cheap paid
    "qwen/qwen3-235b-a22b-2507",
    "google/gemini-2.0-flash-001",
    "mistralai/mistral-small-3.1-24b-instruct",
]

DEFAULT_MODEL = None  # set after benchmarking

MAX_RETRIES = 3
RATE_LIMIT_DELAY = 1.5


def init_api():
    global API_KEY
    API_KEY = os.environ.get("OPENROUTER_API_KEY")
    if not API_KEY:
        print("ERROR: Set OPENROUTER_API_KEY environment variable")
        sys.exit(1)


def llm_call(model: str, messages: list, max_tokens: int = 4096, temperature: float = 0) -> str | None:
    """Call a model via OpenRouter. Returns the text response or None."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/parallax-eval",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RATE_LIMIT_DELAY * (attempt + 1))
                continue
            return None
    return None


# ---------------------------------------------------------------------------
# LLM Claim Extraction
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """\
Extract EVERY numerical claim from this financial analysis into a JSON array.

For each number (percentages, dollar amounts, ratios/multiples), output:
{
  "metric": "<metric_name>",
  "value": <number>,
  "year": "<YYYY or null>",
  "unit": "<dollars_m|percent|ratio|per_share>",
  "text": "<5-15 word snippet>"
}

METRIC NAMES — use exactly these:
- Margins: gross_margin, operating_margin, net_margin, ebitda_margin, pretax_margin
- Returns: roe, roa, roic, roi
- Valuation: pe, pb, pfcf, price_sales, ev_revenue, ev_ebit
- Growth: revenue_growth, net_income_growth, eps_growth (append _cagr for CAGRs)
- Leverage: interest_coverage, ebitda_interest_coverage, debt_equity, total_debt_ebitda, net_debt_to_ebitda, assets_equity
- Liquidity: current_ratio, quick_ratio, dividend_yield, fcf_yield
- Dollar items: revenue, gross_profit, operating_income, net_income, ebitda, operating_cash_flow, free_cash_flow, capex, total_assets, total_equity, total_debt, total_liabilities, cash, depreciation, dividends_paid, interest_expense, shares_outstanding, eps, cost_of_revenue, inventory, accounts_receivable, goodwill, sga_expense, rd_expense, total_current_assets, total_current_liabilities, ppe
- Computed: asset_turnover, effective_tax_rate, cash_cycle_days
- NOT verifiable: segment_revenue, quarterly_figure, scenario_projection, change_pct, other

CRITICAL RULES:
1. In "revenue $75B ÷ assets $60B" → $75B is revenue, $60B is total_assets
2. In "FCF = $20B OCF - $5B CapEx" → $20B is operating_cash_flow, $5B is capex
3. "$7.8B SG&A (38% of revenue)" → $7.8B is sga_expense, 38% is other
4. "Bull case: 40x P/E" or "target P/E of 25x" → scenario_projection
5. "ROE declined 47% from 2023" → 47 is change_pct, NOT roe
6. "segment revenue of $4.16B" → segment_revenue
7. Dollar values: convert to millions (e.g. $75.05B → 75050, $916M → 916)
8. For DuPont "ROE = ROA × Equity Multiplier": label each component correctly

Return ONLY the JSON array. No markdown, no explanation."""


def extract_claims_llm(response_text: str, model: str, max_text_len: int = 8000) -> list[dict]:
    """Use LLM to extract numerical claims from response text."""
    if len(response_text) > max_text_len:
        response_text = response_text[:max_text_len]

    raw = llm_call(model, [
        {"role": "user", "content": f"{EXTRACTION_PROMPT}\n\n---\nTEXT:\n{response_text}"}
    ])

    if not raw:
        return []

    # Parse JSON
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```\w*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)

    # Find array bounds
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]

    try:
        claims = json.loads(raw)
    except json.JSONDecodeError:
        # Try to salvage truncated JSON — add closing brackets
        for suffix in ["]", "}]", "\"}]"]:
            try:
                claims = json.loads(raw + suffix)
                break
            except json.JSONDecodeError:
                continue
        else:
            return []

    # Normalize — handle different models returning different formats
    METRIC_ALIASES = {
        "dividend yield": "dividend_yield", "dividend_yield": "dividend_yield",
        "roe": "roe", "return on equity": "roe", "return_on_equity": "roe",
        "roa": "roa", "return on assets": "roa", "return_on_assets": "roa",
        "roic": "roic", "return on invested capital": "roic",
        "roi": "roi", "return on investment": "roi",
        "pe": "pe", "p/e": "pe", "p/e ratio": "pe", "pe_ratio": "pe", "price to earnings": "pe",
        "pb": "pb", "p/b": "pb", "price to book": "pb",
        "pfcf": "pfcf", "p/fcf": "pfcf",
        "gross margin": "gross_margin", "gross_margin": "gross_margin",
        "operating margin": "operating_margin", "operating_margin": "operating_margin",
        "net margin": "net_margin", "net_margin": "net_margin", "net profit margin": "net_margin",
        "ebitda margin": "ebitda_margin", "ebitda_margin": "ebitda_margin",
        "revenue": "revenue", "total revenue": "revenue",
        "net income": "net_income", "net_income": "net_income",
        "operating income": "operating_income", "operating_income": "operating_income",
        "gross profit": "gross_profit", "gross_profit": "gross_profit",
        "operating cash flow": "operating_cash_flow", "cash from operations": "operating_cash_flow",
        "free cash flow": "free_cash_flow", "fcf": "free_cash_flow",
        "total assets": "total_assets", "total_assets": "total_assets",
        "total equity": "total_equity", "total_equity": "total_equity",
        "total debt": "total_debt", "total_debt": "total_debt",
        "total liabilities": "total_liabilities",
        "cash": "cash", "cash and equivalents": "cash",
        "capex": "capex", "capital expenditure": "capex", "capital expenditures": "capex",
        "depreciation": "depreciation", "depreciation and amortization": "depreciation",
        "interest expense": "interest_expense", "interest_expense": "interest_expense",
        "dividends": "dividends_paid", "dividends paid": "dividends_paid",
        "eps": "eps", "earnings per share": "eps",
        "shares outstanding": "shares_outstanding",
        "current ratio": "current_ratio", "current_ratio": "current_ratio",
        "quick ratio": "quick_ratio", "quick_ratio": "quick_ratio",
        "debt to equity": "debt_equity", "debt_equity": "debt_equity", "d/e ratio": "debt_equity", "d/e": "debt_equity",
        "asset turnover": "asset_turnover", "asset_turnover": "asset_turnover",
        "interest coverage": "interest_coverage", "interest_coverage": "interest_coverage",
        "ebitda interest coverage": "ebitda_interest_coverage",
        "revenue growth": "revenue_growth", "revenue_growth": "revenue_growth",
        "net income growth": "net_income_growth",
        "eps growth": "eps_growth",
        "revenue growth cagr": "revenue_growth_cagr", "revenue_growth_cagr": "revenue_growth_cagr",
        "fcf yield": "fcf_yield", "free cash flow yield": "fcf_yield",
        "ev/revenue": "ev_revenue", "ev_revenue": "ev_revenue",
        "ev/ebit": "ev_ebit", "ev_ebit": "ev_ebit",
        "total debt/ebitda": "total_debt_ebitda", "total_debt_ebitda": "total_debt_ebitda",
        "net debt/ebitda": "net_debt_to_ebitda", "net_debt_to_ebitda": "net_debt_to_ebitda",
        "equity multiplier": "assets_equity", "assets_equity": "assets_equity",
        "effective tax rate": "effective_tax_rate", "effective_tax_rate": "effective_tax_rate",
        "ebitda": "ebitda",
        "cost of revenue": "cost_of_revenue",
        "sga": "sga_expense", "sg&a": "sga_expense", "sga_expense": "sga_expense",
        "r&d": "rd_expense", "rd_expense": "rd_expense",
        "inventory": "inventory",
        "accounts receivable": "accounts_receivable",
        "goodwill": "goodwill",
        "ppe": "ppe", "pp&e": "ppe",
        "segment revenue": "segment_revenue",
        "scenario projection": "scenario_projection", "scenario_projection": "scenario_projection",
        "change percentage": "change_pct", "change_pct": "change_pct",
        "price/sales": "price_sales", "price_sales": "price_sales",
    }

    UNIT_ALIASES = {
        "%": "percent", "percent": "percent", "percentage": "percent",
        "m": "dollars", "M": "dollars", "dollars_m": "dollars", "dollars": "dollars",
        "dollar": "dollars", "millions": "dollars", "usd": "dollars",
        "b": "dollars", "billion": "dollars", "billions": "dollars",
        "x": "ratio", "ratio": "ratio", "multiple": "ratio", "times": "ratio",
        "per_share": "percent",  # EPS gets treated as a raw number
    }

    normalized = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        raw_metric = str(c.get("metric", "unknown")).lower().strip()
        metric = METRIC_ALIASES.get(raw_metric, raw_metric)

        value = c.get("value")
        if value is None:
            continue
        try:
            value = float(str(value).replace(",", ""))
        except (ValueError, TypeError):
            continue

        raw_unit = str(c.get("unit", "unknown")).lower().strip()
        unit = UNIT_ALIASES.get(raw_unit, raw_unit)

        # Handle billion conversion if model didn't convert
        if raw_unit.lower() in ("b", "billion", "billions") and value < 10000:
            value = value * 1000  # convert to millions

        year = c.get("year")
        if year and str(year).lower() not in ("null", "none", "n/a", ""):
            year = str(year).strip()
            # Extract just the year if it's a full date
            ym = re.match(r'(\d{4})', year)
            year = ym.group(1) if ym else None
        else:
            year = None

        normalized.append({
            "claim_text": str(c.get("text", c.get("claim_text", "")))[:120],
            "metric": metric,
            "value": value,
            "year": year,
            "unit": unit,
            "context_field": "llm_extracted",
        })

    return normalized


# ---------------------------------------------------------------------------
# LLM Verification for Unverifiable Claims
# ---------------------------------------------------------------------------

VERIFY_PROMPT = """\
Verify this financial claim against the source data.

CLAIM: "{text}" → {metric} = {value} ({unit}) for year {year}

SOURCE DATA:
{ground_truth}

Compute the expected value from the source data. Return ONLY this JSON:
{{"expected": <number or null>, "source": "<brief calculation>", "verdict": "<correct|warning|error|unverifiable>", "deviation_pct": <number or null>}}

Verdicts: correct (≤1% dev), warning (1-2%), error (>2%), unverifiable (can't determine from data).
Dollar values in millions."""


def build_gt_summary(gt: dict) -> str:
    """Build concise ground truth text for LLM verification."""
    lines = []
    years = gt.get("years", [])
    lines.append(f"Years: {years}")

    for section, label in [("income_statement", "IS"), ("balance_sheet", "BS"),
                            ("cash_flow", "CF"), ("ratios", "Ratios")]:
        data = gt.get(section, {})
        if data:
            for k, v in data.items():
                if k.startswith("_"):
                    continue
                lines.append(f"{label}.{k}: {v}")

    ls = gt.get("latest_stats", {})
    if ls:
        lines.append(f"Latest: {json.dumps(ls)}")
    gr = gt.get("growth_rates", {})
    if gr:
        lines.append(f"Growth: {json.dumps(gr)}")

    return "\n".join(lines)


def verify_claim_llm(claim: dict, gt: dict, model: str) -> dict | None:
    """Use LLM to verify a single claim."""
    gt_text = build_gt_summary(gt)
    prompt = VERIFY_PROMPT.format(
        text=claim.get("claim_text", ""),
        metric=claim.get("metric", "?"),
        value=claim.get("value"),
        unit=claim.get("unit", "?"),
        year=claim.get("year", "?"),
        ground_truth=gt_text,
    )

    raw = llm_call(model, [{"role": "user", "content": prompt}], max_tokens=300)
    if not raw:
        return None

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```\w*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)

    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        pass
    return None


# ---------------------------------------------------------------------------
# Model Benchmark
# ---------------------------------------------------------------------------

def benchmark_models(sample_file: str, n_rows: int = 3):
    """Test multiple models on a small sample and compare quality."""
    with open(sample_file) as f:
        rows = json.load(f)[:n_rows]

    # Get ground truth + response text for each row
    test_cases = []
    for row in rows:
        gt = parse_prompt(row.get("prompt", ""))
        resp_json = extract_response_json(row.get("response", ""))
        if resp_json is None:
            continue
        texts = flatten_json_texts(resp_json)
        full_text = "\n".join(t for _, t in texts)
        test_cases.append({"id": row["id"], "gt": gt, "text": full_text, "symbol": gt.get("company", "?")})

    if not test_cases:
        print("No valid test cases")
        return

    print(f"Benchmarking {len(CANDIDATE_MODELS)} models on {len(test_cases)} rows...\n")
    print(f"{'Model':<50s} {'Claims':>6s} {'Verified':>8s} {'Correct':>7s} {'Errors':>6s} {'Acc%':>6s} {'Unver':>6s} {'Time':>6s}")
    print("-" * 100)

    results = {}
    for model in CANDIDATE_MODELS:
        t0 = time.time()
        total_claims = 0
        total_correct = 0
        total_errors = 0
        total_warnings = 0
        total_unverifiable = 0
        failed = False

        for tc in test_cases:
            claims = extract_claims_llm(tc["text"], model)
            time.sleep(RATE_LIMIT_DELAY)
            if not claims:
                failed = True
                break
            verified = verify_claims(claims, tc["gt"])
            total_claims += len(verified)
            total_correct += sum(1 for v in verified if v["verdict"] == "correct")
            total_errors += sum(1 for v in verified if v["verdict"] == "error")
            total_warnings += sum(1 for v in verified if v["verdict"] == "warning")
            total_unverifiable += sum(1 for v in verified if v["verdict"] == "unverifiable")

        elapsed = time.time() - t0
        verifiable = total_correct + total_errors + total_warnings
        accuracy = (total_correct / verifiable * 100) if verifiable > 0 else 0
        ver_rate = (verifiable / total_claims * 100) if total_claims > 0 else 0

        if failed:
            print(f"{model:<50s} {'FAILED':>6s}")
        else:
            print(f"{model:<50s} {total_claims:>6d} {verifiable:>8d} {total_correct:>7d} {total_errors:>6d} {accuracy:>5.1f}% {total_unverifiable:>6d} {elapsed:>5.1f}s")
            results[model] = {
                "claims": total_claims,
                "correct": total_correct,
                "errors": total_errors,
                "unverifiable": total_unverifiable,
                "accuracy": accuracy,
                "ver_rate": ver_rate,
                "time": elapsed,
            }

    if results:
        # Pick best model by accuracy (with min claims threshold)
        valid = {k: v for k, v in results.items() if v["claims"] >= len(test_cases) * 10}
        if valid:
            best = max(valid.items(), key=lambda x: (x[1]["accuracy"], -x[1]["errors"]))
            print(f"\n→ BEST MODEL: {best[0]} (accuracy={best[1]['accuracy']:.1f}%, {best[1]['claims']} claims)")
            return best[0]

    return None


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def evaluate_row_v2(row: dict, model: str, use_llm_verify: bool = True) -> dict:
    """Evaluate a single row using hybrid approach."""
    row_id = row.get("id", "unknown")
    prompt = row.get("prompt", "")
    response = row.get("response", "")

    # Step 1: Parse prompt (regex — works perfectly)
    ground_truth = parse_prompt(prompt)
    symbol = ground_truth.get("company", "???")
    years = ground_truth.get("years", [])

    # Step 2: Parse response JSON
    response_json = extract_response_json(response)
    if response_json is None:
        return {
            "id": row_id, "symbol": symbol, "parse_error": True,
            "error_msg": "Failed to parse response JSON",
            "total_claims": 0, "verified_correct": 0, "errors": 0,
            "warnings": 0, "unverifiable": 0, "accuracy_pct": None,
            "error_details": [],
        }

    # Step 3: Get full text
    text_parts = flatten_json_texts(response_json)
    full_text = "\n".join(text for _, text in text_parts)

    # Step 4: LLM claim extraction (the key improvement)
    claims = extract_claims_llm(full_text, model)
    time.sleep(RATE_LIMIT_DELAY)

    if not claims:
        # Fallback to regex
        from evaluator import extract_claims
        claims = extract_claims(response_json)

    # Step 5: Deterministic verification
    verified = verify_claims(claims, ground_truth)

    # Step 6: LLM verification for unverifiable claims
    if use_llm_verify:
        skip_metrics = {"unknown", "scenario_projection", "quarterly_figure",
                        "segment_revenue", "other", "change_pct", "unknown_cagr"}
        unverified = [v for v in verified
                      if v["verdict"] == "unverifiable"
                      and v.get("metric") not in skip_metrics]
        # Verify up to 20 per row
        for v in unverified[:20]:
            result = verify_claim_llm(v, ground_truth, model)
            time.sleep(RATE_LIMIT_DELAY * 0.3)
            if result and result.get("verdict") != "unverifiable":
                v["verdict"] = result["verdict"]
                v["expected"] = result.get("expected")
                v["source"] = result.get("source", "llm_verified")
                v["deviation_pct"] = result.get("deviation_pct")

    # Step 7: Aggregate
    correct = sum(1 for v in verified if v["verdict"] == "correct")
    errors = sum(1 for v in verified if v["verdict"] == "error")
    warnings = sum(1 for v in verified if v["verdict"] == "warning")
    unverifiable = sum(1 for v in verified if v["verdict"] == "unverifiable")
    verifiable = correct + errors + warnings
    accuracy = (correct / verifiable * 100) if verifiable > 0 else None

    return {
        "id": row_id, "symbol": symbol, "parse_error": False,
        "total_claims": len(verified),
        "verified_correct": correct, "errors": errors,
        "warnings": warnings, "unverifiable": unverifiable,
        "accuracy_pct": round(accuracy, 1) if accuracy is not None else None,
        "error_details": verified,
        "ground_truth_summary": {
            "years": years,
            "has_income_stmt": bool(ground_truth.get("income_statement")),
            "has_balance_sheet": bool(ground_truth.get("balance_sheet")),
            "has_cash_flow": bool(ground_truth.get("cash_flow")),
            "has_ratios": bool(ground_truth.get("ratios")),
        },
    }


def run_evaluation_v2(sample_file: str, sample_size: int, model: str,
                      use_llm_verify: bool = True) -> dict:
    """Run hybrid evaluation on a sample."""
    with open(sample_file) as f:
        rows = json.load(f)[:sample_size]
    print(f"Evaluating {len(rows)} rows with model={model} (llm_verify={use_llm_verify})...\n")

    results = []
    for i, row in enumerate(rows):
        print(f"  [{i+1}/{len(rows)}] {row.get('id', '?')[:8]}...", end=" ", flush=True)
        result = evaluate_row_v2(row, model, use_llm_verify)
        print(f"{result['symbol']} — {result['total_claims']} claims, "
              f"{result['verified_correct']}✓ {result['errors']}✗ "
              f"{result['unverifiable']}? acc={result.get('accuracy_pct', '?')}%")
        results.append(result)

    # Aggregate
    total_claims = sum(r["total_claims"] for r in results)
    total_correct = sum(r["verified_correct"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    total_warnings = sum(r["warnings"] for r in results)
    total_unverifiable = sum(r["unverifiable"] for r in results)
    parse_errors = sum(1 for r in results if r.get("parse_error"))
    verifiable = total_correct + total_errors + total_warnings
    overall_accuracy = (total_correct / verifiable * 100) if verifiable > 0 else None

    hc_errors = sum(1 for r in results for d in r["error_details"]
                    if d["verdict"] == "error" and (d.get("deviation_pct") or 100) <= 20)
    lc_errors = sum(1 for r in results for d in r["error_details"]
                    if d["verdict"] == "error" and (d.get("deviation_pct") or 0) > 50)
    hc_acc = ((total_correct + lc_errors) / verifiable * 100) if verifiable > 0 else None

    summary = {
        "timestamp": datetime.datetime.now().isoformat(),
        "version": "v2_hybrid",
        "model": model,
        "sample_size": len(rows),
        "total_claims": total_claims,
        "total_correct": total_correct,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "total_unverifiable": total_unverifiable,
        "parse_errors": parse_errors,
        "overall_accuracy_pct": round(overall_accuracy, 1) if overall_accuracy else None,
        "high_confidence_accuracy_pct": round(hc_acc, 1) if hc_acc else None,
        "high_confidence_errors": hc_errors,
        "low_confidence_errors": lc_errors,
        "avg_claims_per_response": round(total_claims / len(rows), 1) if rows else 0,
        "verification_rate_pct": round(verifiable / total_claims * 100, 1) if total_claims > 0 else 0,
        "results": results,
    }
    return summary


def print_summary(s: dict):
    print("\n" + "=" * 60)
    print(f"EVALUATION SUMMARY (v2 hybrid — {s.get('model', '?')})")
    print("=" * 60)
    for k in ["sample_size", "total_claims", "avg_claims_per_response",
              "verification_rate_pct", "overall_accuracy_pct",
              "high_confidence_accuracy_pct", "high_confidence_errors",
              "low_confidence_errors", "total_errors", "total_unverifiable", "parse_errors"]:
        v = s.get(k, "?")
        if "pct" in k and v != "?":
            v = f"{v}%"
        print(f"  {k}: {v}")
    print("=" * 60)


def save_run(summary: dict, sample_size: int):
    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = RUNS_DIR / f"{ts}_v2_n{sample_size}.json"
    with open(run_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nRun saved: {run_file}")
    return run_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Financial Analysis Accuracy Evaluator v2 (Hybrid)")
    parser.add_argument("--sample-file", required=True)
    parser.add_argument("--sample-size", type=int, default=30)
    parser.add_argument("--model", type=str, default=None, help="OpenRouter model ID")
    parser.add_argument("--compare-models", action="store_true", help="Benchmark models first")
    parser.add_argument("--no-llm-verify", action="store_true")
    args = parser.parse_args()

    init_api()

    if args.compare_models:
        best = benchmark_models(args.sample_file, n_rows=3)
        if best and not args.model:
            args.model = best
        if not args.model:
            print("No model selected, exiting")
            sys.exit(1)

    model = args.model or "google/gemini-2.0-flash-001"

    summary = run_evaluation_v2(args.sample_file, args.sample_size, model,
                                 use_llm_verify=not args.no_llm_verify)
    print_summary(summary)
    save_run(summary, args.sample_size)
