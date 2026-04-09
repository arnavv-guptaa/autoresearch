#!/usr/bin/env python3
"""Generate error_trends.png chart from evaluation run data."""

import json
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

RUNS_DIR = Path(__file__).parent / 'runs'

with open(RUNS_DIR / '20260408_141712_n893.json') as f:
    data = json.load(f)
with open(RUNS_DIR / 'full_sample.json') as f:
    sample = json.load(f)

id_to_prompt = {r['id']: r.get('prompt', '') for r in sample}

# ── Gather monthly stats ──
month_stats = defaultdict(lambda: {
    "correct": 0, "errors": 0, "hc_errors": 0, "warnings": 0,
    "unverifiable": 0, "count": 0, "claims": 0
})

for r in data['results']:
    prompt = id_to_prompt.get(r['id'], '')
    m = re.search(r'as of today \((\d{4}-\d{2})', prompt)
    month = m.group(1) if m else None
    if not month:
        continue
    s = month_stats[month]
    s["correct"]      += r['verified_correct']
    s["errors"]       += r['errors']
    s["warnings"]     += r['warnings']
    s["unverifiable"] += r['unverifiable']
    s["count"]        += 1
    s["claims"]       += r['total_claims']
    for d in r['error_details']:
        if d['verdict'] == 'error' and d.get('deviation_pct', 100) <= 20:
            s["hc_errors"] += 1

months = sorted(month_stats.keys())
labels = [m.replace("2025-", "'25-").replace("2026-", "'26-") for m in months]

raw_acc     = []
hc_err_rate = []
n_reports   = []
n_claims    = []
for m in months:
    s = month_stats[m]
    v = s["correct"] + s["errors"] + s["warnings"]
    raw_acc.append(s["correct"] / v * 100 if v else 0)
    hc_err_rate.append(s["hc_errors"] / v * 100 if v else 0)
    n_reports.append(s["count"])
    n_claims.append(s["claims"])

# ── Also gather per-metric error counts by month ──
metric_month = defaultdict(lambda: defaultdict(int))
for r in data['results']:
    prompt = id_to_prompt.get(r['id'], '')
    m_search = re.search(r'as of today \((\d{4}-\d{2})', prompt)
    month = m_search.group(1) if m_search else None
    if not month:
        continue
    for d in r['error_details']:
        if d['verdict'] == 'error' and d.get('deviation_pct', 100) <= 20:
            metric_month[d['metric']][month] += 1

top_metrics = ['asset_turnover', 'roe', 'net_margin', 'revenue_growth', 'operating_margin']

# ── Build figure ──
fig, axes = plt.subplots(3, 1, figsize=(12, 14), gridspec_kw={'height_ratios': [3, 2.5, 2]})
fig.suptitle('Financial Analysis Accuracy — Error Trends\nSep 2025 – Apr 2026',
             fontsize=15, fontweight='bold', y=0.99)

# ── Panel 1: Accuracy & HC error rate ──
ax1 = axes[0]
color_acc = '#2563eb'
color_hc  = '#dc2626'

bar_x = range(len(months))
bars = ax1.bar(bar_x, n_reports, color='#e5e7eb', zorder=1, label='Reports (right)')
ax1r = ax1.twinx()

ln1 = ax1.plot(bar_x, raw_acc, '-o', color=color_acc, linewidth=2.5, markersize=8, zorder=3, label='Raw Accuracy')
ln2 = ax1.plot(bar_x, [100 - r for r in hc_err_rate], '-s', color='#16a34a', linewidth=2.5, markersize=8, zorder=3, label='HC Accuracy (est.)')
ln3 = ax1r.plot(bar_x, hc_err_rate, '--^', color=color_hc, linewidth=2, markersize=7, zorder=3, label='HC Error Rate')

ax1.set_ylim(55, 100)
ax1r.set_ylim(0, 22)
ax1.set_ylabel('Accuracy %', fontsize=12)
ax1r.set_ylabel('HC Error Rate %', color=color_hc, fontsize=12)
ax1r.tick_params(axis='y', labelcolor=color_hc)
ax1.set_xticks(bar_x)
ax1.set_xticklabels(labels, fontsize=11)
ax1.set_title('Overall Accuracy & High-Confidence Error Rate by Month', fontsize=13, pad=4)

