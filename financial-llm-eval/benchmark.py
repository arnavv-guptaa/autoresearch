#!/usr/bin/env python3
"""
Financial Analysis LLM Benchmark

Tests LLM models on real financial analysis tasks from production prompts.
Sends the same prompts used in parallax-api to different models via OpenRouter,
then evaluates numerical accuracy using the accuracy evaluator from financial-accuracy-eval.

Usage:
    python benchmark.py --model anthropic/claude-sonnet-4 --verbose
    python benchmark.py --model openai/gpt-4o --samples 10 --verbose
    python benchmark.py --model google/gemini-2.5-flash --temperature 0.3 --tag temp03
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
SAMPLE_DATA_PATH = SCRIPT_DIR / "sample_data.json"
RUNS_DIR = SCRIPT_DIR / "runs"
RESULTS_TSV = SCRIPT_DIR / "results.tsv"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Import evaluator from financial-accuracy-eval
EVAL_DIR = SCRIPT_DIR.parent / "financial-accuracy-eval"
sys.path.insert(0, str(EVAL_DIR))
from evaluator import (
    parse_prompt,
    extract_response_json,
    extract_claims,
    verify_claims,
)

RUNS_DIR.mkdir(exist_ok=True)

# Production system prompt (same as parallax-api)
SYSTEM_PROMPT = (
    "You are a senior financial analyst at Goldman Sachs providing comprehensive "
    "investment analysis using Krishna Palepu's framework. Analyze company data and "
    "provide a detailed assessment. Do not fabricate numbers, using wrong numbers can "
    "lead to a catastrophic error and undermine credibility."
)


def get_api_key():
    """Load OpenRouter API key from env or parallax-api .env."""
    key = os.getenv("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = Path.home() / "Documents" / "GitHub" / "parallax-api" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    print("error: OPENROUTER_API_KEY not found", file=sys.stderr)
    sys.exit(1)


def load_samples(n: int | None = None) -> list[dict]:
    """Load sample prompts from sample_data.json."""
    if not SAMPLE_DATA_PATH.exists():
        print(f"error: {SAMPLE_DATA_PATH} not found", file=sys.stderr)
        sys.exit(1)
    with open(SAMPLE_DATA_PATH) as f:
        data = json.load(f)
    if n is not None:
        data = data[:n]
    return data


def call_model(api_key: str, model: str, system_prompt: str, user_prompt: str,
               temperature: float = 0.3, timeout: int = 300) -> dict:
    """Call a model via OpenRouter for financial analysis."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://autoresearch.local",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": 16000,
    }

    t_start = time.time()
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
        latency_ms = (time.time() - t_start) * 1000

        if resp.status_code != 200:
            return {
                "response": None,
                "latency_ms": latency_ms,
                "tokens_in": 0, "tokens_out": 0,
                "model_id": None, "finish_reason": None,
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
            }

        data = resp.json()
        if "error" in data:
            return {
                "response": None,
                "latency_ms": latency_ms,
                "tokens_in": 0, "tokens_out": 0,
                "model_id": None, "finish_reason": None,
                "error": f"API error: {data['error']}",
            }

        choice = data["choices"][0]
        content = choice["message"]["content"].strip()
        usage = data.get("usage", {})

        return {
            "response": content,
            "latency_ms": latency_ms,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "model_id": data.get("model", model),
            "finish_reason": choice.get("finish_reason"),
            "error": None,
        }
    except requests.Timeout:
        return {
            "response": None, "latency_ms": (time.time() - t_start) * 1000,
            "tokens_in": 0, "tokens_out": 0,
            "model_id": None, "finish_reason": "timeout",
            "error": "timeout (300s)",
        }
    except Exception as e:
        return {
            "response": None, "latency_ms": (time.time() - t_start) * 1000,
            "tokens_in": 0, "tokens_out": 0,
            "model_id": None, "finish_reason": "error",
            "error": str(e),
        }


