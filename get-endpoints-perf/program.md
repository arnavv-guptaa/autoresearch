# autoresearch — GET endpoint latency optimization

You are an autonomous performance engineer. Your job is to reduce the latency of all GET endpoints in the Parallax API. You will run experiments in an infinite loop: modify code, benchmark, keep improvements, revert failures.

## Targets

- **Fetch endpoints** (simple data retrieval): **0.5 seconds** max
- **Calculation endpoints** (compute metrics/scores): **1.0 second** max

The benchmark measures every GET endpoint and reports how many exceed their target. Your goal is to get `total_over_target` to **0**.

## Hard constraints — DO NOT VIOLATE

1. **Response correctness is sacred.** Every endpoint must return the exact same data structure and values as before your changes. The benchmark validates that responses are non-empty valid JSON. If you break any response, the experiment is a crash.
2. **DO NOT remove or disable Langfuse tracking.** The `@observe()` decorators and Langfuse client initialization must stay. You may optimize *how* they're called (e.g. make them non-blocking) but you cannot remove them.
3. **DO NOT change the API contract.** Request parameters, response schema, HTTP status codes, and error handling must remain identical.
4. **DO NOT modify files outside your scope** (see Rules below).
5. **DO NOT install new packages or add dependencies.**
6. **DO NOT introduce security vulnerabilities** (SQL injection, etc.).

## What optimizations ARE allowed

- Query optimization: better SQL, fewer round-trips, SELECT only needed columns
- Connection pooling and reuse
- Parallelizing independent database queries within an endpoint
- Reducing unnecessary data transformations or copies
- Optimizing pandas/numpy operations (vectorization over loops)
- Making I/O non-blocking where possible
- Reducing serialization overhead
- Removing redundant computation (but not removing features)
- Optimizing Snowflake query patterns (e.g. avoiding SELECT *, using proper indexes)

## Setup

1. **The worktree is ready**: Your isolated working directory is `/Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get` on branch `autoresearch/get-perf`. All your code changes happen here.
2. **Start the dev server on port 8006**: `cd /Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get && ENVIRONMENT=development PORT=8006 python3 app.py &` — wait a few seconds, then verify it's running.
3. **Read the codebase**: The GET endpoints live in `/Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get/routers/v1/`. Read the endpoint files to understand the patterns. Also read:
   - `core/snowflake_pool.py` or however Snowflake connections are managed
   - `core/cache.py` — the caching layer (read-only, for context)
   - Any shared utilities the endpoints use
4. **Verify the server is running**: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8006/docs` should return 200.
5. **Run baseline benchmark**: `/Users/arnavgupta/Documents/GitHub/autoresearch/.venv/bin/python3 /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/benchmark.py > /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/run.log 2>&1` then `cat /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/run.log`. Record as baseline.
6. **Initialize results.tsv**: Create `/Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/results.tsv` with just the header row.
7. **Confirm and go**: Start the loop.

## Rules

**What you CAN modify:**
- Any endpoint file in `/Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get/routers/v1/` — the endpoint handlers themselves
- Shared data-fetching utilities that the endpoints use (e.g. Snowflake query helpers, data transformation functions)
- The worker files if endpoints delegate to them

**What you CANNOT modify:**
- `core/cache.py` — the caching layer is off-limits
- `app.py` — the main application file
- Pydantic models in `routers/v1/models.py` — response schemas must not change
- Any Langfuse integration code — `@observe()` decorators must remain

## Output format

The benchmark script prints:

```
fetch_p50: 0.32
fetch_p99: 0.48
fetch_max: 0.51
fetch_over_target: 2
fetch_slow: daily-price 0.62s
fetch_slow: news 0.55s
calc_p50: 0.65
calc_p99: 0.91
calc_max: 0.95
calc_over_target: 0
total_over_target: 2
status: success
```

The key metric is `total_over_target` — the number of endpoints exceeding their latency target. Your goal is to get this to **0**. Secondary goal: minimize `fetch_p99` and `calc_p99`.

The `*_slow` lines tell you exactly which endpoints are over target — focus your efforts there.

Extract key metrics:

```
grep "^total_over_target:\|^fetch_p99:\|^calc_p99:\|^status:\|_slow:" /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/run.log
```

## Logging results

Log experiments to `/Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/results.tsv` (tab-separated).

Header and columns:

```
commit	total_over_target	fetch_p99	calc_p99	status	description
```

1. git commit hash from the worktree (short, 7 chars)
2. total_over_target count — use -1 for crashes
3. fetch_p99 in seconds (e.g. 0.48) — use 0.0 for crashes
4. calc_p99 in seconds (e.g. 0.91) — use 0.0 for crashes
5. status: `keep`, `discard`, or `crash`
6. short text description of what this experiment tried

Example:

```
commit	total_over_target	fetch_p99	calc_p99	status	description
a1b2c3d	5	0.82	1.23	keep	baseline
b2c3d4e	3	0.51	0.95	keep	optimize Snowflake query in daily-price
c3d4e5f	3	0.55	0.98	discard	add connection pooling (no improvement)
d4e5f6g	-1	0.0	0.0	crash	syntax error in balance_sheet.py
```

## Strategy guide

Start by identifying the slowest endpoints from the baseline benchmark (`*_slow` lines). Then:

1. **Read the slow endpoint's code** — understand what queries it runs and what processing it does
2. **Profile the bottleneck** — is it the Snowflake query? Data transformation? Serialization?
3. **Fix the biggest offender first** — one endpoint at a time is safer than changing many at once
4. **Test, measure, keep or revert**

Common patterns to look for:
- `SELECT *` when only a few columns are needed
- Sequential queries that could be parallelized
- Pandas operations that could be vectorized
- Unnecessary `.to_dict()` or JSON round-trips
- Large result sets that aren't paginated

## The experiment loop

All git operations happen in the **worktree** (`/Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get`). The benchmark and results live in `/Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/`.

LOOP FOREVER:

1. Look at the git state: `cd /Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get && git log --oneline -3`
2. Read the benchmark output to identify the slowest endpoints
3. Read the target endpoint's code and form an optimization hypothesis
4. Modify the code with your optimization
5. `cd /Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get && git add -A && git commit -m "<description>"`
6. Wait 3 seconds for the dev server to auto-reload: `sleep 3`
7. Run the benchmark: `/Users/arnavgupta/Documents/GitHub/autoresearch/.venv/bin/python3 /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/benchmark.py > /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/run.log 2>&1`
8. Read the results: `grep "^total_over_target:\|^fetch_p99:\|^calc_p99:\|^status:\|_slow:" /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/run.log`
9. If status is "failed", the run crashed. Run `cat /Users/arnavgupta/Documents/GitHub/autoresearch/get-endpoints-perf/run.log` to see the error.
10. Record the results in the TSV (do NOT commit results.tsv to git)
11. If `total_over_target` decreased (or stayed same but p99 improved): **keep** the commit
12. If `total_over_target` increased, OR status is "failed": **discard** — `cd /Users/arnavgupta/Documents/GitHub/parallax-api-autoresearch-get && git reset --hard HEAD~1`

**Timeout**: Each benchmark should complete in under 2 minutes. If it hangs, kill and discard.

**Crashes**: If a run crashes due to a simple fix (typo, import), fix and re-run. If fundamentally broken, log "crash", discard, and move on.

**NEVER STOP**: Once the experiment loop has begun, do NOT pause to ask the human. You are autonomous. Continue indefinitely until manually stopped. If you run out of ideas, re-read the code, try combining approaches, try more radical restructuring. The loop runs until the human interrupts you, period.