# Annotation for the big improvement
ax1.annotate('Prompt\nimproved', xy=(2, raw_acc[2]), xytext=(1.2, 72),
             fontsize=9, ha='center', color='#666',
             arrowprops=dict(arrowstyle='->', color='#999'))

# Combined legend
lns = ln1 + ln2 + ln3
labs = [l.get_label() for l in lns]
ax1.legend(lns, labs, loc='lower right', fontsize=10, framealpha=0.9)

ax1.grid(axis='y', alpha=0.3)

# ── Panel 2: HC errors by metric over time ──
ax2 = axes[1]
colors = ['#ef4444', '#f59e0b', '#3b82f6', '#10b981', '#8b5cf6']
for i, metric in enumerate(top_metrics):
    vals = [metric_month[metric].get(m, 0) for m in months]
    # Normalize per 1000 verified claims
    norm_vals = []
    for j, m in enumerate(months):
        s = month_stats[m]
        v = s["correct"] + s["errors"] + s["warnings"]
        norm_vals.append(vals[j] / v * 1000 if v else 0)
    ax2.plot(bar_x, norm_vals, '-o', color=colors[i], linewidth=2, markersize=6, label=metric.replace('_', ' ').title())

ax2.set_ylabel('HC Errors per 1K Verified Claims', fontsize=12)
ax2.set_xticks(bar_x)
ax2.set_xticklabels(labels, fontsize=11)
ax2.set_title('Top Error Metrics — HC Error Rate by Month (normalized)', fontsize=13, pad=10)
ax2.legend(fontsize=10, ncol=3, loc='upper right', framealpha=0.9)
ax2.grid(axis='y', alpha=0.3)
ax2.set_ylim(bottom=0)

# ── Panel 3: Error composition stacked bar ──
ax3 = axes[2]
correct_pct = []
warning_pct = []
hc_pct = []
lc_pct = []
unv_pct = []

for m in months:
    s = month_stats[m]
    total = s["claims"]
    if total == 0:
        correct_pct.append(0); warning_pct.append(0); hc_pct.append(0); lc_pct.append(0); unv_pct.append(0)
        continue
    lc = s["errors"] - s["hc_errors"]
    correct_pct.append(s["correct"] / total * 100)
    warning_pct.append(s["warnings"] / total * 100)
    hc_pct.append(s["hc_errors"] / total * 100)
    lc_pct.append(max(0, lc) / total * 100)
    unv_pct.append(s["unverifiable"] / total * 100)

w = 0.6
ax3.bar(bar_x, correct_pct, w, label='Correct', color='#22c55e')
ax3.bar(bar_x, warning_pct, w, bottom=correct_pct, label='Warning', color='#facc15')
ax3.bar(bar_x, hc_pct, w, bottom=[c+w_ for c, w_ in zip(correct_pct, warning_pct)], label='HC Error (likely real)', color='#ef4444')
ax3.bar(bar_x, lc_pct, w, bottom=[c+w_+h for c, w_, h in zip(correct_pct, warning_pct, hc_pct)], label='LC Error (likely FP)', color='#fca5a5')
ax3.bar(bar_x, unv_pct, w, bottom=[c+w_+h+l for c, w_, h, l in zip(correct_pct, warning_pct, hc_pct, lc_pct)], label='Unverifiable', color='#d1d5db')

ax3.set_ylabel('% of All Claims', fontsize=12)
ax3.set_xticks(bar_x)
ax3.set_xticklabels(labels, fontsize=11)
ax3.set_title('Claim Verdict Composition by Month', fontsize=13, pad=10)
ax3.legend(fontsize=9, ncol=5, loc='upper center', bbox_to_anchor=(0.5, -0.12), framealpha=0.9)
ax3.set_ylim(0, 105)

plt.tight_layout(rect=[0, 0.02, 1, 1.0])
fig.subplots_adjust(top=0.92)
plt.savefig(RUNS_DIR / 'error_trends.png', dpi=150, bbox_inches='tight', facecolor='white')
print("Saved to runs/error_trends.png")