def evaluate_response(prompt_text: str, response_text: str) -> dict:
    """Evaluate a model response using the accuracy evaluator.

    Returns dict with accuracy metrics and error details.
    """
    # Parse ground truth from prompt
    ground_truth = parse_prompt(prompt_text)
    symbol = ground_truth.get("company", "???")

    # Parse response JSON and extract claims
    response_json = extract_response_json(response_text)
    if response_json is None:
        return {
            "symbol": symbol,
            "parse_error": True,
            "total_claims": 0,
            "verified_correct": 0,
            "errors": 0,
            "warnings": 0,
            "unverifiable": 0,
            "hc_errors": 0,
            "lc_errors": 0,
            "accuracy_pct": None,
            "hc_accuracy_pct": None,
            "error_details": [],
        }

    claims = extract_claims(response_json)
    verified = verify_claims(claims, ground_truth)

    correct = sum(1 for v in verified if v["verdict"] == "correct")
    errors = sum(1 for v in verified if v["verdict"] == "error")
    warnings = sum(1 for v in verified if v["verdict"] == "warning")
    unverifiable = sum(1 for v in verified if v["verdict"] == "unverifiable")

    # Split errors into high-confidence (2-20% dev) and low-confidence (>50% dev)
    hc_errors = sum(1 for v in verified if v["verdict"] == "error"
                    and v.get("deviation_pct") is not None
                    and 2 < v["deviation_pct"] <= 50)
    lc_errors = sum(1 for v in verified if v["verdict"] == "error"
                    and v.get("deviation_pct") is not None
                    and v["deviation_pct"] > 50)

    verifiable = correct + errors + warnings
    accuracy = (correct / verifiable * 100) if verifiable > 0 else None
    hc_accuracy = ((correct + lc_errors) / verifiable * 100) if verifiable > 0 else None

    return {
        "symbol": symbol,
        "parse_error": False,
        "total_claims": len(verified),
        "verified_correct": correct,
        "errors": errors,
        "warnings": warnings,
        "unverifiable": unverifiable,
        "hc_errors": hc_errors,
        "lc_errors": lc_errors,
        "accuracy_pct": round(accuracy, 1) if accuracy is not None else None,
        "hc_accuracy_pct": round(hc_accuracy, 1) if hc_accuracy is not None else None,
        "error_details": [v for v in verified if v["verdict"] in ("error", "warning")],
    }


def check_response_quality(response_text: str) -> dict:
    """Basic structural quality checks on the response."""
    response_json = extract_response_json(response_text)
    if response_json is None:
        return {"valid_json": False, "has_executive_summary": False,
                "has_detailed_analysis": False, "response_length": len(response_text or "")}

    has_exec = "executiveSummary" in response_json
    has_detail = "detailedAnalysis" in response_json

    # Check for key subsections
    detail = response_json.get("detailedAnalysis", {})
    subsections = {
        "businessStrategy": "businessStrategy" in detail,
        "accountingQuality": "accountingQuality" in detail,
        "financialPerformance": "financialPerformance" in detail,
        "prospectiveAnalysis": "prospectiveAnalysis" in detail,
    }

    return {
        "valid_json": True,
        "has_executive_summary": has_exec,
        "has_detailed_analysis": has_detail,
        "subsections": subsections,
        "response_length": len(response_text or ""),
    }


def save_run(run_record: dict) -> Path:
    """Save detailed run JSON to runs/ directory."""
    ts = run_record["timestamp"].replace(":", "-").replace("+", "p")
    model_slug = run_record["model"].replace("/", "_").replace(".", "-")
    temp = run_record["temperature"]
    tag = run_record.get("prompt_tag", "default")
    filename = f"{ts}_{model_slug}_t{temp}_{tag}.json"
    filepath = RUNS_DIR / filename
    with open(filepath, "w") as f:
        json.dump(run_record, f, indent=2, default=str)
    return filepath


