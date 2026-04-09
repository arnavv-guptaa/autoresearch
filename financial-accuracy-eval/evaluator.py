#!/usr/bin/env python3
"""
Financial Analysis Accuracy Evaluator

Detects numerical errors in LLM-generated financial analysis by comparing
response claims against source data in the prompt.
"""

import argparse
import json
import os
import re
import sys
import datetime
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPABASE_PROJECT_ID = "jqzlwrvppcfomtlzvioe"
RUNS_DIR = Path(__file__).parent / "runs"
RESULTS_TSV = Path(__file__).parent / "results.tsv"

# Tolerance for computed metric verification
TOLERANCE_OK = 0.01       # <=1% relative deviation → correct
TOLERANCE_WARN = 0.02     # 1-2% → warning
                          # >2% → error

# ---------------------------------------------------------------------------
# Value Parsing Helpers
# ---------------------------------------------------------------------------

def parse_value(raw: str) -> float | None:
    """Parse values like '75.05B', '916.00M', '-28.00M', 'null', '0'."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw.lower() in ("null", "none", "", "-"):
        return None
    raw = raw.replace(",", "")
    multiplier = 1.0
    if raw.upper().endswith("B"):
        multiplier = 1000.0  # convert to millions
        raw = raw[:-1]
    elif raw.upper().endswith("M"):
        multiplier = 1.0     # already millions
        raw = raw[:-1]
    elif raw.upper().endswith("K"):
        multiplier = 0.001
        raw = raw[:-1]
    try:
        return float(raw) * multiplier
    except ValueError:
        return None


def parse_value_raw(raw: str) -> float | None:
    """Parse a raw numeric value without unit conversion (for ratios, percentages)."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw.lower() in ("null", "none", "", "-"):
        return None
    raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Prompt Parser — Ground Truth Extraction
# ---------------------------------------------------------------------------

def parse_prompt(prompt_text: str) -> dict:
    """Parse the structured financial data from the prompt into a dict."""
    result = {
        "company": None,
        "years": [],
        "income_statement": {},
        "balance_sheet": {},
        "cash_flow": {},
        "ratios": {},
        "latest_stats": {},
        "growth_rates": {},
        "quant_scores": {},
    }

    # Extract symbol
    m = re.search(r"Symbol:\s*(\S+)", prompt_text)
    if m:
        result["company"] = m.group(1)

    # Extract latest stats
    stats_patterns = {
        "current_price": r"Current Price:\s*([\d,.]+)",
        "eps": r"EPS:\s*([\d,.]+)",
        "pe": r"P/E:\s*([\d,.]+)",
        "pb": r"P/B:\s*([\d,.]+)",
        "pfcf": r"P/FCF:\s*([\d,.]+)",
        "roe": r"ROE:\s*([\d,.]+)",
        "roa": r"ROA:\s*([\d,.]+)",
        "roi": r"ROI:\s*([\d,.]+)",
        "dividend_yield": r"Latest Dividend Yield:\s*([\d,.]+)",
        "latest_dividend": r"Latest Dividend:\s*([\d,.]+)",
    }
    for key, pattern in stats_patterns.items():
        m = re.search(pattern, prompt_text)
        if m:
            result["latest_stats"][key] = parse_value_raw(m.group(1))

    # Extract growth rates
    m = re.search(r"Revenue per Share \(5yr CAGR\):\s*([\d,.-]+)", prompt_text)
    if m:
        result["growth_rates"]["revenue_per_share_5yr_cagr"] = parse_value_raw(m.group(1))
    m = re.search(r"Net Income per Share \(5yr CAGR\):\s*([\d,.-]+)", prompt_text)
    if m:
        result["growth_rates"]["net_income_per_share_5yr_cagr"] = parse_value_raw(m.group(1))

    # Parse each financial statement section
    sections = [
        ("1. Income Statement:", "income_statement"),
        ("2. Balance Sheet:", "balance_sheet"),
        ("3. Cash Flow:", "cash_flow"),
        ("4. Key Ratios:", "ratios"),
    ]

    for section_header, section_key in sections:
        section_data = _parse_section(prompt_text, section_header, section_key == "ratios")
        if section_data:
            if not result["years"] and section_data.get("_years"):
                result["years"] = section_data.pop("_years")
            else:
                section_data.pop("_years", None)
            result[section_key] = section_data

    return result


def _parse_section(prompt_text: str, header: str, is_ratio: bool = False) -> dict | None:
    """Parse a pipe-delimited financial data section."""
    idx = prompt_text.find(header)
    if idx == -1:
        return None

    # Find the data block after the header
    block = prompt_text[idx:]
    # Find the next section or end
    next_section = None
    for marker in ["1. Income Statement:", "2. Balance Sheet:", "3. Cash Flow:", "4. Key Ratios:", "ANALYSIS FRAMEWORK"]:
        if marker == header:
            continue
        pos = block.find(marker)
        if pos > 0:
            if next_section is None or pos < next_section:
                next_section = pos
    if next_section:
        block = block[:next_section]

    lines = block.strip().split("\n")
    data = {}
    years = []

    # Find the data header line with years
    for line in lines:
        m = re.search(r"Data \(\d+ records\):\s*(.+)", line)
        if m:
            year_parts = [p.strip() for p in m.group(1).split("|")]
            years = []
            for yp in year_parts:
                ym = re.match(r"(\d{4})", yp)
                if ym:
                    years.append(ym.group(1))
            break

    if not years:
        return None

    data["_years"] = years

    # Parse each data row
    for line in lines:
        line = line.strip()
        if not line or line.startswith("=") or line.startswith("Data ("):
            continue
        if ":" not in line:
            continue
        # Split on first colon
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        field_name = parts[0].strip()
        values_str = parts[1].strip()

        # Skip metadata fields
        if field_name in ("ric", "perenddt", "stmtdt", "sourcedt", "pertypecode",
                          "currency", "unitsconvtocode", "rkdcode", "year"):
            # But capture year field for ratios
            if field_name == "year":
                year_vals = [v.strip() for v in values_str.split("|")]
                data["_years"] = year_vals
            continue

        values = [v.strip() for v in values_str.split("|")]
        if is_ratio:
            parsed = [parse_value_raw(v) for v in values]
        else:
            parsed = [parse_value(v) for v in values]

        data[field_name] = parsed

    return data


# ---------------------------------------------------------------------------
# Response Parser — Claim Extraction
# ---------------------------------------------------------------------------

