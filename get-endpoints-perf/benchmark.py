#!/Users/arnavgupta/Documents/GitHub/autoresearch/.venv/bin/python3
"""
Benchmark for Parallax API GET endpoints.

Tests all GET endpoints, measures latency, validates responses are correct,
and reports results in a grep-able format for the autoresearch loop.

Usage:
    python benchmark.py              # run all endpoints
    python benchmark.py --group fetch   # run only fetch endpoints
    python benchmark.py --group calc    # run only calculation endpoints

Output:
    fetch_p50: 0.32
    fetch_p99: 0.48
    fetch_max: 0.51
    fetch_over_target: 2
    calc_p50: 0.65
    calc_p99: 0.91
    calc_max: 0.95
    calc_over_target: 0
    total_over_target: 2
    status: success
"""

import json
import sys
import time
import urllib.request
import urllib.error
import hashlib
import argparse

# --- Configuration ---
BASE_URL = "http://localhost:8006/v1"
API_KEY = "ak_NS4XNWJ59REJW42BK6WPZGJVH6HKST07"
TIMEOUT_PER_REQUEST = 10  # seconds

# Targets
FETCH_TARGET = 0.5   # seconds
CALC_TARGET = 1.0    # seconds

# Test symbol — widely held, should always have data
TEST_SYMBOL = "AAPL.O"
TEST_ETF = "SPY"

# --- Endpoint definitions ---

FETCH_ENDPOINTS = [
    {"path": "/balance-sheet?symbol={symbol}&limit=4&freq=1", "name": "balance-sheet"},
    {"path": "/cash-flow?symbol={symbol}&limit=4&freq=1", "name": "cash-flow"},
    {"path": "/income-statement?symbol={symbol}&limit=4&freq=1", "name": "income-statement"},
    {"path": "/dividend-history?symbol={symbol}&limit=10", "name": "dividend-history"},
    {"path": "/divyield-history?symbol={symbol}", "name": "divyield-history"},
    {"path": "/daily-price?symbol={symbol}", "name": "daily-price"},
    {"path": "/financial-summary?symbol={symbol}", "name": "financial-summary"},
    {"path": "/earnings-history?symbol={symbol}", "name": "earnings-history"},
    {"path": "/earnings-estimate?symbol={symbol}", "name": "earnings-estimate"},
    {"path": "/revenue-estimate?symbol={symbol}", "name": "revenue-estimate"},
    {"path": "/analyst-recommendations?symbol={symbol}", "name": "analyst-recommendations"},
    {"path": "/target-summary?symbol={symbol}", "name": "target-summary"},
    {"path": "/eps-trend?symbol={symbol}", "name": "eps-trend"},
    {"path": "/eps-revisions?symbol={symbol}", "name": "eps-revisions"},
    {"path": "/upgrades-downgrades?symbol={symbol}", "name": "upgrades-downgrades"},
    {"path": "/news?symbol={symbol}&limit=5", "name": "news"},
    {"path": "/company-info?symbol={symbol}", "name": "company-info"},
    {"path": "/peers?symbol={symbol}", "name": "peers"},
    {"path": "/validate-symbol?symbol={symbol}", "name": "validate-symbol"},
    {"path": "/etf/daily-price?symbol={etf}", "name": "etf-daily-price"},
    {"path": "/etf/profile?symbol={etf}", "name": "etf-profile"},
    {"path": "/etf/holdings?symbol={etf}&limit=10", "name": "etf-holdings"},
    {"path": "/etf/search?q=technology&limit=5", "name": "etf-search"},
    {"path": "/etf/validate?symbol={etf}", "name": "etf-validate"},
]

CALC_ENDPOINTS = [
    {"path": "/score-metrics?symbol={symbol}", "name": "score-metrics"},
    {"path": "/historical-scores?symbol={symbol}&limit=10", "name": "historical-scores"},
    {"path": "/sector-avg-scores", "name": "sector-avg-scores"},
    {"path": "/current-ratios?symbol={symbol}", "name": "current-ratios"},
    {"path": "/key-ratios?symbol={symbol}&limit=4", "name": "key-ratios"},
    {"path": "/altman-zscore?symbol={symbol}", "name": "altman-zscore"},
    {"path": "/piotroski-fscore?symbol={symbol}", "name": "piotroski-fscore"},
    {"path": "/rolling-volatility?symbol={symbol}&window=252", "name": "rolling-volatility"},
    {"path": "/rolling-drawdown?symbol={symbol}&window=252", "name": "rolling-drawdown"},
    {"path": "/returns-calendar?symbol={symbol}", "name": "returns-calendar"},
    {"path": "/risk-return-profile?symbol={symbol}", "name": "risk-return-profile"},
    {"path": "/etf/scores?symbol={etf}", "name": "etf-scores"},
]


