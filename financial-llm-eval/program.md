# Financial Analysis LLM Evaluation

## Goal

Find the best LLM model for running Parallax API financial analysis at scale — **50,000+ companies per month** — without degrading quality below the current Claude Sonnet 4 baseline.

**Priority order**: Accuracy (98%+ HC accuracy) > Quality (expert-level writing) > Latency > Cost

The current production model is `claude-sonnet-4-20250514` via Anthropic direct API, running at temperature 0.3 with a 64K max token budget. We need to determine if we can achieve equivalent or better results through OpenRouter at scale.

## How It Works

1. **Real prompts**: `sample_data.json` contains 20 real financial analysis prompts pulled from `prompt_response_log` in Supabase — the exact same prompts sent to Claude in production
2. **Send to model**: Each prompt is sent to a candidate model via OpenRouter with the same system prompt and temperature as production
3. **Accuracy evaluation**: Responses are scored using the `financial-accuracy-eval` evaluator, which:
   - Parses ground truth from the prompt (financial statements, ratios, stats)
   - Extracts numerical claims from the LLM response (regex-based)
   - Verifies each claim against ground truth (within tolerance)
   - Reports accuracy, HC accuracy (filters false positives), error counts
4. **Quality checks**: JSON validity, schema completeness (all required sections present)
5. **Results auto-saved**: `runs/` for detailed JSON, `results.tsv` for summary

## Key Metrics

- **accuracy_pct**: % of verifiable claims that match ground truth (≤1% deviation)
- **hc_accuracy_pct**: High-confidence accuracy — treats >50% deviation errors as false positives (evaluator limitations)
- **verification_rate**: % of total claims that can be verified against source data
- **json_valid_pct**: % of responses that parse as valid JSON
- **schema_complete_pct**: % of responses with all required sections (executiveSummary, detailedAnalysis with all subsections)

## Setup

1. **Working directory**: `/Users/arnavgupta/Documents/GitHub/autoresearch/financial-llm-eval`
2. **API Key**: Auto-loads from `~/Documents/GitHub/parallax-api/.env`
3. **Python environment**: `/tmp/fineval-venv` (needs `requests`)
4. **Evaluator dependency**: `../financial-accuracy-eval/evaluator.py` (imported at runtime)

## Running

```bash
# Basic run (all 20 samples)
/tmp/fineval-venv/bin/python benchmark.py --model anthropic/claude-sonnet-4 --verbose

# Quick test (5 samples)
/tmp/fineval-venv/bin/python benchmark.py --model openai/gpt-4o --samples 5 --verbose

# With temperature variation
/tmp/fineval-venv/bin/python benchmark.py --model openai/gpt-4o --temperature 0.1 --tag temp01 --verbose
```

## Candidate Models

### Tier 1 — Frontier
1. `anthropic/claude-sonnet-4` — current production baseline
2. `openai/gpt-4o` — strong general reasoning
3. `google/gemini-2.5-flash` — fast, cheap
4. `google/gemini-2.5-pro` — strongest Google reasoner
5. `openai/o4-mini` — reasoning-focused

### Tier 2 — Cost-Effective
6. `openai/gpt-4o-mini` — cheapest frontier-class
7. `deepseek/deepseek-chat-v3` — strong math, very cheap
8. `meta-llama/llama-4-maverick` — open-source
9. `qwen/qwen3-235b-a22b` — strong on financial/quantitative

### Tier 3 — If time permits
10. `anthropic/claude-haiku-4` — fast/cheap Anthropic
11. `mistralai/mistral-large-latest` — European alternative

## Experiment Phases

### Phase 1: Model Survey
Run each candidate model on all 20 samples. Compare accuracy, HC accuracy, latency, JSON validity.

### Phase 2: Prompt Engineering
For top 3 models by HC accuracy:
- Temperature variations: 0.0, 0.1, 0.3, 0.5
- Custom system prompts (chain-of-thought, domain expert, stricter format instructions)

### Phase 3: Scale Analysis
For the winning model(s):
- Estimate cost per analysis at OpenRouter pricing
- Calculate monthly cost for 50k companies
- Measure consistency (run same samples 3x)
- Identify systematic error patterns

## Rules

1. Only modify files in this directory
2. Every entry in results.tsv must come from an actual benchmark run
3. Never fabricate results
4. Store everything locally — no Supabase uploads
5. The accuracy evaluator has known limitations (~72% verification rate, some false positives) — use HC accuracy as the primary comparison metric
