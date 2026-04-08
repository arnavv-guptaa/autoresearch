#!/usr/bin/env python3
"""
Compare production vs local API responses for correctness validation.
Sends identical requests to both, diffs the full result structure.
"""

import json
import math
import sys
import time
import urllib.request
import urllib.error
from datetime import date, timedelta

PROD_URL = "https://api.chicago.global/v1"
PROD_KEY = "ak_4PQW1GDSVQAF4C179HGNV39C0R0H5FJ0"

LOCAL_URL = "http://localhost:8005/v1"
LOCAL_KEY = "cZG6RzM3K5edOoZb5FhoS28r9bWkoTOD"

POLL_INTERVAL = 2
TIMEOUT = 180


def make_request(base_url, api_key, method, path, body=None):
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
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


def submit_and_poll(base_url, api_key, payload, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    t0 = time.time()
    status, resp = make_request(base_url, api_key, "POST", "/portfolio/analyze", payload)
    if status not in (200, 202):
        print(f"  POST failed: {status} - {resp.get('detail', resp)}")
        return None, 0

    job_id = resp.get("job_id")
    if not job_id:
        print(f"  No job_id: {resp}")
        return None, 0

    print(f"  Job ID: {job_id}")

    while True:
        elapsed = time.time() - t0
        if elapsed > TIMEOUT:
            print(f"  TIMEOUT after {TIMEOUT}s")
            return None, elapsed
        time.sleep(POLL_INTERVAL)
        status, job = make_request(base_url, api_key, "GET", f"/jobs/{job_id}")
        if status != 200:
            print(f"  Poll error: {status}")
            return None, elapsed
        if job.get("status") == "completed":
            break
        if job.get("status") == "failed":
            print(f"  Job failed: {job.get('error')}")
            return None, elapsed

    latency = time.time() - t0
    print(f"  Completed in {latency:.1f}s")

    wrapper = job.get("result", {})
    result = wrapper.get("result", wrapper)
    return result, latency


def compare_values(prod_val, local_val, path, diffs, tolerance=1e-4):
    """Recursively compare two values, collecting differences."""
    if prod_val is None and local_val is None:
        return
    if type(prod_val) != type(local_val):
        # Allow None vs missing
        if prod_val is None or local_val is None:
            diffs.append((path, f"type mismatch: {type(prod_val).__name__} vs {type(local_val).__name__}", prod_val, local_val))
        else:
            diffs.append((path, f"type mismatch: {type(prod_val).__name__} vs {type(local_val).__name__}", prod_val, local_val))
        return

    if isinstance(prod_val, dict):
        all_keys = set(list(prod_val.keys()) + list(local_val.keys()))
        for k in sorted(all_keys):
            if k not in prod_val:
                diffs.append((f"{path}.{k}", "missing in PROD", None, local_val[k]))
            elif k not in local_val:
                diffs.append((f"{path}.{k}", "missing in LOCAL", prod_val[k], None))
            else:
                compare_values(prod_val[k], local_val[k], f"{path}.{k}", diffs, tolerance)
    elif isinstance(prod_val, list):
        if len(prod_val) != len(local_val):
            diffs.append((path, f"list length: {len(prod_val)} vs {len(local_val)}", None, None))
            # Compare up to the shorter length
        for i in range(min(len(prod_val), len(local_val))):
            compare_values(prod_val[i], local_val[i], f"{path}[{i}]", diffs, tolerance)
    elif isinstance(prod_val, float):
        if math.isnan(prod_val) and math.isnan(local_val):
            return
        if abs(prod_val - local_val) > tolerance and abs(prod_val - local_val) > tolerance * max(abs(prod_val), abs(local_val), 1):
            diffs.append((path, f"float diff", prod_val, local_val))
    elif isinstance(prod_val, (int, bool)):
        if prod_val != local_val:
            diffs.append((path, "value diff", prod_val, local_val))
    elif isinstance(prod_val, str):
        if prod_val != local_val:
            diffs.append((path, "string diff", prod_val, local_val))


def main():
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

    print(f"Payload: {start} to {yesterday}, 4 stocks, USD, SPY benchmark")

    # Hit both
    prod_result, prod_latency = submit_and_poll(PROD_URL, PROD_KEY, payload, "PRODUCTION")
    local_result, local_latency = submit_and_poll(LOCAL_URL, LOCAL_KEY, payload, "LOCAL (optimized)")

    if not prod_result or not local_result:
        print("\nOne or both requests failed. Cannot compare.")
        sys.exit(1)

    # Compare top-level keys
    prod_keys = set(prod_result.keys())
    local_keys = set(local_result.keys())

    print(f"\n{'='*60}")
    print(f"  COMPARISON")
    print(f"{'='*60}")
    print(f"  Prod latency:  {prod_latency:.1f}s")
    print(f"  Local latency: {local_latency:.1f}s")
    print(f"  Speedup:       {prod_latency/local_latency:.1f}x" if local_latency > 0 else "")

    print(f"\n  Prod keys:  {sorted(prod_keys)}")
    print(f"  Local keys: {sorted(local_keys)}")

    only_prod = prod_keys - local_keys
    only_local = local_keys - prod_keys
    if only_prod:
        print(f"\n  Keys only in PROD:  {only_prod}")
    if only_local:
        print(f"\n  Keys only in LOCAL: {only_local}")

    # Normalize list ordering before comparison — DB queries may return rows in different order
    def sort_records(records, sort_key=None):
        """Sort a list of dicts by a key if present, to normalize ordering."""
        if not isinstance(records, list) or not records:
            return records
        if isinstance(records[0], dict):
            # Try common sort keys
            for k in (sort_key, 'ric', 'date', 'period', 'year', 'sector', 'market', 'currency'):
                if k and all(k in r for r in records):
                    return sorted(records, key=lambda r: str(r[k]))
        return records

    for key in sorted(prod_keys & local_keys):
        if isinstance(prod_result.get(key), list):
            prod_result[key] = sort_records(prod_result[key])
        if isinstance(local_result.get(key), list):
            local_result[key] = sort_records(local_result[key])

    # Deep comparison on shared keys
    shared_keys = prod_keys & local_keys
    diffs = []
    for key in sorted(shared_keys):
        compare_values(prod_result[key], local_result[key], key, diffs)

    if not diffs:
        print(f"\n  ALL {len(shared_keys)} SHARED SECTIONS MATCH")
    else:
        print(f"\n  DIFFERENCES FOUND: {len(diffs)}")
        # Group by top-level section
        sections = {}
        for path, desc, pv, lv in diffs:
            section = path.split(".")[0].split("[")[0]
            if section not in sections:
                sections[section] = []
            sections[section].append((path, desc, pv, lv))

        for section, section_diffs in sorted(sections.items()):
            print(f"\n  --- {section} ({len(section_diffs)} diffs) ---")
            for path, desc, pv, lv in section_diffs[:10]:  # Show first 10 per section
                pv_str = f"{pv}" if not isinstance(pv, float) else f"{pv:.6f}"
                lv_str = f"{lv}" if not isinstance(lv, float) else f"{lv:.6f}"
                if len(pv_str) > 60:
                    pv_str = pv_str[:60] + "..."
                if len(lv_str) > 60:
                    lv_str = lv_str[:60] + "..."
                print(f"    {path}: {desc}")
                print(f"      PROD:  {pv_str}")
                print(f"      LOCAL: {lv_str}")
            if len(section_diffs) > 10:
                print(f"    ... and {len(section_diffs) - 10} more")

    # Save full results for manual inspection
    with open("/Users/arnavgupta/Documents/GitHub/autoresearch/api-perf/compare_prod.json", "w") as f:
        json.dump(prod_result, f, indent=2, default=str)
    with open("/Users/arnavgupta/Documents/GitHub/autoresearch/api-perf/compare_local.json", "w") as f:
        json.dump(local_result, f, indent=2, default=str)
    print(f"\n  Full results saved to compare_prod.json and compare_local.json")

    sys.exit(0 if not diffs else 1)


if __name__ == "__main__":
    main()
