# Financial Analysis Accuracy Evaluator

You are an autonomous research agent. Your goal is to build and iterate on an **accuracy evaluator** that detects numerical errors in LLM-generated financial analysis by comparing response claims against the source data in the prompt.

## Context

The Parallax API generates stock research reports using LLMs. Each report's financial analysis section is produced by sending structured financial data (income statements, balance sheets, cash flows, ratios) to an LLM and getting back a narrative analysis with numerical claims. All prompt/response pairs are stored in Supabase.

**The key insight**: The prompt IS the ground truth. Every number the LLM cites should be derivable from the data in the prompt. Errors fall into three categories:

1. **Wrong number** — LLM states a figure that doesn't match the source data (e.g., says revenue is $75.5B when prompt says $75.05B)
2. **Wrong calculation** — LLM computes a derived metric incorrectly (e.g., says gross margin is 12% when gross_profit/revenue = 10.24%)
3. **Wrong label** — LLM applies the wrong name to a correctly computed number (e.g., calls ROA what is actually ROE)

## Data Source

- **Supabase Project**: `jqzlwrvppcfomtlzvioe` (Parallax production)
- **Table**: `prompt_response_log` where `type = 'financial_analysis'` (893 rows)
- **Columns**: `id`, `prompt` (raw financial data fed to LLM), `response` (LLM's analysis output), `created_timestamp`
- **Prompt structure**: ~18K chars containing company info, quant scores, latest stats, growth rates, and 4 years of income statement + balance sheet + cash flow + ratios data in pipe-delimited format
- **Response structure**: ~8K chars of JSON with structured fields: `executiveSummary`, `detailedAnalysis` (containing `financialPerformance`, `accountingQuality`, `businessStrategy`, `prospectiveAnalysis`)

## Setup

1. **Working directory**: `/Users/arnavgupta/Documents/GitHub/autoresearch/financial-accuracy-eval`
2. **Python environment**: Use the venv at `/tmp/fineval-venv` (has `requests` installed). If it doesn't exist:
   ```bash
   uv venv /tmp/fineval-venv --python 3.12 && uv pip install requests --python /tmp/fineval-venv/bin/python
   ```
3. **Supabase access**: Use the MCP tools (`mcp__supabase__execute_sql`) with project_id `jqzlwrvppcfomtlzvioe`

## The Evaluator You Need to Build

Build `evaluator.py` — a script that:

### Step 1: Sample Selection
- Pull N random rows from `prompt_response_log` where `type = 'financial_analysis'`
- Default N = 30 (configurable via `--sample-size`)
- Save the sample to `runs/` for reproducibility

### Step 2: Prompt Parsing (Ground Truth Extraction)
Parse the structured financial data from the prompt. The prompt contains:
- **Latest Stats**: Current Price, EPS, P/E, P/B, P/FCF, ROE, ROA, ROI, Dividend Yield, etc.
- **Income Statement** (4 years): revenue, cost_of_revenue, gross_profit, operating_income, net_income, basic_eps, etc.
- **Balance Sheet** (4 years): total_assets, total_liabilities, total_equity, total_debt, current_assets, current_liabilities, cash, etc.
- **Cash Flow Statement** (4 years): operating_cash_flow, capex, free_cash_flow, etc.
- **Financial Ratios** (4 years): various pre-computed ratios

Format: pipe-delimited columns (one field per year), values like `75.05B`, `916.00M`, `21.55995`, etc.

Build a parser that extracts these into a structured dict like:
```python
{
    "company": "LMT",
    "years": ["2025", "2024", "2023", "2022"],
    "income_statement": {
        "revenue": [75050, 71040, 67570, 65980],  # in millions
        "gross_profit": [7680, 7020, 8570, 8390],
        "operating_income": [7730, 7010, 8510, 6810],
        "net_income": [5020, 5340, 6920, 5730],
        ...
    },
    "balance_sheet": {
        "total_assets": [59840, 55620, 52460, 52880],
        ...
    },
    "latest_stats": {
        "pe": 28.87,
        "roe": 76.87,
        ...
    }
}
```

### Step 3: Response Parsing (Claim Extraction)
Extract every numerical claim from the LLM response. The response is JSON with text fields containing claims like:
- "Revenue growth acceleration to 5.6% in 2025 ($75.05B vs $71.04B in 2024)"
- "gross margins compressed to 10.24% from 12.68% in 2023"
- "ROE remains exceptionally high at 76.87%"
- "debt-to-equity of 3.23x"
- "operating cash flow of $8.56B in 2025"

Extract structured claims:
```python
{
    "claim_text": "gross margins compressed to 10.24%",
    "metric": "gross_margin",
    "value": 10.24,
    "year": "2025",  # if determinable
    "unit": "percent",
    "context_field": "executiveSummary.keyHighlights[0]"
}
```

**Approach for extraction**: Use regex patterns to find numbers in context. Key patterns:
- `(\d+\.?\d*)%` — percentages (margins, growth rates, yields)
- `\$(\d+\.?\d*)[BM]` — dollar amounts
- `(\d+\.?\d*)x` — ratios/multiples
- Numbers near keywords like "revenue", "margin", "ROE", "debt", "growth", etc.

### Step 4: Verification Engine
For each extracted claim, attempt to verify it against the parsed prompt data:

1. **Direct lookups**: "ROE of 76.87%" → check `latest_stats.roe` = 76.87 ✓
2. **Computed metrics**: "gross margin 10.24%" → compute `gross_profit[2025] / revenue[2025]` = 7680/75050 = 10.23% → within tolerance ✓
3. **Growth rates**: "Revenue growth 5.6%" → compute `(75050-71040)/71040` = 5.64% → ✓
4. **Ratios**: "debt-to-equity 3.23x" → compute `total_debt / total_equity` from balance sheet
5. **Dollar amounts**: "$8.56B operating cash flow" → check cash flow statement

**Tolerance**: Allow 1% relative tolerance for computed metrics (rounding differences are fine). Flag anything >2% off as an error, 1-2% as a warning.

**Unverifiable claims**: Some claims may reference data not in the prompt or be forward-looking projections. Mark these as `unverifiable` rather than errors.

### Step 5: Error Report
For each sampled row, produce:
```json
{
    "id": "row-uuid",
    "symbol": "LMT",
    "total_claims": 45,
    "verified_correct": 38,
    "errors": 3,
    "warnings": 1,
    "unverifiable": 3,
    "accuracy_pct": 92.7,
    "error_details": [
        {
            "claim": "gross margin 12.68% in 2023",
            "expected": 12.68,
            "computed": 12.68,
            "source": "gross_profit[2023]/revenue[2023] = 8570/67570",
            "verdict": "correct"
        },
        {
            "claim": "interest coverage deteriorated to 6.92x",
            "expected": null,
            "computed": 6.90,
            "source": "operating_income[2025]/interest_expense[2025] = 7730/1120",
            "verdict": "warning",
            "deviation_pct": 0.3
        }
    ]
}
```

Aggregate across all sampled rows:
```
overall_accuracy_pct, total_errors, errors_by_type, most_common_error_patterns
```

## Output & Tracking

1. **Detailed run JSON** → `runs/{timestamp}_n{sample_size}.json` — full per-row, per-claim results
2. **Summary TSV** → `results.tsv` — one row per run with aggregate metrics
3. **Stdout** — grep-able `key: value` pairs (like benchmark.py)

## The Research Loop

### Phase 1: Build the Basic Evaluator
1. Start with the prompt parser — get the structured data extraction working
2. Then build claim extraction with regex
3. Then the verification engine for direct lookups and simple computed metrics
4. Run on 5 rows manually to debug, then scale to 30

### Phase 2: Maximize Detection Rate
The agent's real job is to catch as many real errors as possible while minimizing false positives.

After the basic evaluator works:
1. **Analyze misses**: Look at the response text for claims the extractor didn't catch. Add more patterns.
2. **Analyze false positives**: Claims flagged as errors that are actually correct (parser bug, wrong year mapping, etc.). Fix the verification logic.
3. **Add more metric computations**: Start with simple ones (margins, growth rates), then add complex ones (DuPont decomposition, coverage ratios, working capital metrics).
4. **Track coverage**: What % of numerical claims are you even attempting to verify? Aim for >80%.

### Phase 3: Scale & Categorize
1. Run on larger samples (50, 100, 200 rows)
2. Categorize errors: which metrics are most commonly wrong? Which companies? Recent vs. older analyses?
3. Look for systematic patterns (e.g., "the LLM consistently miscalculates YoY growth rates")

### Phase 4: Harden
1. Run 3x on the same sample — results should be deterministic (no randomness in verification)
2. Manually spot-check 10 flagged errors — are they real?
3. Manually spot-check 10 "verified correct" claims — did we miss anything?

## Rules

1. **Only modify files in this directory** (`financial-accuracy-eval/`). Never modify parallax-api or other project code.
2. **You may**:
   - Modify `evaluator.py` to improve extraction and verification logic
   - Add helper scripts for analysis
   - Create test fixtures in a `tests/` directory
3. **You may NOT**:
   - Modify any data in Supabase (read-only access)
   - Fabricate results — every entry in results.tsv must come from an actual eval run
   - Skip rows that are hard to parse — mark them as parse_errors and fix the parser
4. **Log everything** — save full details to `runs/` so results are reproducible.

## Key Metrics to Track

- **Claim extraction rate**: How many numerical claims did you find per response?
- **Verification rate**: Of those, how many could you verify (had enough data in prompt)?
- **Accuracy rate**: Of verified claims, what % matched the source data?
- **Error rate by category**: Wrong number vs. wrong calculation vs. wrong label
- **False positive rate**: Claims flagged as errors that are actually correct (aim for <5%)

## Important Notes

- The prompt data uses formats like `75.05B`, `916.00M`, `-28.00M`. Your parser must handle B (billions), M (millions), negative numbers, null values, and varying precision.
- Some prompts may have slightly different structures (older vs newer). Build the parser to be resilient.
- The response is often wrapped in ```json``` code fences and may have escaped characters. Handle this.
- Year identification matters — "margin of 10.24%" could refer to any of the 4 years. Use context clues (nearby year mentions, "in 2025", "from 2023", etc.).
- Some claims reference derived metrics not directly in the data (e.g., "free cash flow yield" requires FCF and market cap). These require multi-step computation.

## NEVER STOP

After Phase 4, cycle back:
- Increase sample sizes
- Add verification for more complex derived metrics
- Analyze error trends over time (are newer analyses more accurate?)
- Compare error rates across different symbols/sectors
- Test other analysis types (`score_analysis`, `analyst_analysis`, `technical_analysis`)