def append_results_tsv(run_record: dict):
    """Append summary row to results.tsv."""
    header_cols = [
        "timestamp", "model", "resolved_model", "samples", "prompt_tag", "temperature",
        "accuracy_pct", "hc_accuracy_pct", "total_claims", "verified_correct",
        "errors", "hc_errors", "lc_errors", "warnings", "unverifiable",
        "verification_rate_pct", "avg_claims_per_response",
        "parse_errors", "api_errors",
        "avg_latency_ms", "p50_latency_ms", "p95_latency_ms",
        "tokens_in", "tokens_out",
        "json_valid_pct", "schema_complete_pct",
        "status", "run_file",
    ]

    write_header = not RESULTS_TSV.exists() or RESULTS_TSV.stat().st_size == 0

    row = [
        run_record["timestamp"],
        run_record["model"],
        run_record.get("resolved_model", ""),
        str(run_record["samples"]),
        run_record.get("prompt_tag", "default"),
        str(run_record["temperature"]),
        f"{run_record['accuracy_pct']:.1f}" if run_record['accuracy_pct'] is not None else "n/a",
        f"{run_record['hc_accuracy_pct']:.1f}" if run_record.get('hc_accuracy_pct') is not None else "n/a",
        str(run_record["total_claims"]),
        str(run_record["verified_correct"]),
        str(run_record["errors"]),
        str(run_record.get("hc_errors", 0)),
        str(run_record.get("lc_errors", 0)),
        str(run_record["warnings"]),
        str(run_record["unverifiable"]),
        f"{run_record['verification_rate_pct']:.1f}" if run_record.get('verification_rate_pct') is not None else "n/a",
        f"{run_record['avg_claims_per_response']:.1f}" if run_record.get('avg_claims_per_response') is not None else "n/a",
        str(run_record.get("parse_errors", 0)),
        str(run_record.get("api_errors", 0)),
        f"{run_record['avg_latency_ms']:.0f}",
        f"{run_record.get('p50_latency_ms', 0):.0f}",
        f"{run_record.get('p95_latency_ms', 0):.0f}",
        str(run_record["total_tokens_in"]),
        str(run_record["total_tokens_out"]),
        f"{run_record.get('json_valid_pct', 0):.0f}",
        f"{run_record.get('schema_complete_pct', 0):.0f}",
        run_record["status"],
        run_record.get("run_file", ""),
    ]

    with open(RESULTS_TSV, "a") as f:
        if write_header:
            f.write("\t".join(header_cols) + "\n")
        f.write("\t".join(row) + "\n")


