#!/Users/arnavgupta/Documents/GitHub/autoresearch/.venv/bin/python3
"""
Benchmark for the Parallax API portfolio analyze endpoint.

Sends a portfolio analysis request, polls for completion, validates the response,
and prints latency in a grep-able format for the autoresearch loop.

Usage:
    python benchmark.py

Output:
    latency_seconds: 65.2
    status: success

    or on failure:
    latency_seconds: 0.0
    status: failed
    error: <reason>
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date, timedelta

# --- Configuration ---
BASE_URL = "http://localhost:8005/v1"
API_KEY = "cZG6RzM3K5edOoZb5FhoS28r9bWkoTOD"
POLL_INTERVAL = 0.5  # seconds between status checks
TIMEOUT = 180  # max seconds before giving up

# Expected keys in a successful analysis result
REQUIRED_KEYS = {"performance_metrics", "portfolio_summary"}


def make_request(method, path, body=None):
    """Make an HTTP request to the API. Returns (status_code, parsed_json)."""
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


def build_payload():
    """Build a portfolio analysis request with dynamic dates."""
    yesterday = date.today() - timedelta(days=1)
    start = yesterday - timedelta(days=30)
    return {
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


def fail(msg):
    """Print failure output and exit."""
    print(f"latency_seconds: 0.0")
    print(f"status: failed")
    print(f"error: {msg}")
    sys.exit(1)


def main():
    # 1. Submit the analysis job
    payload = build_payload()
    t_start = time.time()

    status, resp = make_request("POST", "/portfolio/analyze", payload)
    if status != 202 and status != 200:
        fail(f"POST returned {status}: {resp.get('detail', resp)}")

    job_id = resp.get("job_id")
    if not job_id:
        fail(f"No job_id in response: {resp}")

    # 2. Poll for completion
    while True:
        elapsed = time.time() - t_start
        if elapsed > TIMEOUT:
            fail(f"Timed out after {TIMEOUT}s waiting for job {job_id}")

        time.sleep(POLL_INTERVAL)

        status, job = make_request("GET", f"/jobs/{job_id}")
        if status != 200:
            fail(f"GET /jobs/{job_id} returned {status}: {job.get('detail', job)}")

        job_status = job.get("status")
        if job_status == "completed":
            break
        elif job_status == "failed":
            error_msg = job.get("error", "unknown error")
            fail(f"Job failed: {error_msg}")
        # else: pending/processing — keep polling

    t_end = time.time()
    latency = t_end - t_start

    # 3. Validate the response
    # Worker wraps result as {"success": True, "result": {actual data}}
    wrapper = job.get("result", {})
    result = wrapper.get("result", wrapper)
    missing = REQUIRED_KEYS - set(result.keys())
    if missing:
        fail(f"Response missing keys: {missing}")

    # 4. Report success
    print(f"latency_seconds: {latency:.1f}")
    print(f"status: success")
    sys.exit(0)


if __name__ == "__main__":
    main()