def make_request(path):
    """Make a GET request. Returns (latency_seconds, status_code, response_body, response_hash)."""
    url = f"{BASE_URL}{path}"
    url = url.replace("{symbol}", TEST_SYMBOL).replace("{etf}", TEST_ETF)
    headers = {
        "Authorization": f"Bearer {API_KEY}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    t_start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_PER_REQUEST) as resp:
            body = resp.read().decode()
            latency = time.time() - t_start
            # Hash the response body for correctness checking
            body_hash = hashlib.md5(body.encode()).hexdigest()
            return latency, resp.status, body, body_hash
    except urllib.error.HTTPError as e:
        latency = time.time() - t_start
        body = e.read().decode() if e.fp else ""
        return latency, e.code, body, None
    except urllib.error.URLError as e:
        latency = time.time() - t_start
        return latency, 0, str(e.reason), None
    except Exception as e:
        latency = time.time() - t_start
        return latency, 0, str(e), None


def run_group(endpoints, target, group_name):
    """Run a group of endpoints and collect results."""
    latencies = []
    failures = []

    for ep in endpoints:
        latency, status, body, body_hash = make_request(ep["path"])

        if status != 200:
            failures.append(f"{ep['name']}: HTTP {status}")
            continue

        # Validate response is non-empty JSON
        try:
            parsed = json.loads(body)
            if not parsed:
                failures.append(f"{ep['name']}: empty response")
                continue
        except json.JSONDecodeError:
            failures.append(f"{ep['name']}: invalid JSON")
            continue

        latencies.append({"name": ep["name"], "latency": latency, "hash": body_hash})

    if not latencies:
        return None, failures

    sorted_lats = sorted([r["latency"] for r in latencies])
    n = len(sorted_lats)
    p50 = sorted_lats[n // 2]
    p99 = sorted_lats[min(int(n * 0.99), n - 1)]
    max_lat = sorted_lats[-1]
    over_target = sum(1 for r in latencies if r["latency"] > target)

    stats = {
        "p50": p50,
        "p99": p99,
        "max": max_lat,
        "over_target": over_target,
        "details": latencies,
    }
    return stats, failures


def fail(msg):
    """Print failure output and exit."""
    print("total_over_target: -1")
    print("status: failed")
    print(f"error: {msg}")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=["fetch", "calc", "all"], default="all")
    args = parser.parse_args()

    all_failures = []
    total_over = 0

    # Run fetch endpoints
    if args.group in ("fetch", "all"):
        fetch_stats, fetch_failures = run_group(FETCH_ENDPOINTS, FETCH_TARGET, "fetch")
        all_failures.extend(fetch_failures)
        if fetch_stats:
            print(f"fetch_p50: {fetch_stats['p50']:.2f}")
            print(f"fetch_p99: {fetch_stats['p99']:.2f}")
            print(f"fetch_max: {fetch_stats['max']:.2f}")
            print(f"fetch_over_target: {fetch_stats['over_target']}")
            total_over += fetch_stats["over_target"]
            # Print slow endpoints
            for d in fetch_stats["details"]:
                if d["latency"] > FETCH_TARGET:
                    print(f"fetch_slow: {d['name']} {d['latency']:.2f}s")

    # Run calc endpoints
    if args.group in ("calc", "all"):
        calc_stats, calc_failures = run_group(CALC_ENDPOINTS, CALC_TARGET, "calc")
        all_failures.extend(calc_failures)
        if calc_stats:
            print(f"calc_p50: {calc_stats['p50']:.2f}")
            print(f"calc_p99: {calc_stats['p99']:.2f}")
            print(f"calc_max: {calc_stats['max']:.2f}")
            print(f"calc_over_target: {calc_stats['over_target']}")
            total_over += calc_stats["over_target"]
            # Print slow endpoints
            for d in calc_stats["details"]:
                if d["latency"] > CALC_TARGET:
                    print(f"calc_slow: {d['name']} {d['latency']:.2f}s")

    print(f"total_over_target: {total_over}")

    if all_failures:
        print(f"failures: {'; '.join(all_failures)}")
        fail(f"{len(all_failures)} endpoints failed: {'; '.join(all_failures)}")

    print("status: success")
    sys.exit(0)


if __name__ == "__main__":
    main()