def extract_response_json(response_text: str) -> dict | None:
    """Extract JSON from the LLM response text (handles code fences, Message wrapper)."""
    # Handle Message(...) / BetaMessage(...) wrapper from Anthropic API
    if "TextBlock(" in response_text or "text='" in response_text or "text=\"" in response_text:
        # Try to extract ALL text blocks (may have multiple)
        # Pattern: text='...' or text="..."
        texts = []
        for m in re.finditer(r"text='(.*?)'(?:\s*,\s*type='text'|\s*\))", response_text, re.DOTALL):
            t = m.group(1)
            t = t.replace("\\\\'", "'").replace("\\\\n", "\n").replace('\\"', '"')
            t = t.replace("\\n", "\n").replace("\\'", "'")
            texts.append(t)
        for m in re.finditer(r'text="(.*?)"(?:\s*,\s*type="text"|\s*\))', response_text, re.DOTALL):
            t = m.group(1)
            t = t.replace('\\\\"', '"').replace("\\\\n", "\n").replace("\\n", "\n")
            texts.append(t)
        if texts:
            # Use the longest text block (likely the actual analysis)
            response_text = max(texts, key=len)

    # Try to extract JSON from code fences
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response_text, re.DOTALL)
    if m:
        json_str = m.group(1).strip()
    else:
        json_str = response_text.strip()

    # Clean up escaped characters
    json_str = json_str.replace("\\\\n", " ")
    json_str = json_str.replace("\\\\", "\\")

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # Try harder — find first { to last }
        start = json_str.find("{")
        end = json_str.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(json_str[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def flatten_json_texts(obj, path="") -> list[tuple[str, str]]:
    """Recursively extract all text values from the JSON response with their paths."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            results.extend(flatten_json_texts(v, new_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            new_path = f"{path}[{i}]"
            results.extend(flatten_json_texts(item, new_path))
    elif isinstance(obj, str):
        results.append((path, obj))
    return results


def extract_claims(response_json: dict) -> list[dict]:
    """Extract numerical claims from the response JSON."""
    claims = []
    text_fields = flatten_json_texts(response_json)

    for field_path, text in text_fields:
        # Handle DuPont decomposition formulas specially
        dupont_claims, dupont_spans = _extract_dupont_claims(text, field_path)
        claims.extend(dupont_claims)
        # Extract remaining claims, skipping DuPont spans
        claims.extend(_extract_claims_from_text(text, field_path, skip_spans=dupont_spans))

    return claims


def _extract_dupont_claims(text: str, field_path: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """Extract claims from DuPont decomposition formulas."""
    claims = []
    spans = []
    year_mentions = re.findall(r'\b(20[12]\d)\b', text)

    # 3-factor: ROE = Net Margin × Asset Turnover × Equity Multiplier
    # Also handles: ROE of X% (YEAR) = ROA Y% × Equity Multiplier Zx
    dupont_patterns = [
        # ROE of X% = ROA/Net Margin Y% × Asset Turnover Z × Equity Multiplier W
        r'ROE\s+(?:of\s+)?([\d.]+)%?\s*(?:\([^)]*\))?\s*(?:=|:)\s*'
        r'(?:ROA|Net\s+Margin)\s+([\d.]+)%?\s*[×x*]\s*'
        r'(?:Asset\s+Turnover\s+)?([\d.]+)x?\s*[×x*]\s*'
        r'(?:Equity\s+Multiplier\s+)?([\d.]+)x?',
        # ROE = X% × Y% × Zx (unlabeled components)
        r'ROE\s*(?:=|:)\s*([\d.]+)%?\s*[×x*]\s*([\d.]+)%?\s*[×x*]\s*([\d.]+)x?',
    ]

    for pattern in dupont_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if any(s[0] <= m.start() < s[1] for s in spans):
                continue
            spans.append((m.start(), m.end()))
            year = _identify_year_from_context(text, m.start(), year_mentions)
            try:
                claims.append({"claim_text": m.group(0)[:80], "metric": "roe", "value": float(m.group(1)),
                               "year": year, "unit": "percent", "context_field": field_path})
            except (ValueError, IndexError):
                pass

    # 2-factor: ROE of X% = ROA Y% × Equity Multiplier Zx
    two_factor_patterns = [
        # ROE of X% = ROA Y% × Equity Multiplier Zx (with optional parens/negative)
        r'ROE\s+(?:of\s+)?-?([\d.]+)%?\s*(?:\([^)]*\))?\s*(?:=|:)\s*'
        r'ROA\s*\(?-?([\d.]+)%?\)?\s*[×x*]\s*'
        r'(?:Equity\s+Multiplier\s+)?\(?([\d.]+)x?\)?',
        # ROE = Net Margin × Equity Multiplier
        r'ROE\s+(?:of\s+)?-?([\d.]+)%?\s*(?:\([^)]*\))?\s*(?:=|:)\s*'
        r'(?:Net\s+Margin\s+)\(?([\d.]+)%?\)?\s*[×x*]\s*'
        r'(?:Equity\s+Multiplier\s+)\(?([\d.]+)x?\)?',
    ]

    for pattern in two_factor_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if any(s[0] <= m.start() < s[1] for s in spans):
                continue
            spans.append((m.start(), m.end()))
            year = _identify_year_from_context(text, m.start(), year_mentions)
            try:
                claims.append({"claim_text": m.group(0)[:80], "metric": "roe", "value": float(m.group(1)),
                               "year": year, "unit": "percent", "context_field": field_path})
                claims.append({"claim_text": m.group(0)[:80], "metric": "roa", "value": float(m.group(2)),
                               "year": year, "unit": "percent", "context_field": field_path})
                claims.append({"claim_text": m.group(0)[:80], "metric": "assets_equity", "value": float(m.group(3)),
                               "year": year, "unit": "ratio", "context_field": field_path})
            except (ValueError, IndexError):
                pass

    return claims, spans


def _is_part_of_range(text: str, match_start: int, match_end: int) -> bool:
    """Check if a number is part of a range like '12-13x' or '5-7%' or '$500-550'."""
    # Check if preceded by digit-dash (this number is the high end of range)
    before = text[max(0, match_start - 10):match_start]
    if re.search(r'\d\s*[-–—]\s*$', before):
        return True
    # Check if followed by dash-digit (this number is the low end of range)
    after = text[match_end:min(len(text), match_end + 10)]
    if re.search(r'^\s*[-–—]\s*\d', after):
        return True
    return False


def _extract_claims_from_text(text: str, field_path: str, skip_spans: list[tuple[int, int]] = None) -> list[dict]:
    """Extract numerical claims from a text string."""
    claims = []
    skip_spans = skip_spans or []

    def _in_skip_span(pos):
        return any(s[0] <= pos < s[1] for s in skip_spans)

    # Determine year context from surrounding text
    year_mentions = re.findall(r'\b(20[12]\d)\b', text)

    # --- Pattern 1: Dollar amounts like $75.05B, $8.56B ---
    # Also handle currency-prefixed: HK$, CAD, etc.
    for m in re.finditer(r'(?:HK|CAD|SGD|TWD|THB|MYR|AUD|INR|EUR|GBP|CNY|JPY)?\$\s*([\d,.]+)\s*([BbMmKk])\b', text):
        if _is_part_of_range(text, m.start(), m.end()) or _in_skip_span(m.start()):
            continue
        val_str = m.group(1).replace(",", "")
        unit_char = m.group(2).upper()
        try:
            val = float(val_str)
        except ValueError:
            continue

        # --- Dollar amount metric identification ---
        after_dollar = text[m.end():min(len(text), m.end() + 40)].strip()
        after_dollar_lower = after_dollar.lower()
        before_dollar_30 = text[max(0, m.start() - 30):m.start()].strip().lower()

        # Extended after-text keywords (only used for formula/denominator detection)
        AFTER_KEYWORDS = METRIC_KEYWORDS + [
            ("assets", "total_assets"),
            ("average assets", "total_assets"),
        ]

        # Check if this dollar amount is a formula denominator or component
        # e.g., "revenue / $171B assets" or "÷ Average Assets $114B"
        formula_op_match = re.search(r'[÷/]\s*(.{0,30}?)$', before_dollar_30)
        is_formula_denominator = formula_op_match is not None

        if is_formula_denominator:
            # This is a formula denominator — identify from between-text or after-text
            between_text = formula_op_match.group(1).strip().lower() if formula_op_match else ""
            after_stripped = after_dollar_lower.lstrip(' ,;')
            metric = "unknown"
            # First check between operator and dollar sign: "÷ Average Assets $114B"
            for kw, mn in AFTER_KEYWORDS:
                if kw in between_text:
                    metric = mn
                    break
            # Then check after dollar amount: "$114B assets"
            if metric == "unknown":
                for kw, mn in AFTER_KEYWORDS:
                    if after_stripped[:len(kw) + 5].startswith(kw):
                        metric = mn
                        break
        else:
            # Check if this is a subtraction/addition formula component
            # "$402M operating cash flow - $46M capex"
            # Also detect parenthetical breakdowns: "of $356M ($402M OCF - $46M capex)"
            is_subtraction = bool(re.search(r'[\-+]\s*$', before_dollar_30))
            is_paren_breakdown = bool(re.search(r'\(\s*$', before_dollar_30))
            after_stripped = after_dollar_lower.lstrip(' ,;')

            if is_subtraction or is_paren_breakdown:
                # Formula component — check after-text for the component label
                metric = "unknown"
                for kw, mn in METRIC_KEYWORDS:
                    if after_stripped[:len(kw) + 5].startswith(kw):
                        metric = mn
                        break
                if metric == "unknown":
                    # Fall back to before-context
                    ctx_start = max(0, m.start() - 60)
                    ctx_end = min(len(text), m.end() + 30)
                    context = text[ctx_start:ctx_end]
                    num_pos = m.start() - ctx_start
                    metric = _identify_metric_from_context(context, num_pos)
            else:
                # Default: use before-context (standard proximity matching)
                ctx_start = max(0, m.start() - 60)
                ctx_end = min(len(text), m.end() + 30)
                context = text[ctx_start:ctx_end]
                num_pos = m.start() - ctx_start
                metric = _identify_metric_from_context(context, num_pos)

        # "$7.8B (38% of revenue)" — dollar amount before a "% of revenue"
        # parenthetical is an expense, not revenue
        if metric == "revenue" and re.search(r'^\s*\([\d.]+%\s*(of\s+)?revenue', after_dollar_lower):
            metric = "unknown"
        if metric == "revenue" and ("of revenue" in after_dollar_lower[:20] or "% of revenue" in after_dollar_lower[:20]):
            metric = "unknown"

        # "X% of revenue ($2.60B / $52.55B)" — the first dollar amount in a fraction
        # showing an expense ratio is NOT revenue. Detect: "of revenue" in before + "/ $" in after.
        if metric == "revenue" and "of revenue" in before_dollar_30:
            if re.search(r'^\s*[÷/]\s*\$', after_dollar_lower):
                metric = "unknown"  # This is the numerator (expense), not revenue

        year = _identify_year_from_context(text, m.start(), year_mentions)

        # Skip scenario/forward-looking dollar claims
        if _is_scenario_claim(text, m.start()):
            continue

        claims.append({
            "claim_text": text[max(0, m.start()-30):m.end()+10].strip(),
            "metric": metric,
            "value": val,
            "unit_char": unit_char,
            "year": year,
            "unit": "dollars",
            "context_field": field_path,
        })

    # --- Pattern 2: Percentages like 10.24%, -5.6% ---
    for m in re.finditer(r'(-?\s*[\d,.]+)\s*%', text):
        if _is_part_of_range(text, m.start(), m.end()) or _in_skip_span(m.start()):
            continue
        val_str = m.group(1).replace(",", "").replace(" ", "")
        # Additional range check: if the negative sign is actually a range dash
        if val_str.startswith("-"):
            before_neg = text[max(0, m.start() - 5):m.start()]
            if re.search(r'\d\s*$', before_neg):
                continue  # This is part of a range like "8-18%"
        try:
            val = float(val_str)
        except ValueError:
            continue

        ctx_start = max(0, m.start() - 120)
        ctx_end = min(len(text), m.end() + 40)
        context = text[ctx_start:ctx_end]
        num_pos = m.start() - ctx_start

        metric = _identify_metric_from_context(context, num_pos)

        # FIX: Context bleeding — "Multiplier 1.80x. ROE decline from 9.34%"
        # When "from X%" is preceded by a metric mention like "ROE decline from",
        # the metric should be whatever is the SUBJECT of the decline, not the
        # closest keyword. Check if "from" appears in the immediate before-text
        # and a metric name appears before "from".
        immediate_before = text[max(0, m.start() - 40):m.start()].lower()
        from_match = re.search(r'(\w[\w\s/]*?)\s+(?:decline[d]?|fell|drop(?:ped)?|increase[d]?|improve[d]?|grew|rose|change[d]?)\s+(?:by\s+)?(?:approximately\s+)?(?:from\s+|to\s+)?$', immediate_before)
        if from_match:
            subject = from_match.group(1).strip()
            # Try to identify the subject metric
            for kw, mn in METRIC_KEYWORDS:
                if subject.endswith(kw) or subject == kw:
                    metric = mn
                    break

        year = _identify_year_from_context(text, m.start(), year_mentions)

        # Skip forward-looking scenario claims
        if _is_scenario_claim(text, m.start()):
            continue

        # Check for "X% of revenue" pattern — not a standard margin metric
        after_pct = text[m.end():min(len(text), m.end() + 20)].strip().lower()
        if after_pct.startswith("of revenue") or after_pct.startswith("of total"):
            metric = "pct_of_revenue"

        # Check for CAGR — mark distinctly so we don't compare against YoY
        before_pct = text[max(0, m.start() - 60):m.start()].lower()
        after_pct_cagr = text[m.end():min(len(text), m.end() + 30)].lower()
        is_cagr = "cagr" in before_pct or "cagr" in after_pct_cagr
        if is_cagr:
            if metric in ("revenue_growth", "net_income_growth", "eps_growth"):
                metric = metric + "_cagr"
            elif metric == "eps":
                metric = "eps_growth_cagr"
            elif metric in ("unknown", "revenue", "net_income"):
                # Generic CAGR — try to identify what it's for
                if "revenue" in before_pct or "revenue" in after_pct_cagr:
                    metric = "revenue_growth_cagr"
                elif "eps" in before_pct or "earnings" in before_pct:
                    metric = "eps_growth_cagr"
                else:
                    metric = "unknown_cagr"

        # Skip "total assets growing X%" — not revenue growth
        if metric == "revenue_growth":
            if "total assets" in before_pct or "asset growth" in before_pct:
                metric = "asset_growth"
        # Skip "X% revenue decline" where number is NOT revenue growth
        if metric == "roe" and ("revenue" in after_pct[:20].lower()):
            metric = "unknown"
        # Skip "30%+ growth" from being tagged as P/E
        if metric == "pe":
            after_check = after_pct_cagr[:20] if after_pct_cagr else ""
            if "growth" in after_check or "+" in after_check[:5]:
                metric = "unknown"
        # Skip "ROE declined X% from Y" — X is a decline percentage
        if metric == "roe" and ("declined" in before_pct or "dropped" in before_pct
                                or "decreased" in before_pct or "fell" in before_pct
                                or "decline" in before_pct):
            after_from = text[m.end():min(len(text), m.end() + 20)].lower()
            if "from" in after_from:
                metric = "unknown"  # Percentage decline, not ROE value

        claims.append({
            "claim_text": text[max(0, m.start()-30):m.end()+10].strip(),
            "metric": metric,
            "value": val,
            "year": year,
            "unit": "percent",
            "context_field": field_path,
        })

    # --- Pattern 3: Ratios/multiples like 3.23x, 8.9x ---
    for m in re.finditer(r'([\d,.]+)\s*x\b', text):
        if _is_part_of_range(text, m.start(), m.end()) or _in_skip_span(m.start()):
            continue
        val_str = m.group(1).replace(",", "")
        try:
            val = float(val_str)
        except ValueError:
            continue

        # For ratios, also check what comes RIGHT AFTER "Xx" — patterns like "35x P/E"
        ctx_start = max(0, m.start() - 120)
        ctx_end = min(len(text), m.end() + 60)
        context = text[ctx_start:ctx_end]
        num_pos = m.start() - ctx_start

        # Check for "Xx METRIC" pattern (metric label after the number)
        after_x = text[m.end():min(len(text), m.end() + 40)].strip().lower()
        after_metric = None
        for kw, mn in [("p/e", "pe"), ("pe", "pe"), ("p/fcf", "pfcf"),
                        ("p/b", "pb"), ("forward earnings", "pe"),
                        ("forward p/e", "pe"), ("trailing p/e", "pe")]:
            if after_x.startswith(kw):
                after_metric = mn
                break

        metric = after_metric or _identify_metric_from_context(context, num_pos)
        year = _identify_year_from_context(text, m.start(), year_mentions)

        # Skip scenario/forward-looking ratio claims
        if _is_scenario_claim(text, m.start()):
            continue

        # Skip forward/historical/target P/E and other scenario multiples
        if metric in ("pe", "pb", "pfcf", "price_sales", "ev_revenue", "ev_ebit"):
            before_ratio = text[max(0, m.start() - 80):m.start()].lower()
            if any(kw in before_ratio for kw in ["forward", "historical", "target",
                                                   "projected", "re-rating", "normalized",
                                                   "average", "expansion", "justif",
                                                   "based on", "valuation at", "fair value",
                                                   "suggesting", "conservative", "premium"]):
                continue

        claims.append({
            "claim_text": text[max(0, m.start()-30):m.end()+10].strip(),
            "metric": metric,
            "value": val,
            "year": year,
            "unit": "ratio",
            "context_field": field_path,
        })

    return claims


def _is_scenario_claim(text: str, pos: int) -> bool:
    """Check if a claim is in a scenario/forward-looking context (bull/bear/base case)."""
    before = text[max(0, pos - 100):pos].lower()
    scenario_markers = ["bull case", "bear case", "base case", "scenario",
                        "target price", "could reach", "margin expansion to",
                        "growth to ", "annually with", "target $", "target of"]
    return any(marker in before for marker in scenario_markers)


def _is_target_price_multiple(text: str, pos: int) -> bool:
    """Check if a ratio is a P/E multiple in target price context: '25x $2.80 EPS'."""
    before = text[max(0, pos - 80):pos].lower()
    after = text[pos:min(len(text), pos + 40)].lower()
    return ("target" in before or "price" in before) and ("$" in after[:15] or "eps" in after[:15])


# Metric identification keywords (ordered by specificity)
METRIC_KEYWORDS = [
    # Margins
    ("gross margin", "gross_margin"),
    ("gross profit margin", "gross_margin"),
    ("ebitda margin", "ebitda_margin"),
    ("operating margin", "operating_margin"),
    ("pretax margin", "pretax_margin"),
    ("net margin", "net_margin"),
    ("net profit margin", "net_margin"),
    ("profit margin", "net_margin"),
    # Returns
    ("return on equity", "roe"),
    ("return on assets", "roa"),
    ("return on invested capital", "roic"),
    ("roe decline", "roe"),
    ("roe of", "roe"),
    ("roe remains", "roe"),
    ("roe", "roe"),
    ("roa dropping", "roa"),
    ("roa of", "roa"),
    ("roa", "roa"),
    ("roic", "roic"),
    ("roi remains", "roi"),
    ("roi", "roi"),
    # Valuation
    ("p/e", "pe"),
    ("price-to-earnings", "pe"),
    ("price to earnings", "pe"),
    ("p/b", "pb"),
    ("price-to-book", "pb"),
    ("price to book", "pb"),
    ("price book value", "pb"),
    ("p/fcf", "pfcf"),
    ("price-to-free-cash-flow", "pfcf"),
    ("price to free cash flow", "pfcf"),
    ("price cash flow", "price_cash_flow"),
    ("price sales", "price_sales"),
    ("ev/revenue", "ev_revenue"),
    ("ev/ebit", "ev_ebit"),
    ("enterprise value revenue", "ev_revenue"),
    ("enterprise value ebit", "ev_ebit"),
    # Growth
    ("revenue growth", "revenue_growth"),
    ("revenue cagr", "revenue_cagr"),
    ("net income growth", "net_income_growth"),
    ("eps growth", "eps_growth"),
    # Coverage & leverage
    ("ebitda interest coverage", "ebitda_interest_coverage"),
    ("ebitda-to-interest", "ebitda_interest_coverage"),
    ("ebitda/interest", "ebitda_interest_coverage"),
    ("ebitda to interest", "ebitda_interest_coverage"),
    ("interest coverage", "interest_coverage"),
    ("times interest earned", "times_interest_earned"),
    ("net debt-to-ebitda", "net_debt_to_ebitda"),
    ("net debt to ebitda", "net_debt_to_ebitda"),
    ("net debt/ebitda", "net_debt_to_ebitda"),
    ("debt-to-equity", "debt_equity"),
    ("debt to equity", "debt_equity"),
    ("debt/equity", "debt_equity"),
    ("total debt ebitda", "total_debt_ebitda"),
    ("total debt/ebitda", "total_debt_ebitda"),
    ("total debt to ebitda", "total_debt_ebitda"),
    ("lt debt to total capital", "lt_debt_to_total_capital"),
    ("long-term debt", "lt_debt_to_total_capital"),
    ("equity multiplier", "assets_equity"),
    ("assets equity", "assets_equity"),
    ("financial leverage", "assets_equity"),
    # Liquidity
    ("current ratio", "current_ratio"),
    ("quick ratio", "quick_ratio"),
    ("cash ratio", "cash_ratio"),
    ("cash cycle", "cash_cycle_days"),
    # Yields
    ("dividend yield", "dividend_yield"),
    ("fcf yield", "fcf_yield"),
    ("free cash flow yield", "fcf_yield"),
    # Dollar amounts — more specific first
    ("r&d", "rd_expense"),
    ("research and development", "rd_expense"),
    ("sga expense", "sga_expense"),
    ("sg&a", "sga_expense"),
    ("selling general", "sga_expense"),
    ("cost of revenue", "cost_of_revenue"),
    ("cost of goods", "cost_of_revenue"),
    ("total revenue", "total_revenue"),
    ("quarterly revenue", "quarterly_revenue"),
    ("segment revenue", "segment_revenue"),
    ("revenue", "revenue"),
    ("gross profit", "gross_profit"),
    ("operating income", "operating_income"),
    ("operating cash flow", "operating_cash_flow"),
    ("cash from operating", "operating_cash_flow"),
    ("cash generation", "operating_cash_flow"),
    ("cash flow from operations", "operating_cash_flow"),
    ("operational cash flow", "operating_cash_flow"),
    ("net income", "net_income"),
    ("net loss", "net_income"),
    ("free cash flow", "free_cash_flow"),
    ("total debt", "total_debt"),
    ("total assets", "total_assets"),
    ("average assets", "total_assets"),
    ("total equity", "total_equity"),
    ("total liabilities", "total_liabilities"),
    ("cash and short", "cash"),
    ("cash position", "cash"),
    ("cash balance", "cash"),
    ("cash reserves", "cash"),
    ("capital expenditure", "capex"),
    ("capital investment", "capex"),
    ("capex", "capex"),
    ("interest expense", "interest_expense"),
    ("interest payment", "interest_expense"),
    ("goodwill", "goodwill"),
    ("accounts receivable", "accounts_receivable"),
    ("inventory", "inventory"),
    ("current assets", "total_current_assets"),
    ("current liabilities", "total_current_liabilities"),
    ("long term debt", "total_long_term_debt"),
    ("working capital", "working_capital"),
    ("share repurchase", "share_repurchase"),
    ("buyback", "share_repurchase"),
    ("dividend", "dividends_paid"),
    ("depreciation", "depreciation"),
    ("earnings per share", "eps"),
    ("eps", "eps"),
    ("shares outstanding", "shares_outstanding"),
    ("fixed asset turnover", "fixed_asset_turnover"),
    ("asset turnover", "asset_turnover"),
    ("tax rate", "effective_tax_rate"),
    ("effective tax", "effective_tax_rate"),
    ("provision for income", "provision_for_income_taxes"),
    ("debt/ebitda", "total_debt_ebitda"),
    ("debt-to-ebitda", "total_debt_ebitda"),
    ("debt to ebitda", "total_debt_ebitda"),
    # Property/plant/equipment
    ("pp&e", "ppe"),
    ("ppe", "ppe"),
    ("property plant", "ppe"),
    # Investment income
    ("investment income", "investment_income"),
    # EBITDA (dollar amount)
    ("ebitda", "ebitda"),
    # Unusual/other expenses
    ("unusual expense", "unusual_expense"),
    # Accruals / other
    ("other liabilities", "other_liabilities"),
    ("operating expense", "operating_expense"),
    ("net loss", "net_income"),
]


def _identify_metric_from_context(context: str, num_pos_in_context: int = None) -> str:
    """Identify which financial metric a number refers to based on surrounding text.

    Strategy: prefer keywords that appear BEFORE the number (the label that
    introduces the number) over keywords that appear after it. Among keywords
    before the number, prefer the closest one. Use after-context only as
    fallback.
    """
    context_lower = context.lower()

    if num_pos_in_context is None:
        # Estimate: number is near the end of context
        num_pos_in_context = len(context) - 20

    before = context_lower[:num_pos_in_context]
    after = context_lower[num_pos_in_context:]

    # Find candidates in the BEFORE text (prefer closest to number = highest position)
    before_candidates = []
    for keyword, metric_name in METRIC_KEYWORDS:
        pos = before.rfind(keyword)
        if pos >= 0:
            before_candidates.append((pos, len(keyword), metric_name, keyword))

    if before_candidates:
        # Sort by position descending (closest to number), then keyword length desc
        before_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best = before_candidates[0]
        # Among candidates close to each other, prefer longer (more specific)
        top = [c for c in before_candidates if best[0] - c[0] <= 25]
        top.sort(key=lambda x: x[1], reverse=True)
        return top[0][2]

    # Fallback: check after-context
    after_candidates = []
    for keyword, metric_name in METRIC_KEYWORDS:
        pos = after.find(keyword)
        if pos >= 0:
            after_candidates.append((pos, len(keyword), metric_name, keyword))

    if after_candidates:
        after_candidates.sort(key=lambda x: (x[0], -x[1]))
        return after_candidates[0][2]

    return "unknown"


def _identify_year_from_context(text: str, claim_pos: int, year_mentions: list[str]) -> str | None:
    """Try to determine which year a claim refers to.

    Strategy: find year mentions near the claim. Prefer years that appear
    immediately after the number (e.g., "10.24% in 2023") or in parentheses
    (e.g., "10.24% (2023)"). Fall back to the nearest preceding year mention.
    """
    # First check for "in YEAR" or "(YEAR)" right after the claim
    after = text[claim_pos:min(len(text), claim_pos + 40)]
    m = re.search(r'(?:in\s+|[\(]\s*)(20[12]\d)', after)
    if m:
        return m.group(1)

    # Look for year in parentheses right before: "(2024)" or "2024:"
    before = text[max(0, claim_pos - 25):claim_pos]
    m = re.search(r'[\(](20[12]\d)[\)]|(?:^|\s)(20[12]\d)\s*:', before)
    if m:
        return m.group(1) or m.group(2)

    # Find ALL year mentions with positions
    year_positions = [(m.start(), m.group(1)) for m in re.finditer(r'\b(20[12]\d)\b', text)]

    # Find the nearest year BEFORE the claim
    nearest_before = None
    for pos, yr in year_positions:
        if pos < claim_pos and (claim_pos - pos) < 80:
            nearest_before = yr

    # Find the nearest year AFTER the claim
    nearest_after = None
    for pos, yr in year_positions:
        if pos > claim_pos and (pos - claim_pos) < 30:
            nearest_after = yr
            break

    if nearest_after:
        return nearest_after
    if nearest_before:
        return nearest_before

    # If there's exactly one year context in the whole text, use it
    if len(set(year_mentions)) == 1:
        return year_mentions[0]

    return None


# ---------------------------------------------------------------------------
# Verification Engine
# ---------------------------------------------------------------------------

def verify_claims(claims: list[dict], ground_truth: dict) -> list[dict]:
    """Verify each claim against the ground truth data."""
    results = []
    years = ground_truth.get("years", [])

    for claim in claims:
        result = verify_single_claim(claim, ground_truth, years)
        results.append(result)

    return results


def _year_index(years: list[str], year: str | None) -> int | None:
    """Get the index for a year in the years list."""
    if year is None:
        return 0  # default to most recent
    for i, y in enumerate(years):
        if y == year:
            return i
    return None


def _get_value(ground_truth: dict, section: str, field: str, year_idx: int) -> float | None:
    """Get a value from ground truth data."""
    data = ground_truth.get(section, {})
    values = data.get(field, [])
    if year_idx is not None and year_idx < len(values):
        return values[year_idx]
    return None


def _compute_yoy_growth(values: list, year_idx: int) -> float | None:
    """Compute year-over-year growth rate."""
    if year_idx is None or year_idx + 1 >= len(values):
        return None
    curr = values[year_idx]
    prev = values[year_idx + 1]
    if curr is None or prev is None or prev == 0:
        return None
    return ((curr - prev) / abs(prev)) * 100


def verify_single_claim(claim: dict, gt: dict, years: list[str]) -> dict:
    """Verify a single claim against ground truth."""
    metric = claim["metric"]
    value = claim["value"]
    unit = claim["unit"]
    year = claim.get("year")
    year_idx = _year_index(years, year)
    unit_char = claim.get("unit_char", "")

    result = {
        **claim,
        "expected": None,
        "computed": None,
        "source": None,
        "verdict": "unverifiable",
        "deviation_pct": None,
    }

    if metric == "unknown" or metric.startswith("unknown"):
        return result

    # Skip mismatched unit-metric combos (e.g., dollar amount tagged as a ratio metric)
    ratio_only_metrics = {"interest_coverage", "times_interest_earned", "ebitda_interest_coverage",
                          "current_ratio", "quick_ratio", "cash_ratio", "debt_equity",
                          "total_debt_ebitda", "net_debt_to_ebitda", "assets_equity",
                          "pe", "pb", "pfcf", "price_cash_flow", "price_sales",
                          "ev_revenue", "ev_ebit", "asset_turnover", "fixed_asset_turnover",
                          "lt_debt_to_total_capital", "total_debt_enterprise_value"}
    if unit == "dollars" and metric in ratio_only_metrics:
        return result  # Dollar amount can't be a ratio — skip

    # P/E is a ratio (Xx), not a percentage — if unit=percent, likely misidentified
    valuation_ratio_metrics = {"pe", "pb", "pfcf", "price_cash_flow", "price_sales",
                               "ev_revenue", "ev_ebit"}
    if unit == "percent" and metric in valuation_ratio_metrics:
        return result  # Percentage can't be a valuation multiple — skip

    # Assets/equity multiplier is a ratio (Xx), not a percentage or dollar
    if metric == "assets_equity" and unit in ("percent", "dollars"):
        return result  # Equity multiplier is always expressed as a ratio

    # ROE/ROA are percentages — dollar amounts or ratios tagged as ROE/ROA are wrong
    if metric in ("roe", "roa", "roic") and unit in ("dollars", "ratio"):
        return result  # ROE can't be a dollar amount or ratio

    # EPS is a per-share value — percentages and ratios tagged as EPS are wrong
    # (EPS jumping 55%, P/E of 15.96x, etc.)
    if metric == "eps" and unit in ("percent", "ratio"):
        return result

    # ROI is a percentage — dollar amounts and ratios tagged as ROI are wrong
    if metric == "roi" and unit in ("dollars", "ratio"):
        return result

    # Convert dollar claim to millions for comparison
    claim_val_m = value
    if unit == "dollars":
        if unit_char == "B":
            claim_val_m = value * 1000  # billions to millions
        elif unit_char == "M":
            claim_val_m = value  # already millions
        elif unit_char == "K":
            claim_val_m = value / 1000

    # --- Direct ratio lookups ---
    ratio_map = {
        "gross_margin": "gross_margin",
        "ebitda_margin": "ebitda_margin",
        "operating_margin": "operating_margin",
        "pretax_margin": "pretax_margin",
        "net_margin": "net_margin",
        "roe": "return_on_equity",
        "roa": "return_on_assets",
        "roic": "return_on_invested_capital",
        "roi": "return_on_invested_capital",
        "pe": "pe",
        "pb": "price_book_value",
        "pfcf": None,  # not directly in ratios
        "price_cash_flow": "price_cash_flow",
        "price_sales": "price_sales",
        "ev_revenue": "enterprise_value_revenue",
        "ev_ebit": "enterprise_value_ebit",
        "current_ratio": "current_ratio",
        "quick_ratio": "quick_ratio",
        "debt_equity": "debt_equity",
        "total_debt_ebitda": "total_debt_ebitda",
        "net_debt_to_ebitda": "net_debt_to_ebitda",
        "lt_debt_to_total_capital": "lt_debt_to_total_capital",
        "assets_equity": "assets_equity",
        "interest_coverage": ["times_interest_earned", "ebitda_interest_expense"],
        "times_interest_earned": "times_interest_earned",
        "ebitda_interest_coverage": "ebitda_interest_expense",
        "dividend_yield": "dividend_yield",
        "fcf_yield": "fcf_yield",
        "effective_tax_rate": "effective_tax_rate",
        "cash_cycle_days": "cash_cycle_days",
    }

    # --- Latest stats lookups ---
    latest_stats_map = {
        "roe": "roe",
        "roa": "roa",
        "roi": "roi",
        "pe": "pe",
        "pb": "pb",
        "pfcf": "pfcf",
        "eps": "eps",
        "dividend_yield": "dividend_yield",
    }

    # For ratio/stat metrics, check BOTH ratios table AND latest_stats.
    # Accept whichever one matches better (the LLM might cite either source).
    if metric in ratio_map and ratio_map[metric]:
        ratio_fields = ratio_map[metric]
        if isinstance(ratio_fields, str):
            ratio_fields = [ratio_fields]
        # Try all fields, all years in ratios table, plus latest_stats
        best_verdict = None
        best_dev = float("inf")
        best_result = None

        for ratio_field in ratio_fields:
            # Check ratios for the identified year
            expected_ratio = _get_value(gt, "ratios", ratio_field, year_idx)
            if expected_ratio is not None:
                v, d = _compare(value, expected_ratio)
                if d is not None and d < best_dev:
                    best_dev = d
                    best_verdict = v
                    best_result = {
                        "expected": expected_ratio,
                        "source": f"ratios.{ratio_field}[{year or years[0] if years else '?'}]",
                    }

            # Also check ratios for ALL years (in case year assignment is wrong)
            ratio_values = gt.get("ratios", {}).get(ratio_field, [])
            for i, rv in enumerate(ratio_values):
                if rv is not None:
                    v, d = _compare(value, rv)
                    if d is not None and d < best_dev:
                        best_dev = d
                        best_verdict = v
                        yr = years[i] if i < len(years) else "?"
                        best_result = {
                            "expected": rv,
                            "source": f"ratios.{ratio_field}[{yr}]",
                        }

        # Check latest_stats
        if metric in latest_stats_map:
            stat_field = latest_stats_map[metric]
            expected_stat = gt.get("latest_stats", {}).get(stat_field)
            if expected_stat is not None:
                v, d = _compare(value, expected_stat)
                if d is not None and d < best_dev:
                    best_dev = d
                    best_verdict = v
                    best_result = {
                        "expected": expected_stat,
                        "source": f"latest_stats.{stat_field}",
                    }

        if best_result is not None:
            result["expected"] = best_result["expected"]
            result["computed"] = value
            result["source"] = best_result["source"]
            result["verdict"] = best_verdict
            result["deviation_pct"] = best_dev
            return result

    # Latest stats only (metrics not in ratio_map)
    if metric in latest_stats_map:
        stat_field = latest_stats_map[metric]
        expected = gt.get("latest_stats", {}).get(stat_field)
        if expected is not None:
            result["expected"] = expected
            result["computed"] = value
            result["source"] = f"latest_stats.{stat_field}"
            result["verdict"], result["deviation_pct"] = _compare(value, expected)
            return result

    # --- Direct financial statement lookups (dollar amounts) ---
    if unit == "dollars":
        stmt_lookups = {
            "revenue": ("income_statement", "revenue"),
            "total_revenue": ("income_statement", "total_revenue"),
            "gross_profit": ("income_statement", "gross_profit"),
            "operating_income": ("income_statement", "operating_income"),
            "net_income": ("income_statement", "net_income"),
            "interest_expense": ("income_statement", "interest_expense_net_non_operating"),
            "operating_cash_flow": ("cash_flow", "cash_from_operating_activities"),
            "capex": ("cash_flow", "capital_expenditures"),
            "free_cash_flow": ("cash_flow", None),  # computed
            "total_assets": ("balance_sheet", "total_assets"),
            "total_equity": ("balance_sheet", "total_equity"),
            "total_liabilities": ("balance_sheet", "total_liabilities"),
            "total_debt": ("balance_sheet", "total_debt"),
            "total_long_term_debt": ("balance_sheet", "total_long_term_debt"),
            "cash": ("balance_sheet", "cash_and_short_term_investments"),
            "total_current_assets": ("balance_sheet", "total_current_assets"),
            "total_current_liabilities": ("balance_sheet", "total_current_liabilities"),
            "goodwill": ("balance_sheet", "goodwill_net"),
            "accounts_receivable": ("balance_sheet", "accounts_receivable_trade_net"),
            "inventory": ("balance_sheet", "total_inventory"),
            "dividends_paid": ("cash_flow", "total_cash_dividends_paid"),
            "share_repurchase": ("cash_flow", "issuance_retirement_of_stock_net"),
            "depreciation": ("cash_flow", "depreciation_depletion"),
            "provision_for_income_taxes": ("income_statement", "provision_for_income_taxes"),
            "shares_outstanding": ("balance_sheet", "total_common_shares_outstanding"),
            "eps": ("income_statement", "basic_eps_excluding_extraordinary_items"),
            "cost_of_revenue": ("income_statement", "cost_of_revenue_total"),
            "accrued_expenses": ("balance_sheet", "accrued_expenses"),
            "accounts_payable": ("balance_sheet", "accounts_payable"),
            "net_income_before_taxes": ("income_statement", "net_income_before_taxes"),
            "unusual_expense": ("income_statement", "unusual_expense_income"),
            "ppe": ("balance_sheet", "property_plant_equipment_total_net"),
            "operating_expense": ("income_statement", "total_operating_expense"),
            "other_liabilities": ("balance_sheet", "other_liabilities_total"),
            "intangibles": ("balance_sheet", "intangibles_net"),
        }

        if metric in stmt_lookups:
            section, field = stmt_lookups[metric]
            if field is not None:
                # Check identified year first, then all years (best match)
                best_verdict = None
                best_dev = float("inf")
                best_result = None

                all_values = gt.get(section, {}).get(field, [])
                # Check identified year first
                expected = _get_value(gt, section, field, year_idx)
                if expected is not None:
                    v, d = _compare(abs(claim_val_m), abs(expected))
                    if d is not None and d < best_dev:
                        best_dev = d
                        best_verdict = v
                        best_result = {"expected": expected,
                                       "source": f"{section}.{field}[{year or years[0] if years else '?'}]"}

                # Also check all years to handle year-assignment errors
                for i, val in enumerate(all_values):
                    if val is not None:
                        v, d = _compare(abs(claim_val_m), abs(val))
                        if d is not None and d < best_dev:
                            best_dev = d
                            best_verdict = v
                            yr = years[i] if i < len(years) else "?"
                            best_result = {"expected": val,
                                           "source": f"{section}.{field}[{yr}]"}

                if best_result is not None:
                    result["expected"] = best_result["expected"]
                    result["computed"] = claim_val_m
                    result["source"] = best_result["source"]
                    result["verdict"] = best_verdict
                    result["deviation_pct"] = best_dev
                    return result

    # --- EBITDA (computed: operating_income + depreciation) ---
    if metric == "ebitda" and unit == "dollars":
        oi = gt.get("income_statement", {}).get("operating_income", [])
        dep = gt.get("cash_flow", {}).get("depreciation_depletion", [])
        best_v, best_d, best_src, best_exp = None, float("inf"), None, None
        for i in range(min(len(oi), len(dep))):
            if oi[i] is not None and dep[i] is not None:
                ebitda = oi[i] + dep[i]
                v, d = _compare(abs(claim_val_m), abs(ebitda))
                if d is not None and d < best_d:
                    best_d = d
                    best_v = v
                    best_exp = ebitda
                    yr = years[i] if i < len(years) else "?"
                    best_src = f"OI[{yr}]+depreciation[{yr}] = {oi[i]}+{dep[i]}"
        if best_src is not None:
            result["expected"] = best_exp
            result["computed"] = claim_val_m
            result["source"] = best_src
            result["verdict"] = best_v
            result["deviation_pct"] = best_d
            return result

    # --- Free cash flow (computed: OCF - capex) ---
    if metric == "free_cash_flow" and unit == "dollars":
        ocf = gt.get("cash_flow", {}).get("cash_from_operating_activities", [])
        capex = gt.get("cash_flow", {}).get("capital_expenditures", [])
        best_v, best_d, best_src, best_exp = None, float("inf"), None, None
        for i in range(min(len(ocf), len(capex))):
            if ocf[i] is not None and capex[i] is not None:
                fcf = ocf[i] + capex[i]  # capex is negative
                v, d = _compare(abs(claim_val_m), abs(fcf))
                if d is not None and d < best_d:
                    best_d = d
                    best_v = v
                    best_exp = fcf
                    yr = years[i] if i < len(years) else "?"
                    best_src = f"OCF[{yr}]+capex[{yr}] = {ocf[i]}+{capex[i]}"
        if best_src is not None:
            result["expected"] = best_exp
            result["computed"] = claim_val_m
            result["source"] = best_src
            result["verdict"] = best_v
            result["deviation_pct"] = best_d
            return result

    # --- Computed metrics ---

    # CAGR metrics — check against growth_rates from prompt
    if metric in ("revenue_growth_cagr", "revenue_cagr") and unit == "percent":
        expected = gt.get("growth_rates", {}).get("revenue_per_share_5yr_cagr")
        if expected is not None:
            result["expected"] = expected
            result["computed"] = value
            result["source"] = "growth_rates.revenue_per_share_5yr_cagr"
            result["verdict"], result["deviation_pct"] = _compare(value, expected)
            return result

    if metric in ("net_income_growth_cagr", "eps_growth_cagr") and unit == "percent":
        expected = gt.get("growth_rates", {}).get("net_income_per_share_5yr_cagr")
        if expected is not None:
            result["expected"] = expected
            result["computed"] = value
            result["source"] = "growth_rates.net_income_per_share_5yr_cagr"
            result["verdict"], result["deviation_pct"] = _compare(value, expected)
            return result

    # Revenue growth
    if metric == "revenue_growth" and unit == "percent":
        rev = gt.get("income_statement", {}).get("revenue", [])
        if year_idx is not None:
            growth = _compute_yoy_growth(rev, year_idx)
            if growth is not None:
                result["expected"] = round(growth, 2)
                result["computed"] = value
                idx0 = year_idx
                idx1 = year_idx + 1
                y0 = years[idx0] if idx0 < len(years) else "?"
                y1 = years[idx1] if idx1 < len(years) else "?"
                result["source"] = f"(revenue[{y0}]-revenue[{y1}])/revenue[{y1}] = ({rev[idx0]}-{rev[idx1]})/{rev[idx1]}"
                result["verdict"], result["deviation_pct"] = _compare(value, growth)
                return result

    # Net income growth
    if metric == "net_income_growth" and unit == "percent":
        ni = gt.get("income_statement", {}).get("net_income", [])
        if year_idx is not None:
            growth = _compute_yoy_growth(ni, year_idx)
            if growth is not None:
                result["expected"] = round(growth, 2)
                result["computed"] = value
                result["source"] = f"net_income YoY growth"
                result["verdict"], result["deviation_pct"] = _compare(value, growth)
                return result

    # EPS growth
    if metric == "eps_growth" and unit == "percent":
        eps_vals = gt.get("income_statement", {}).get("basic_eps_excluding_extraordinary_items", [])
        if year_idx is not None:
            growth = _compute_yoy_growth(eps_vals, year_idx)
            if growth is not None:
                result["expected"] = round(growth, 2)
                result["computed"] = value
                result["source"] = f"EPS YoY growth"
                result["verdict"], result["deviation_pct"] = _compare(value, growth)
                return result

    # Gross margin computation
    if metric == "gross_margin" and unit == "percent":
        gp = gt.get("income_statement", {}).get("gross_profit", [])
        rev = gt.get("income_statement", {}).get("revenue", [])
        if year_idx is not None and year_idx < len(gp) and year_idx < len(rev):
            if gp[year_idx] is not None and rev[year_idx] is not None and rev[year_idx] != 0:
                computed = (gp[year_idx] / rev[year_idx]) * 100
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"gross_profit[{year}]/revenue[{year}] = {gp[year_idx]}/{rev[year_idx]}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # Operating margin computation
    if metric == "operating_margin" and unit == "percent":
        oi = gt.get("income_statement", {}).get("operating_income", [])
        rev = gt.get("income_statement", {}).get("revenue", [])
        if year_idx is not None and year_idx < len(oi) and year_idx < len(rev):
            if oi[year_idx] is not None and rev[year_idx] is not None and rev[year_idx] != 0:
                computed = (oi[year_idx] / rev[year_idx]) * 100
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"operating_income/revenue = {oi[year_idx]}/{rev[year_idx]}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # Net margin computation
    if metric == "net_margin" and unit == "percent":
        ni = gt.get("income_statement", {}).get("net_income", [])
        rev = gt.get("income_statement", {}).get("revenue", [])
        if year_idx is not None and year_idx < len(ni) and year_idx < len(rev):
            if ni[year_idx] is not None and rev[year_idx] is not None and rev[year_idx] != 0:
                computed = (ni[year_idx] / rev[year_idx]) * 100
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"net_income/revenue = {ni[year_idx]}/{rev[year_idx]}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # Asset turnover
    if metric == "asset_turnover" and unit == "ratio":
        rev = gt.get("income_statement", {}).get("revenue", [])
        ta = gt.get("balance_sheet", {}).get("total_assets", [])
        if year_idx is not None and year_idx < len(rev) and year_idx < len(ta):
            if rev[year_idx] is not None and ta[year_idx] is not None and ta[year_idx] != 0:
                computed = rev[year_idx] / ta[year_idx]
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"revenue/total_assets = {rev[year_idx]}/{ta[year_idx]}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # Interest coverage from statements
    if metric in ("interest_coverage", "times_interest_earned") and unit == "ratio":
        oi = gt.get("income_statement", {}).get("operating_income", [])
        ie = gt.get("income_statement", {}).get("interest_expense_net_non_operating", [])
        if year_idx is not None and year_idx < len(oi) and year_idx < len(ie):
            if oi[year_idx] is not None and ie[year_idx] is not None and ie[year_idx] != 0:
                computed = oi[year_idx] / abs(ie[year_idx])
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"operating_income/|interest_expense| = {oi[year_idx]}/{abs(ie[year_idx])}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # Debt-to-equity from statements
    if metric == "debt_equity" and unit == "ratio":
        td = gt.get("balance_sheet", {}).get("total_debt", [])
        eq = gt.get("balance_sheet", {}).get("total_equity", [])
        if year_idx is not None and year_idx < len(td) and year_idx < len(eq):
            if td[year_idx] is not None and eq[year_idx] is not None and eq[year_idx] != 0:
                computed = td[year_idx] / eq[year_idx]
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"total_debt/total_equity = {td[year_idx]}/{eq[year_idx]}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # Equity multiplier / assets-to-equity
    if metric == "assets_equity" and unit == "ratio":
        ta = gt.get("balance_sheet", {}).get("total_assets", [])
        eq = gt.get("balance_sheet", {}).get("total_equity", [])
        if year_idx is not None and year_idx < len(ta) and year_idx < len(eq):
            if ta[year_idx] is not None and eq[year_idx] is not None and eq[year_idx] != 0:
                computed = ta[year_idx] / eq[year_idx]
                result["expected"] = round(computed, 2)
                result["computed"] = value
                result["source"] = f"total_assets/total_equity = {ta[year_idx]}/{eq[year_idx]}"
                result["verdict"], result["deviation_pct"] = _compare(value, computed)
                return result

    # EPS (non-dollar context, just the number)
    if metric == "eps" and unit != "dollars":
        eps_vals = gt.get("income_statement", {}).get("basic_eps_excluding_extraordinary_items", [])
        if year_idx is not None and year_idx < len(eps_vals):
            expected = eps_vals[year_idx]
            if expected is not None:
                result["expected"] = expected
                result["computed"] = value
                result["source"] = f"income_statement.basic_eps[{year or years[0] if years else '?'}]"
                result["verdict"], result["deviation_pct"] = _compare(value, expected)
                return result
        # Also check latest stats
        expected = gt.get("latest_stats", {}).get("eps")
        if expected is not None:
            result["expected"] = expected
            result["computed"] = value
            result["source"] = "latest_stats.eps"
            result["verdict"], result["deviation_pct"] = _compare(value, expected)
            return result

    return result


def _compare(claimed: float, expected: float) -> tuple[str, float | None]:
    """Compare two values and return verdict + deviation percentage."""
    if expected == 0:
        if claimed == 0:
            return "correct", 0.0
        return "error", 100.0

    deviation = abs(claimed - expected) / abs(expected)
    dev_pct = round(deviation * 100, 2)

    if deviation <= TOLERANCE_OK:
        return "correct", dev_pct
    elif deviation <= TOLERANCE_WARN:
        return "warning", dev_pct
    else:
        return "error", dev_pct


# ---------------------------------------------------------------------------
# Main Evaluation Pipeline
# ---------------------------------------------------------------------------

def fetch_samples(n: int) -> list[dict]:
    """Fetch N random samples from Supabase via MCP CLI."""
    # We'll use the Supabase MCP tool via subprocess calling claude
    # Instead, use direct SQL via supabase REST API
    # Actually, we need to fetch from Supabase - let's write a helper that
    # reads from a pre-fetched file or fetches via SQL
    sample_file = RUNS_DIR / "current_sample.json"
    if sample_file.exists():
        with open(sample_file) as f:
            data = json.load(f)
            if len(data) >= n:
                return data[:n]

    print(f"ERROR: No sample data found. Please run fetch_samples.py first.")
    sys.exit(1)


def evaluate_row(row: dict) -> dict:
    """Evaluate a single prompt-response pair."""
    row_id = row.get("id", "unknown")
    prompt = row.get("prompt", "")
    response = row.get("response", "")

    # Step 1: Parse prompt (ground truth)
    ground_truth = parse_prompt(prompt)
    symbol = ground_truth.get("company", "???")

    # Step 2: Parse response and extract claims
    response_json = extract_response_json(response)
    if response_json is None:
        return {
            "id": row_id,
            "symbol": symbol,
            "parse_error": True,
            "error_msg": "Failed to parse response JSON",
            "total_claims": 0,
            "verified_correct": 0,
            "errors": 0,
            "warnings": 0,
            "unverifiable": 0,
            "accuracy_pct": None,
            "error_details": [],
        }

    claims = extract_claims(response_json)

    # Step 3: Verify claims
    verified = verify_claims(claims, ground_truth)

    # Step 4: Aggregate
    correct = sum(1 for v in verified if v["verdict"] == "correct")
    errors = sum(1 for v in verified if v["verdict"] == "error")
    warnings = sum(1 for v in verified if v["verdict"] == "warning")
    unverifiable = sum(1 for v in verified if v["verdict"] == "unverifiable")
    total = len(verified)

    verifiable = correct + errors + warnings
    accuracy = (correct / verifiable * 100) if verifiable > 0 else None

    return {
        "id": row_id,
        "symbol": symbol,
        "parse_error": False,
        "total_claims": total,
        "verified_correct": correct,
        "errors": errors,
        "warnings": warnings,
        "unverifiable": unverifiable,
        "accuracy_pct": round(accuracy, 1) if accuracy is not None else None,
        "error_details": verified,
        "ground_truth_summary": {
            "years": ground_truth.get("years", []),
            "has_income_stmt": bool(ground_truth.get("income_statement")),
            "has_balance_sheet": bool(ground_truth.get("balance_sheet")),
            "has_cash_flow": bool(ground_truth.get("cash_flow")),
            "has_ratios": bool(ground_truth.get("ratios")),
        },
    }


def run_evaluation(sample_file: str, sample_size: int) -> dict:
    """Run full evaluation on a sample."""
    with open(sample_file) as f:
        rows = json.load(f)

    rows = rows[:sample_size]
    print(f"Evaluating {len(rows)} rows...")

    results = []
    for i, row in enumerate(rows):
        print(f"  [{i+1}/{len(rows)}] Evaluating {row.get('id', '?')[:8]}...", end=" ")
        result = evaluate_row(row)
        print(f"{result['symbol']} — {result['total_claims']} claims, "
              f"{result['verified_correct']} correct, {result['errors']} errors, "
              f"{result['unverifiable']} unverifiable")
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

    summary = {
        "timestamp": datetime.datetime.now().isoformat(),
        "sample_size": len(rows),
        "total_claims": total_claims,
        "total_correct": total_correct,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "total_unverifiable": total_unverifiable,
        "parse_errors": parse_errors,
        "overall_accuracy_pct": round(overall_accuracy, 1) if overall_accuracy else None,
        "avg_claims_per_response": round(total_claims / len(rows), 1) if rows else 0,
        "verification_rate_pct": round(verifiable / total_claims * 100, 1) if total_claims > 0 else 0,
        "results": results,
    }

    # Error categorization
    error_metrics = {}
    high_confidence_errors = 0  # errors with 2-20% deviation (likely real)
    low_confidence_errors = 0   # errors with >50% deviation (likely FP)
    for r in results:
        for d in r["error_details"]:
            if d["verdict"] == "error":
                m = d.get("metric", "unknown")
                error_metrics[m] = error_metrics.get(m, 0) + 1
                dev = d.get("deviation_pct", 0)
                if dev <= 20:
                    high_confidence_errors += 1
                elif dev > 50:
                    low_confidence_errors += 1
    summary["errors_by_metric"] = dict(sorted(error_metrics.items(), key=lambda x: -x[1]))
    summary["high_confidence_errors"] = high_confidence_errors
    summary["low_confidence_errors"] = low_confidence_errors
    hc_accuracy = ((total_correct + low_confidence_errors) /
                   (verifiable) * 100) if verifiable > 0 else None
    summary["high_confidence_accuracy_pct"] = round(hc_accuracy, 1) if hc_accuracy else None

    return summary


def save_run(summary: dict, sample_size: int):
    """Save run results to files."""
    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = RUNS_DIR / f"{ts}_n{sample_size}.json"

    with open(run_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nRun saved: {run_file}")

    # Append to results.tsv
    if not RESULTS_TSV.exists():
        with open(RESULTS_TSV, "w") as f:
            f.write("timestamp\tsample_size\ttotal_claims\tverified_correct\terrors\twarnings\tunverifiable\taccuracy_pct\thc_accuracy_pct\tverification_rate\tavg_claims\thc_errors\tlc_errors\n")

    with open(RESULTS_TSV, "a") as f:
        f.write(f"{summary['timestamp']}\t{summary['sample_size']}\t{summary['total_claims']}\t"
                f"{summary['total_correct']}\t{summary['total_errors']}\t{summary['total_warnings']}\t"
                f"{summary['total_unverifiable']}\t{summary['overall_accuracy_pct']}\t"
                f"{summary.get('high_confidence_accuracy_pct', '')}\t"
                f"{summary['verification_rate_pct']}\t{summary['avg_claims_per_response']}\t"
                f"{summary.get('high_confidence_errors', '')}\t{summary.get('low_confidence_errors', '')}\n")

    return run_file


def print_summary(summary: dict):
    """Print summary to stdout."""
    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY")
    print("=" * 60)
    print(f"sample_size: {summary['sample_size']}")
    print(f"total_claims: {summary['total_claims']}")
    print(f"avg_claims_per_response: {summary['avg_claims_per_response']}")
    print(f"verification_rate: {summary['verification_rate_pct']}%")
    print(f"verified_correct: {summary['total_correct']}")
    print(f"errors: {summary['total_errors']}")
    print(f"warnings: {summary['total_warnings']}")
    print(f"unverifiable: {summary['total_unverifiable']}")
    print(f"parse_errors: {summary['parse_errors']}")
    print(f"overall_accuracy: {summary['overall_accuracy_pct']}%")
    print(f"high_confidence_errors: {summary.get('high_confidence_errors', '?')} (2-20% dev, likely real)")
    print(f"low_confidence_errors: {summary.get('low_confidence_errors', '?')} (>50% dev, likely FP)")
    print(f"high_confidence_accuracy: {summary.get('high_confidence_accuracy_pct', '?')}%")
    if summary.get("errors_by_metric"):
        print(f"\nTop error metrics:")
        for metric, count in list(summary["errors_by_metric"].items())[:10]:
            print(f"  {metric}: {count}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Financial Analysis Accuracy Evaluator")
    parser.add_argument("--sample-file", required=True, help="Path to sample JSON file")
    parser.add_argument("--sample-size", type=int, default=30, help="Number of rows to evaluate")
    args = parser.parse_args()

    summary = run_evaluation(args.sample_file, args.sample_size)
    print_summary(summary)
    run_file = save_run(summary, args.sample_size)
