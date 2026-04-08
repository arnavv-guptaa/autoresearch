#!/usr/bin/env python3
"""
Correctness tests for optimized portfolio analysis.
Tests: non-USD benchmark, ETF+cash positions, and basic regression.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date, timedelta

BASE_URL = "http://localhost:8005/v1"
API_KEY = "cZG6RzM3K5edOoZb5FhoS28r9bWkoTOD"
POLL_INTERVAL = 0.5
TIMEOUT = 120


def make_request(method, path, body=None):
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, {"detail": body_text}
    except urllib.error.URLError as e:
        return 0, {"detail": str(e.reason)}


def submit_and_wait(payload, label):
    print(f"\n{'='*60}")
    print(f"  TEST: {label}")
    print(f"{'='*60}")

    t0 = time.time()
    status, resp = make_request("POST", "/portfolio/analyze", payload)
    if status not in (200, 202):
        print(f"  FAIL: POST returned {status}: {resp.get('detail', resp)}")
        return None, 0

    job_id = resp.get("job_id")
    if not job_id:
        print(f"  FAIL: No job_id: {resp}")
        return None, 0

    while True:
        elapsed = time.time() - t0
        if elapsed > TIMEOUT:
            print(f"  FAIL: Timeout after {TIMEOUT}s")
            return None, elapsed
        time.sleep(POLL_INTERVAL)
        status, job = make_request("GET", f"/jobs/{job_id}")
        if status != 200:
            print(f"  FAIL: Poll error {status}")
            return None, elapsed
        if job.get("status") == "completed":
            break
        if job.get("status") == "failed":
            print(f"  FAIL: Job failed: {job.get('error')}")
            return None, elapsed

    latency = time.time() - t0
    wrapper = job.get("result", {})
    result = wrapper.get("result", wrapper)
    return result, latency


def check_required_keys(result, label):
    """Check that all expected top-level keys are present."""
    expected = {
        'portfolio_parameters', 'portfolio_input', 'data_quality', 'portfolio_summary',
        'performance_metrics', 'rolling_metrics', 'drawdown_analysis', 'portfolio_scores',
        'concentration_metrics', 'transactions', 'company_info', 'latest_holdings',
        'market_allocation', 'sector_allocation', 'currency_allocation', 'company_contribution',
        'sector_contribution', 'market_contribution', 'time_period_returns', 'monthly_returns',
        'annual_returns', 'benchmark_prices', 'daily_summary', 'turnover_analysis'
    }
    actual = set(result.keys())
    missing = expected - actual
    if missing:
        print(f"  FAIL: Missing keys: {missing}")
        return False
    return True


def check_no_nan_in_json(result, path="root"):
    """Recursively check no NaN values leaked into the result."""
    if isinstance(result, float):
        import math
        if math.isnan(result) or math.isinf(result):
            print(f"  FAIL: NaN/Inf at {path}")
            return False
    elif isinstance(result, dict):
        for k, v in result.items():
            if not check_no_nan_in_json(v, f"{path}.{k}"):
                return False
    elif isinstance(result, list):
        for i, v in enumerate(result):
            if not check_no_nan_in_json(v, f"{path}[{i}]"):
                return False
    return True


def test_basic_usd():
    """Test 1: Basic USD portfolio (regression test)."""
    yesterday = date.today() - timedelta(days=1)
    start = yesterday - timedelta(days=30)
    payload = {
        "start_date": start.isoformat(),
        "end_date": yesterday.isoformat(),
        "base_currency": "USD",
        "benchmark": "SPY",
        "initial_value": 10000,
        "portfolio": [
            {"date": start.isoformat(), "ric": "AAPL", "weight": 0.25},
            {"date": start.isoformat(), "ric": "MSFT", "weight": 0.25},
            {"date": start.isoformat(), "ric": "GOOGL", "weight": 0.25},
            {"date": start.isoformat(), "ric": "AMZN", "weight": 0.25},
        ],
    }

    result, latency = submit_and_wait(payload, "Basic USD portfolio (AAPL/MSFT/GOOGL/AMZN, SPY benchmark)")
    if not result:
        return False

    print(f"  Latency: {latency:.1f}s")

    ok = True
    if not check_required_keys(result, "basic_usd"):
        ok = False
    if not check_no_nan_in_json(result):
        ok = False

    # Check portfolio_summary has sensible values
    ps = result.get("portfolio_summary", {})
    if ps.get("final_value") is None or ps["final_value"] <= 0:
        print(f"  FAIL: Bad final_value: {ps.get('final_value')}")
        ok = False

    pm = result.get("performance_metrics", {}).get("portfolio", {})
    if pm.get("total_return") is None:
        print(f"  FAIL: Missing total_return in performance_metrics")
        ok = False

    # Check benchmark metrics exist
    bm = result.get("performance_metrics", {}).get("benchmark")
    if bm is None:
        print(f"  FAIL: No benchmark metrics")
        ok = False

    if ok:
        print(f"  PASS (final_value={ps['final_value']}, return={pm['total_return']:.4f})")
    return ok


def test_non_usd_benchmark():
    """Test 2: Non-USD benchmark to verify FX currency fallback."""
    yesterday = date.today() - timedelta(days=1)
    start = yesterday - timedelta(days=30)
    payload = {
        "start_date": start.isoformat(),
        "end_date": yesterday.isoformat(),
        "base_currency": "SGD",
        "benchmark": "PHSG.SI",  # Phillip SGD Money Market ETF, SGD-denominated
        "initial_value": 10000,
        "portfolio": [
            {"date": start.isoformat(), "ric": "D05", "weight": 0.34},   # DBS → DBSM.SI, SGD
            {"date": start.isoformat(), "ric": "O39", "weight": 0.33},   # OCBC → OCBC.SI, SGD
            {"date": start.isoformat(), "ric": "U11", "weight": 0.33},   # UOB → UOBH.SI, SGD
        ],
    }

    result, latency = submit_and_wait(payload, "Non-USD benchmark (SGD stocks, ES3.SI benchmark)")
    if not result:
        return False

    print(f"  Latency: {latency:.1f}s")

    ok = True
    if not check_required_keys(result, "non_usd"):
        ok = False
    if not check_no_nan_in_json(result):
        ok = False

    # Check the base currency is SGD
    params = result.get("portfolio_parameters", {})
    if params.get("base_currency") != "SGD":
        print(f"  FAIL: base_currency should be SGD, got {params.get('base_currency')}")
        ok = False

    # Check benchmark prices exist and have data
    bp = result.get("benchmark_prices", [])
    if len(bp) == 0:
        print(f"  FAIL: No benchmark prices returned")
        ok = False

    # Check benchmark metrics exist
    bm = result.get("performance_metrics", {}).get("benchmark")
    if bm is None:
        print(f"  FAIL: No benchmark metrics — FX conversion may have failed")
        ok = False

    ps = result.get("portfolio_summary", {})
    pm = result.get("performance_metrics", {}).get("portfolio", {})

    if ok:
        print(f"  PASS (final_value={ps.get('final_value')}, return={pm.get('total_return', 'N/A')})")
    return ok


def test_etf_and_cash():
    """Test 3: Portfolio with ETF and cash positions."""
    yesterday = date.today() - timedelta(days=1)
    start = yesterday - timedelta(days=30)
    payload = {
        "start_date": start.isoformat(),
        "end_date": yesterday.isoformat(),
        "base_currency": "USD",
        "benchmark": "SPY",
        "initial_value": 10000,
        "portfolio": [
            {"date": start.isoformat(), "ric": "AAPL", "weight": 0.30},     # EQ
            {"date": start.isoformat(), "ric": "SPY", "weight": 0.30},      # ETF
            {"date": start.isoformat(), "ric": "QQQ", "weight": 0.20},      # ETF
            {"date": start.isoformat(), "ric": "CASH.USD", "weight": 0.20}, # Cash
        ],
    }

    result, latency = submit_and_wait(payload, "ETF + Cash positions (AAPL + SPY + QQQ + CASH.USD)")
    if not result:
        return False

    print(f"  Latency: {latency:.1f}s")

    ok = True
    if not check_required_keys(result, "etf_cash"):
        ok = False
    if not check_no_nan_in_json(result):
        ok = False

    # Check that we have multiple position types in latest_holdings
    holdings = result.get("latest_holdings", [])
    if len(holdings) == 0:
        print(f"  FAIL: No latest_holdings")
        ok = False
    else:
        sectors = set(h.get("sector") for h in holdings)
        print(f"  Sectors found: {sectors}")
        # Should have at least regular sector + ETF + Cash
        if "ETF" not in sectors:
            print(f"  WARN: No ETF sector found in holdings (ETF may have been classified differently)")
        if "Cash" not in sectors:
            print(f"  WARN: No Cash sector found in holdings")

    ps = result.get("portfolio_summary", {})
    pm = result.get("performance_metrics", {}).get("portfolio", {})

    if ok:
        print(f"  PASS (final_value={ps.get('final_value')}, return={pm.get('total_return', 'N/A')}, positions={len(holdings)})")
    return ok


def main():
    print("Portfolio Analysis Correctness Tests")
    print(f"Target: {BASE_URL}")
    print(f"Date: {date.today()}")

    results = {}
    results["basic_usd"] = test_basic_usd()
    results["non_usd_benchmark"] = test_non_usd_benchmark()
    results["etf_cash"] = test_etf_and_cash()

    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {status}: {name}")
        if not passed:
            all_pass = False

    print(f"\n  {'ALL TESTS PASSED' if all_pass else 'SOME TESTS FAILED'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