def run_benchmark(model: str, temperature: float = 0.3, verbose: bool = False,
                  samples: int | None = None, tag: str | None = None,
                  system_prompt: str | None = None):
    """Run the full financial analysis benchmark against a model."""
    api_key = get_api_key()
    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prompt_tag = tag or "default"
    active_system_prompt = system_prompt or SYSTEM_PROMPT

    sample_data = load_samples(samples)
    total = len(sample_data)

    print(f"Running financial analysis eval: {model} | {total} samples | temp={temperature} | tag={prompt_tag}", file=sys.stderr)
    print(f"{'─' * 70}", file=sys.stderr)

    # Aggregates
    all_results = []
    total_claims = 0
    total_correct = 0
    total_errors = 0
    total_warnings = 0
    total_unverifiable = 0
    total_hc_errors = 0
    total_lc_errors = 0
    parse_errors = 0
    api_errors = 0
    total_latency_ms = 0
    total_tokens_in = 0
    total_tokens_out = 0
    resolved_model = None
    json_valid_count = 0
    schema_complete_count = 0
    latencies = []

    for i, sample in enumerate(sample_data):
        prompt = sample["prompt"]
        sample_id = sample.get("id", f"sample_{i}")

        # Extract symbol from prompt
        sym_m = re.search(r'Symbol:\s*(\S+)', prompt)
        symbol = sym_m.group(1) if sym_m else f"sample_{i}"

        print(f"  [{i+1}/{total}] {symbol}: ", end="", flush=True, file=sys.stderr)

        # Call the model
        result = call_model(api_key, model, active_system_prompt, prompt, temperature)

        if result.get("model_id") and not resolved_model:
            resolved_model = result["model_id"]

        total_latency_ms += result["latency_ms"]
        latencies.append(result["latency_ms"])

        if result["error"]:
            api_errors += 1
            print(f"ERROR - {result['error'][:80]}", file=sys.stderr)
            all_results.append({
                "sample_id": sample_id,
                "symbol": symbol,
                "api_error": result["error"],
                "latency_ms": result["latency_ms"],
            })
            continue

        total_tokens_in += result["tokens_in"]
        total_tokens_out += result["tokens_out"]

        # Evaluate accuracy
        eval_result = evaluate_response(prompt, result["response"])

        # Check structural quality
        quality = check_response_quality(result["response"])
        if quality["valid_json"]:
            json_valid_count += 1
        if quality.get("has_executive_summary") and quality.get("has_detailed_analysis"):
            subsections = quality.get("subsections", {})
            if all(subsections.values()):
                schema_complete_count += 1

        # Aggregate
        total_claims += eval_result["total_claims"]
        total_correct += eval_result["verified_correct"]
        total_errors += eval_result["errors"]
        total_warnings += eval_result["warnings"]
        total_unverifiable += eval_result["unverifiable"]
        total_hc_errors += eval_result["hc_errors"]
        total_lc_errors += eval_result["lc_errors"]
        if eval_result["parse_error"]:
            parse_errors += 1

        # Print per-sample summary
        acc_str = f"{eval_result['accuracy_pct']:.1f}%" if eval_result['accuracy_pct'] is not None else "N/A"
        hc_str = f"{eval_result['hc_accuracy_pct']:.1f}%" if eval_result.get('hc_accuracy_pct') is not None else "N/A"
        claims_str = f"{eval_result['total_claims']} claims"
        err_str = f"{eval_result['errors']} errors ({eval_result['hc_errors']} HC)"

        if verbose:
            print(f"acc={acc_str} hc_acc={hc_str} | {claims_str} | {err_str} | {result['latency_ms']:.0f}ms | {result['tokens_out']} tok_out", file=sys.stderr)
        else:
            marker = "✓" if (eval_result.get('hc_accuracy_pct') or 0) >= 90 else "✗"
            print(f"{marker} acc={acc_str} hc_acc={hc_str} | {result['latency_ms']/1000:.1f}s", file=sys.stderr)

        all_results.append({
            "sample_id": sample_id,
            "symbol": symbol,
            "accuracy": eval_result,
            "quality": quality,
            "latency_ms": result["latency_ms"],
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "finish_reason": result["finish_reason"],
            "response_text": result["response"],
        })

    # Compute aggregates
    successful = total - api_errors
    verifiable = total_correct + total_errors + total_warnings
    accuracy_pct = (total_correct / verifiable * 100) if verifiable > 0 else None
    hc_accuracy_pct = ((total_correct + total_lc_errors) / verifiable * 100) if verifiable > 0 else None
    verification_rate = (verifiable / total_claims * 100) if total_claims > 0 else None
    avg_claims = (total_claims / successful) if successful > 0 else None
    avg_latency = (total_latency_ms / total) if total > 0 else 0
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
    json_valid_pct = (json_valid_count / successful * 100) if successful > 0 else 0
    schema_complete_pct = (schema_complete_count / successful * 100) if successful > 0 else 0
    status = "failed" if api_errors == total else "success"

    # Build run record
    run_record = {
        "timestamp": run_ts,
        "model": model,
        "resolved_model": resolved_model or model,
        "temperature": temperature,
        "prompt_tag": prompt_tag,
        "system_prompt": active_system_prompt,
        "samples": total,
        "accuracy_pct": accuracy_pct,
        "hc_accuracy_pct": hc_accuracy_pct,
        "total_claims": total_claims,
        "verified_correct": total_correct,
        "errors": total_errors,
        "hc_errors": total_hc_errors,
        "lc_errors": total_lc_errors,
        "warnings": total_warnings,
        "unverifiable": total_unverifiable,
        "verification_rate_pct": verification_rate,
        "avg_claims_per_response": avg_claims,
        "parse_errors": parse_errors,
        "api_errors": api_errors,
        "avg_latency_ms": avg_latency,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "json_valid_pct": json_valid_pct,
        "schema_complete_pct": schema_complete_pct,
        "status": status,
        "results": all_results,
    }

    # Save
    run_file = save_run(run_record)
    run_record["run_file"] = str(run_file.name)
    append_results_tsv(run_record)

    # Print grep-able stdout
    print(f"model: {model}")
    print(f"resolved_model: {resolved_model or model}")
    print(f"samples: {total}")
    print(f"accuracy_pct: {accuracy_pct:.1f}" if accuracy_pct is not None else "accuracy_pct: n/a")
    print(f"hc_accuracy_pct: {hc_accuracy_pct:.1f}" if hc_accuracy_pct is not None else "hc_accuracy_pct: n/a")
    print(f"total_claims: {total_claims}")
    print(f"verified_correct: {total_correct}")
    print(f"errors: {total_errors}")
    print(f"hc_errors: {total_hc_errors}")
    print(f"lc_errors: {total_lc_errors}")
    print(f"warnings: {total_warnings}")
    print(f"unverifiable: {total_unverifiable}")
    print(f"parse_errors: {parse_errors}")
    print(f"api_errors: {api_errors}")
    print(f"avg_latency_ms: {avg_latency:.0f}")
    print(f"tokens_in: {total_tokens_in}")
    print(f"tokens_out: {total_tokens_out}")
    print(f"json_valid_pct: {json_valid_pct:.0f}")
    print(f"schema_complete_pct: {schema_complete_pct:.0f}")
    print(f"status: {status}")

    # Stderr summary
    print(f"\n{'─' * 70}", file=sys.stderr)
    print(f"Model: {model}  (resolved: {resolved_model or model})", file=sys.stderr)
    print(f"Samples: {total} ({api_errors} API errors, {parse_errors} parse errors)", file=sys.stderr)
    print(f"Accuracy: {accuracy_pct:.1f}% (HC: {hc_accuracy_pct:.1f}%)" if accuracy_pct else "Accuracy: N/A", file=sys.stderr)
    print(f"Claims: {total_claims} total, {total_correct} correct, {total_errors} errors ({total_hc_errors} HC, {total_lc_errors} LC)", file=sys.stderr)
    print(f"Verification rate: {verification_rate:.1f}%" if verification_rate else "Verification rate: N/A", file=sys.stderr)
    print(f"Avg Latency: {avg_latency/1000:.1f}s  (P50: {p50/1000:.1f}s, P95: {p95/1000:.1f}s)", file=sys.stderr)
    print(f"Tokens: {total_tokens_in} in / {total_tokens_out} out", file=sys.stderr)
    print(f"JSON valid: {json_valid_pct:.0f}%  Schema complete: {schema_complete_pct:.0f}%", file=sys.stderr)
    print(f"Run saved: {run_file}", file=sys.stderr)
    print(f"Results appended: {RESULTS_TSV}", file=sys.stderr)

    return 0 if status == "success" else 1


def main():
    parser = argparse.ArgumentParser(description="Financial Analysis LLM Benchmark")
    parser.add_argument("--model", required=True, help="OpenRouter model identifier")
    parser.add_argument("--temperature", type=float, default=0.3, help="Sampling temperature (default: 0.3, same as production)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed per-sample results")
    parser.add_argument("--samples", "-n", type=int, default=None, help="Number of samples to test (default: all)")
    parser.add_argument("--tag", help="Tag for this run (default: 'default')")
    parser.add_argument("--system-prompt-file", help="Path to custom system prompt file")
    args = parser.parse_args()

    system_prompt = None
    if args.system_prompt_file and Path(args.system_prompt_file).exists():
        system_prompt = Path(args.system_prompt_file).read_text().strip()

    exit_code = run_benchmark(
        model=args.model,
        temperature=args.temperature,
        verbose=args.verbose,
        samples=args.samples,
        tag=args.tag,
        system_prompt=system_prompt,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
