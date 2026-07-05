"""
SendSmart Report Generator.

Fetches email data, runs the analyzer, and generates index.html —
a GitHub Pages dashboard with performance metrics, benchmark comparisons,
and playbook insights per content type.
"""

import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import anthropic
from dotenv import load_dotenv

from analyzer import build_playbook
from hubspot_client import EmailRecord, fetch_emails

try:
    from pipeline import analyze_campaign_pipeline
    _PIPELINE_IMPORT_OK = True
except ImportError:
    _PIPELINE_IMPORT_OK = False

load_dotenv()

# B2B SaaS email benchmarks (industry averages)
BENCHMARKS = {
    "open_rate": 0.35,
    "click_rate": 0.03,
    "ctor": 0.10,
}

BENCHMARK_LABELS = {
    "open_rate": "35%",
    "click_rate": "3%",
    "ctor": "10%",
}

AI_BENCHMARKS = {
    "open_rate":        ("Open Rate",        0.19,  False),
    "click_rate":       ("Click Rate",       None,  False),
    "ctor":             ("CTOR",             0.075, False),
    "delivered_rate":   ("Delivered Rate",   None,  False),
    "unsubscribe_rate": ("Unsubscribe Rate", 0.005, True),
    "bounce_rate":      ("Bounce Rate",      0.0025,True),
}

# Pipeline Association covers every campaign sent on/after this fixed date —
# not a rolling window. Bump manually if the program's start date changes.
PIPELINE_START_DATE = "2026-01-01"


def _generate_ai_summary(label: str, metrics: dict, prior: Optional[dict]) -> dict:
    """Call Claude to generate a 2-3 sentence summary + 2-3 bullets for a metric set."""
    client = anthropic.Anthropic()

    def fmt(key):
        val = metrics.get(key, 0)
        bm_label, bm_val, invert = AI_BENCHMARKS[key]
        line = f"  {bm_label}: {val:.1%}"
        if bm_val is not None:
            direction = "below" if val < bm_val else "above"
            line += f" (benchmark {bm_val:.2%}, {direction})"
        if prior:
            delta = val - prior.get(key, 0)
            arrow = "↑" if delta >= 0 else "↓"
            line += f" | {arrow}{abs(delta):.1%} vs prev period"
        return line

    metrics_text = "\n".join([
        fmt("delivered_rate"),
        fmt("open_rate"),
        fmt("click_rate"),
        fmt("ctor"),
        fmt("unsubscribe_rate"),
        fmt("bounce_rate"),
    ])

    prompt = f"""You are analyzing email performance data for Medallion, a B2B SaaS company.
Segment: {label}
Sample: {metrics.get('count', 0)} emails, {metrics.get('sent', 0):,} total sent

Metrics (with benchmark and period-over-period delta where available):
{metrics_text}

Write a performance summary with this exact JSON structure:
{{
  "summary": "2-3 sentences. Reference actual percentages. State plainly if metrics are behind benchmark — do not soften. Note any interesting relationships between metrics (e.g. high delivered but low open suggests subject line issue, not deliverability). Be direct and specific.",
  "recommendations": [
    "Specific, actionable recommendation based directly on the numbers above — not generic advice",
    "Second recommendation",
    "Third recommendation (only include if genuinely supported by the data)"
  ]
}}

Rules:
- Use the actual numbers in every sentence
- If a metric has no benchmark listed, do not invent one
- Recommendations must reference specific metrics from the data
- 2-3 sentences for summary, 2-3 bullets max
- Return ONLY valid JSON, no markdown fences"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        output_config={"format": {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "recommendations": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["summary", "recommendations"],
            "additionalProperties": False,
        }}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _delta_class(actual: float, benchmark: float) -> str:
    if actual >= benchmark * 1.1:
        return "good"
    if actual >= benchmark * 0.85:
        return "ok"
    return "bad"


def _trend_label(current: float, prior: float) -> str:
    if prior == 0:
        return ""
    delta = current - prior
    arrow = "↑" if delta > 0 else "↓"
    return f"{arrow} {abs(delta):.1%}"


def _trend_class(current: float, prior: float) -> str:
    if prior == 0:
        return ""
    return "trend-up" if current >= prior else "trend-down"


def _group_by_type(emails: list[EmailRecord]) -> dict[str, list[EmailRecord]]:
    groups: dict[str, list[EmailRecord]] = defaultdict(list)
    for e in emails:
        key = e.content_type or "unknown"
        groups[key].append(e)
    return dict(groups)


def _aggregate(emails: list[EmailRecord]) -> dict:
    sent = sum(e.sent for e in emails)
    delivered = sum(e.delivered for e in emails)
    opens = sum(e.unique_opens for e in emails)
    clicks = sum(e.unique_clicks for e in emails)
    bounced = sum(e.bounced for e in emails)
    unsubscribed = sum(e.unsubscribed for e in emails)
    open_rate = opens / delivered if delivered else 0
    click_rate = clicks / delivered if delivered else 0
    ctor = clicks / opens if opens else 0
    bounce_rate = bounced / sent if sent else 0
    unsubscribe_rate = unsubscribed / delivered if delivered else 0
    delivered_rate = delivered / sent if sent else 0
    return {
        "count": len(emails),
        "sent": sent,
        "delivered": delivered,
        "open_rate": open_rate,
        "click_rate": click_rate,
        "ctor": ctor,
        "bounce_rate": bounce_rate,
        "unsubscribe_rate": unsubscribe_rate,
        "delivered_rate": delivered_rate,
    }


def _emails_to_json(emails: list[EmailRecord]) -> str:
    records = []
    for e in emails:
        records.append({
            "id": e.email_id,
            "name": e.name,
            "subject": e.subject,
            "content_type": e.content_type or "unknown",
            "campaign_name": e.campaign_name or "",
            "send_date": e.send_date.strftime("%Y-%m-%d") if e.send_date else None,
            "sent": e.sent,
            "delivered": e.delivered,
            "opens": e.unique_opens,
            "clicks": e.unique_clicks,
            "bounced": e.bounced,
            "unsubscribed": e.unsubscribed,
        })
    return json.dumps(records)


def _top_emails(emails: list[EmailRecord], n: int = 5) -> list[EmailRecord]:
    # Only include emails with meaningful send volume
    valid = [e for e in emails if e.delivered >= 50]
    return sorted(valid, key=lambda e: e.open_rate, reverse=True)[:n]


def _build_summary_rows(
    current_groups: dict[str, list[EmailRecord]],
    prior_groups: dict[str, list[EmailRecord]],
) -> list[dict]:
    rows = []
    for ct, emails in sorted(current_groups.items(), key=lambda x: -len(x[1])):
        if ct == "unknown":
            continue
        cur = _aggregate(emails)
        prior_emails = prior_groups.get(ct, [])
        pri = _aggregate(prior_emails) if prior_emails else None
        rows.append({"type": ct, "current": cur, "prior": pri})
    return rows


def _render_html(
    all_emails: list[EmailRecord],
    rows: list[dict],
    playbook: dict[str, dict],
    current_groups: dict[str, list[EmailRecord]],
    prior_groups: dict[str, list[EmailRecord]],
    ai_summaries: dict[str, dict],
    generated_at: datetime,
    pipeline_data: Optional[dict] = None,
    pipeline_start_date: str = PIPELINE_START_DATE,
) -> str:
    now_str = generated_at.strftime("%B %d, %Y at %I:%M %p UTC")

    # Embed all emails as JSON for client-side filtering
    emails_json = _emails_to_json(all_emails)

    # Build playbook panels (static — not affected by filters)
    playbook_panels = ""
    first = True
    all_types = sorted(playbook.keys())

    for ct in all_types:
        data = playbook[ct]
        anchor = ct.replace(" ", "-")
        emails = current_groups.get(ct, [])
        top = _top_emails(emails)
        hidden = '' if first else 'style="display:none"'
        first = False

        if "status" in data:
            count = data.get("sample_count", 0)
            minimum = data.get("minimum_required", 5)
            playbook_panels += f"""
        <div id="panel-{anchor}" class="playbook-panel" {hidden}>
            <p class="insufficient-note">⚠ Insufficient data ({count} emails, need {minimum}+ for reliable analysis)</p>
        </div>"""
            continue

        top_emails_html = ""
        for e in top:
            top_emails_html += f"""
                <tr>
                    <td>{e.subject}</td>
                    <td>{e.sent:,}</td>
                    <td><span class="pct-pill {_delta_class(e.open_rate, BENCHMARKS['open_rate'])}">{_pct(e.open_rate)}</span></td>
                    <td><span class="pct-pill {_delta_class(e.click_rate, BENCHMARKS['click_rate'])}">{_pct(e.click_rate)}</span></td>
                </tr>"""
        if not top_emails_html:
            top_emails_html = '<tr><td colspan="4">No emails with 50+ recipients</td></tr>'

        playbook_panels += f"""
        <div id="panel-{anchor}" class="playbook-panel" {hidden}>
            <div class="insights-grid">
                <div class="insight-card">
                    <h3>Subject Line Patterns</h3>
                    <p>{data['subject_line_patterns']}</p>
                </div>
                <div class="insight-card">
                    <h3>CTA Patterns</h3>
                    <p>{data['cta_patterns']}</p>
                </div>
                <div class="insight-card">
                    <h3>Timing Patterns</h3>
                    <p>{data['timing_patterns']}</p>
                </div>
                <div class="insight-card">
                    <h3>Data Note</h3>
                    <p>{data['sample_size_note']}</p>
                </div>
            </div>
            <h3 class="top-label">Top Performing Emails</h3>
            <table class="top-emails">
                <thead><tr><th>Subject</th><th>Sent</th><th>Open Rate</th><th>Click Rate</th></tr></thead>
                <tbody>{top_emails_html}</tbody>
            </table>
        </div>"""

    playbook_data_json = json.dumps({
        ct.replace(" ", "-"): {"title": ct.title(), "count": playbook[ct].get("sample_count", "")}
        for ct in all_types if "status" not in playbook[ct]
    })
    first_anchor = all_types[0].replace(" ", "-") if all_types else ""
    first_title = all_types[0].title() if all_types else ""

    # Card color palette (one per metric)
    card_colors = [
        ("#1e3a5f", "#4a9eff"),   # delivered rate — navy
        ("#1a3a6b", "#2563eb"),   # open rate — blue
        ("#1e3a5f", "#0ea5e9"),   # click rate — sky
        ("#2d1f6e", "#7c3aed"),   # CTOR — purple
        ("#4a1942", "#db2777"),   # unsubscribe — pink (inverted logic)
        ("#4a1a1a", "#dc2626"),   # bounce — red (inverted logic)
    ]

    pipeline_data = pipeline_data or {}
    pipeline_data_json = json.dumps(pipeline_data)
    pipeline_campaigns = sorted(pipeline_data.keys())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reputation — Medallion</title>
<style>
  :root {{
    --color-bg: #E9E6DF;
    --color-border: #A29A90;
    --color-text: #0E0E0F;
    --color-text-secondary: #5A5548;
    --color-sidebar: #433F2A;
    --color-sidebar-muted: rgba(233, 230, 223, 0.72);
    --color-primary-hover: #754F4D;
    --color-accent: #9B756E;
    --color-good: #7B745B;
    --color-caution: #947B50;
    --color-avoid: #433F2A;
    --color-positive: #4B6B35;
    --color-good-bg: rgba(123, 116, 91, 0.18);
    --color-bad-bg: rgba(148, 123, 80, 0.22);
    --color-neutral-bg: rgba(162, 154, 144, 0.20);
    /* Metric cards — one solid color each, reused wherever a card-specific accent is needed */
    --color-card-1: #BDA49B;  /* Delivered rate */
    --color-card-2: #754F4D;  /* Open rate */
    --color-card-3: #9B756E;  /* Click rate */
    --color-card-4: #7B745B;  /* CTOR */
    --color-card-5: #433F2A;  /* Unsubscribe rate */
    --color-card-6: #947B50;  /* Bounce rate */
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--color-bg); color: var(--color-text); display: flex; height: 100vh; overflow: hidden; }}

  /* ── Sidebar ── */
  .sidebar {{
    width: 220px;
    min-width: 220px;
    background: var(--color-sidebar);
    border-right: 1px solid var(--color-border);
    display: flex;
    flex-direction: column;
    height: 100vh;
    position: fixed;
    top: 0; left: 0;
    z-index: 100;
  }}
  .sidebar-logo {{
    padding: 28px 24px 20px;
    border-bottom: 1px solid var(--color-primary-hover);
  }}
  .sidebar-logo h1 {{ font-size: 17px; font-weight: 700; color: var(--color-bg); letter-spacing: -0.02em; }}
  .sidebar-logo p {{ font-size: 11px; color: var(--color-sidebar-muted); margin-top: 3px; }}
  .sidebar-nav {{ flex: 1; padding: 12px 12px; display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }}
  .nav-item {{
    display: block;
    width: 100%;
    text-align: left;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 500;
    color: var(--color-bg);
    opacity: 0.72;
    cursor: pointer;
    border: none;
    background: none;
    transition: color 0.12s, background 0.12s, opacity 0.12s;
    letter-spacing: 0;
  }}
  .nav-item:hover {{ opacity: 1; background: var(--color-primary-hover); }}
  .nav-item.active {{ opacity: 1; color: var(--color-text); background: var(--color-bg); font-weight: 600; }}
  .sidebar-footer {{
    padding: 16px 24px;
    font-size: 11px;
    color: var(--color-sidebar-muted);
    border-top: 1px solid var(--color-primary-hover);
  }}

  /* ── Main content ── */
  .content-area {{
    margin-left: 220px;
    flex: 1;
    height: 100vh;
    overflow-y: auto;
    background: var(--color-bg);
  }}
  .view {{ display: none; max-width: 1100px; margin: 0 auto; padding: 32px 28px; }}
  .view.active {{ display: block; }}
  .view-title {{ font-size: 22px; font-weight: 700; color: var(--color-text); margin-bottom: 24px; }}

  /* ── Filters ── */
  .filters {{ display: flex; gap: 12px; margin-bottom: 20px; align-items: center; flex-wrap: wrap; }}
  .filter-group {{ display: flex; flex-direction: column; gap: 4px; }}
  .filter-group label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--color-primary-hover); }}
  .filters select, .filters input[type=date] {{
    background: white; border: 1px solid var(--color-border); border-radius: 8px;
    padding: 8px 12px; font-size: 13px; color: var(--color-text); cursor: pointer;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04); min-width: 160px;
  }}
  .filters select:focus, .filters input[type=date]:focus {{ outline: none; border-color: var(--color-accent); }}
  .filter-sep {{ color: var(--color-text-secondary); font-size: 18px; align-self: flex-end; padding-bottom: 8px; }}

  /* ── Metric cards ── */
  .metric-bar {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 28px; }}
  .metric-card {{ border-radius: 14px; padding: 20px 18px; color: white; position: relative; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.07); }}
  .metric-card-1 {{ background: var(--color-card-1); }}
  .metric-card-2 {{ background: var(--color-card-2); }}
  .metric-card-3 {{ background: var(--color-card-3); }}
  .metric-card-4 {{ background: var(--color-card-4); }}
  .metric-card-5 {{ background: var(--color-card-5); }}
  .metric-card-6 {{ background: var(--color-card-6); }}
  .metric-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: white; opacity: 0.85; margin-bottom: 10px; }}
  .metric-value {{ font-size: 30px; font-weight: 800; line-height: 1; margin-bottom: 8px; color: white; }}
  .metric-delta {{ font-size: 12px; font-weight: 600; }}
  .metric-benchmark {{ font-size: 11px; font-weight: 600; margin-top: 4px; }}
  /* Delta/benchmark text color is chosen per card for contrast against that
     card's specific solid background (e.g. card-1 is light, needs dark text;
     card-5 is very dark, needs light text) — not a good/bad semantic. */
  .metric-card-1 .metric-delta, .metric-card-1 .metric-benchmark {{ color: var(--color-text); }}
  .metric-card-2 .metric-delta, .metric-card-2 .metric-benchmark {{ color: white; }}
  .metric-card-3 .metric-delta, .metric-card-3 .metric-benchmark {{ color: var(--color-text); }}
  .metric-card-4 .metric-delta, .metric-card-4 .metric-benchmark {{ color: white; }}
  .metric-card-5 .metric-delta, .metric-card-5 .metric-benchmark {{ color: white; }}
  .metric-card-6 .metric-delta, .metric-card-6 .metric-benchmark {{ color: var(--color-text); }}

  /* ── Summary table ── */
  .section-title {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--color-primary-hover); margin-bottom: 12px; }}
  .summary-card {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 28px; overflow: hidden; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  thead {{ background: var(--color-bg); }}
  th {{ padding: 11px 16px; text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--color-primary-hover); }}
  td {{ padding: 12px 16px; border-bottom: 1px solid var(--color-border); }}
  tr:last-child td {{ border-bottom: none; }}
  .benchmark-row td {{ font-size: 12px; color: var(--color-text-secondary); font-style: italic; background: var(--color-bg); }}
  .data-row {{ cursor: pointer; transition: background 0.1s; }}
  .data-row:hover {{ background: var(--color-bg); }}
  .data-row.active {{ background: var(--color-bg); }}
  .data-row.active .type-cell {{ color: var(--color-accent); font-weight: 600; }}
  .type-cell {{ text-transform: capitalize; font-weight: 500; }}
  .pct-pill {{ color: var(--color-text); font-weight: 600; }}
  .pct-pill.good {{ background: var(--color-good-bg); padding: 3px 10px; border-radius: 99px; }}
  .pct-pill.bad {{ background: var(--color-bad-bg); padding: 3px 10px; border-radius: 99px; }}
  .pct-pill.ok {{ font-weight: 500; }}

  /* ── Playbook ── */
  .playbook-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }}
  .playbook-header h2 {{ font-size: 17px; font-weight: 600; text-transform: capitalize; color: var(--color-text); }}
  .playbook-header .sample-count {{ font-size: 13px; color: var(--color-text-secondary); font-weight: 400; margin-left: 8px; }}
  .select-styled {{
    appearance: none;
    background: white url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23A29A90' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E") no-repeat right 12px center;
    border: 1px solid var(--color-border); border-radius: 8px; padding: 8px 36px 8px 14px;
    font-size: 13px; font-weight: 500; color: var(--color-text); cursor: pointer; min-width: 180px;
  }}
  .playbook-card {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); padding: 24px; }}
  .insights-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }}
  .insight-card {{ background: var(--color-bg); border-radius: 8px; padding: 14px; }}
  .insight-card h3 {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--color-primary-hover); margin-bottom: 7px; }}
  .insight-card p {{ font-size: 13px; line-height: 1.65; color: var(--color-text); }}
  .top-label {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--color-primary-hover); margin-bottom: 8px; }}
  .top-emails thead {{ background: var(--color-bg); }}
  .top-emails th, .top-emails td {{ padding: 9px 12px; font-size: 13px; }}
  .insufficient-note {{ color: var(--color-caution); font-size: 14px; padding: 8px 0; }}

  /* ── AI Summary ── */
  .ai-summary-card {{ background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 12px; padding: 20px 24px; margin-bottom: 28px; }}
  .ai-summary-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--color-accent); }}
  .ai-summary-text {{ font-size: 14px; line-height: 1.65; color: var(--color-text); margin-bottom: 12px; }}
  .ai-summary-recs {{ list-style: none; padding: 0; display: flex; flex-direction: column; gap: 6px; }}
  .ai-summary-recs li {{ font-size: 13px; color: var(--color-text); padding-left: 18px; position: relative; line-height: 1.5; }}
  .ai-summary-recs li::before {{ content: "→"; position: absolute; left: 0; color: var(--color-accent); font-weight: 700; }}
  .ai-summary-note {{ font-size: 11px; color: var(--color-text-secondary); margin-top: 10px; }}

  /* ── Pipeline ── */
  .pipeline-meta {{ font-size: 13px; color: var(--color-text-secondary); margin-bottom: 20px; }}
  .pipeline-meta strong {{ color: var(--color-text); }}
  .pipeline-cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 28px; }}
  .pipeline-card {{ background: white; border: 1px solid var(--color-border); border-radius: 14px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); }}
  .pipeline-card-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.07em; color: var(--color-primary-hover); margin-bottom: 8px; }}
  .pipeline-card-value {{ font-size: 26px; font-weight: 800; color: var(--color-text); line-height: 1; margin-bottom: 4px; }}
  .pipeline-card-sub {{ font-size: 12px; color: var(--color-text-secondary); }}
  .pipeline-card-post {{ font-size: 12px; color: var(--color-positive); font-weight: 600; margin-top: 6px; }}
  .pipeline-section-title {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--color-primary-hover); margin-bottom: 12px; }}
  .pipeline-table-wrap {{ background: white; border: 1px solid var(--color-border); border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); overflow-x: auto; -webkit-overflow-scrolling: touch; margin-bottom: 24px; }}
  .pipeline-table-wrap table {{ min-width: 900px; }}
  .post-send-badge {{ display: inline-block; background: var(--color-bg); color: var(--color-positive); font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 99px; letter-spacing: 0.04em; margin-left: 6px; vertical-align: middle; }}
  .pipeline-tier-chip {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 99px; margin-right: 6px; color: var(--color-text); }}
  .tier-directly {{ background: var(--color-good-bg); }}
  .tier-none {{ background: var(--color-neutral-bg); }}
  .pipeline-tier-timing {{ font-size: 10px; color: var(--color-text-secondary); margin-top: 4px; white-space: nowrap; }}
  .pipeline-disclaimer {{ font-size: 12px; color: var(--color-text-secondary); margin-top: 8px; font-style: italic; }}
  .pipeline-empty {{ color: var(--color-text-secondary); font-size: 15px; padding: 60px; text-align: center; background: white; border-radius: 12px; }}

  /* ── Placeholder views ── */
  .placeholder-card {{ background: white; border-radius: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); padding: 64px 40px; text-align: center; max-width: 480px; margin: 60px auto 0; }}
  .placeholder-card h2 {{ font-size: 20px; font-weight: 700; color: var(--color-text); margin-bottom: 10px; }}
  .placeholder-card p {{ font-size: 14px; color: var(--color-text-secondary); line-height: 1.6; }}
  .placeholder-badge {{ display: inline-block; background: var(--color-bg); color: var(--color-accent); font-size: 11px; font-weight: 700; letter-spacing: 0.07em; padding: 4px 12px; border-radius: 99px; margin-bottom: 20px; text-transform: uppercase; }}

  /* ── Settings ── */
  .settings-section {{ background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.07); padding: 24px; margin-bottom: 20px; }}
  .settings-section h3 {{ font-size: 14px; font-weight: 600; color: var(--color-text); margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--color-border); }}
  .settings-row {{ display: flex; align-items: center; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--color-border); }}
  .settings-row:last-child {{ border-bottom: none; }}
  .settings-row-label {{ font-size: 13px; color: var(--color-text); font-weight: 500; }}
  .settings-row-value {{ font-size: 13px; color: var(--color-text-secondary); }}
  .status-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }}
  .status-dot.green {{ background: var(--color-good); }}
  .status-dot.red {{ background: var(--color-avoid); }}

  @media (max-width: 900px) {{
    .sidebar {{ width: 180px; min-width: 180px; }}
    .content-area {{ margin-left: 180px; }}
    .metric-bar {{ grid-template-columns: repeat(3, 1fr); }}
    .insights-grid {{ grid-template-columns: 1fr; }}
    .pipeline-cards {{ grid-template-columns: 1fr 1fr; }}
    .view {{ padding: 20px 16px; }}
  }}
</style>
</head>
<body>

<!-- Sidebar -->
<aside class="sidebar">
  <div class="sidebar-logo">
    <h1>Reputation</h1>
    <p>Medallion</p>
  </div>
  <nav class="sidebar-nav">
    <button class="nav-item active" onclick="switchView('dashboard', this)">Dashboard</button>
    <button class="nav-item" onclick="switchView('analyzer', this)">Analyzer</button>
    <button class="nav-item" onclick="switchView('pipeline', this)">Pipeline</button>
    <button class="nav-item" onclick="switchView('cowrite', this)">Co-write</button>
    <button class="nav-item" onclick="switchView('settings', this)">Settings</button>
  </nav>
  <div class="sidebar-footer">Updated {now_str}</div>
</aside>

<!-- Main content -->
<div class="content-area">

  <!-- Dashboard -->
  <div id="view-dashboard" class="view active">
    <div class="filters">
      <div class="filter-group">
        <label>From</label>
        <input type="date" id="filter-from" onchange="applyFilters()">
      </div>
      <div class="filter-sep">–</div>
      <div class="filter-group">
        <label>To</label>
        <input type="date" id="filter-to" onchange="applyFilters()">
      </div>
      <div class="filter-group">
        <label>Content Type</label>
        <select id="filter-type" onchange="applyFilters()">
          <option value="">All types</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Campaign</label>
        <select id="filter-campaign" onchange="applyFilters()">
          <option value="">All campaigns</option>
        </select>
      </div>
    </div>

    <div class="metric-bar" id="metric-bar">
      <div class="metric-card metric-card-1">
        <div class="metric-label">Delivered Rate</div>
        <div class="metric-value" id="val-delivered">—</div>
        <div class="metric-delta" id="delta-delivered"></div>
      </div>
      <div class="metric-card metric-card-2">
        <div class="metric-label">Open Rate</div>
        <div class="metric-value" id="val-open">—</div>
        <div class="metric-delta" id="delta-open"></div>
        <div class="metric-benchmark">Benchmark: 19%</div>
      </div>
      <div class="metric-card metric-card-3">
        <div class="metric-label">Click Rate</div>
        <div class="metric-value" id="val-click">—</div>
        <div class="metric-delta" id="delta-click"></div>
      </div>
      <div class="metric-card metric-card-4">
        <div class="metric-label">CTOR</div>
        <div class="metric-value" id="val-ctor">—</div>
        <div class="metric-delta" id="delta-ctor"></div>
        <div class="metric-benchmark">Benchmark: 7.5%</div>
      </div>
      <div class="metric-card metric-card-5">
        <div class="metric-label">Unsubscribe Rate</div>
        <div class="metric-value" id="val-unsub">—</div>
        <div class="metric-delta" id="delta-unsub"></div>
        <div class="metric-benchmark">Benchmark: 0.5%</div>
      </div>
      <div class="metric-card metric-card-6">
        <div class="metric-label">Bounce Rate</div>
        <div class="metric-value" id="val-bounce">—</div>
        <div class="metric-delta" id="delta-bounce"></div>
        <div class="metric-benchmark">Benchmark: 0.25%</div>
      </div>
    </div>

    <div class="ai-summary-card">
      <div class="ai-summary-header"><span class="sparkle">✦</span> AI Summary</div>
      <p class="ai-summary-text" id="ai-summary-text">Loading…</p>
      <ul class="ai-summary-recs" id="ai-summary-recs"></ul>
      <p class="ai-summary-note" id="ai-summary-note"></p>
    </div>

    <p class="section-title">By Content Type</p>
    <div class="summary-card">
      <table>
        <thead>
          <tr>
            <th>Content Type</th><th>Emails</th><th>Total Sent</th>
            <th>Open Rate</th><th>Click Rate</th><th>CTOR</th>
          </tr>
        </thead>
        <tbody id="summary-tbody"></tbody>
      </table>
    </div>
  </div><!-- /dashboard -->

  <!-- Analyzer -->
  <div id="view-analyzer" class="view">
    <div class="playbook-header">
      <h2 id="playbook-title">{first_title} <span class="sample-count" id="playbook-count"></span></h2>
      <select class="select-styled" id="type-picker" onchange="selectType(this.value)">
        <option value="">— select type —</option>
      </select>
    </div>
    <div class="playbook-card">
      {playbook_panels}
    </div>
  </div><!-- /analyzer -->

  <!-- Pipeline -->
  <div id="view-pipeline" class="view">
    <div class="filters">
      <div class="filter-group">
        <label>From</label>
        <input type="date" id="pipeline-filter-from" onchange="renderPipeline()">
      </div>
      <div class="filter-sep">–</div>
      <div class="filter-group">
        <label>To</label>
        <input type="date" id="pipeline-filter-to" onchange="renderPipeline()">
      </div>
      <div class="filter-group">
        <label>Content Type</label>
        <select id="pipeline-filter-type" onchange="renderPipeline()">
          <option value="">All types</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Campaign</label>
        <select id="pipeline-filter-campaign" onchange="renderPipeline()">
          <option value="">All campaigns</option>
        </select>
      </div>
    </div>
    <div id="pipeline-content">
      <div class="pipeline-empty">No pipeline data for the selected filters.</div>
    </div>
  </div><!-- /pipeline -->

  <!-- Co-write -->
  <div id="view-cowrite" class="view">
    <div class="placeholder-card">
      <div class="placeholder-badge">Coming Soon</div>
      <h2>Co-write</h2>
      <p>Brief-to-draft email writer powered by Claude. Paste a campaign brief and get a ready-to-send email draft in seconds, tailored to your content type and audience.</p>
    </div>
  </div><!-- /cowrite -->

  <!-- Settings -->
  <div id="view-settings" class="view">
    <p class="view-title">Settings</p>
    <div class="settings-section">
      <h3>Connections</h3>
      <div class="settings-row">
        <span class="settings-row-label">HubSpot</span>
        <span class="settings-row-value"><span class="status-dot green"></span>Connected</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Salesforce</span>
        <span class="settings-row-value"><span class="status-dot green"></span>Connected</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Anthropic AI</span>
        <span class="settings-row-value"><span class="status-dot green"></span>Connected</span>
      </div>
    </div>
    <div class="settings-section">
      <h3>Report Defaults</h3>
      <div class="settings-row">
        <span class="settings-row-label">Lookback window</span>
        <span class="settings-row-value">365 days</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Pipeline campaigns</span>
        <span class="settings-row-value">All since {pipeline_start_date}</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Internal domain filter</span>
        <span class="settings-row-value">@medallion.co excluded</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Scheduled refresh</span>
        <span class="settings-row-value">Daily at 5:00 AM PT</span>
      </div>
    </div>
    <div class="settings-section">
      <h3>Benchmarks</h3>
      <div class="settings-row">
        <span class="settings-row-label">Open Rate</span>
        <span class="settings-row-value">19%</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">CTOR</span>
        <span class="settings-row-value">7.5%</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Unsubscribe Rate</span>
        <span class="settings-row-value">0.5%</span>
      </div>
      <div class="settings-row">
        <span class="settings-row-label">Bounce Rate</span>
        <span class="settings-row-value">0.25%</span>
      </div>
    </div>
  </div><!-- /settings -->

</div><!-- /content-area -->

<script>
const ALL_EMAILS = {emails_json};
const PLAYBOOK = {playbook_data_json};
const AI_SUMMARIES = {json.dumps(ai_summaries)};
const PIPELINE_DATA = {pipeline_data_json};

// No chart components exist in this dashboard yet (Dashboard/Analyzer use
// tables, not canvas/SVG). This is the palette's specified 5-color sequence
// for whenever one is built, so new charts pick it up instead of library
// defaults.
const CHART_COLOR_SEQUENCE = [
  'var(--color-good)', 'var(--color-accent)', 'var(--color-caution)',
  'var(--color-primary-hover)', 'var(--color-border)',
];

// ── Sidebar navigation ──────────────────────────────────────────
function switchView(name, btn) {{
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  btn.classList.add('active');
}}

// Populate type filter and playbook picker from data
const typeSet = new Set(ALL_EMAILS.map(e => e.content_type).filter(t => t && t !== 'unknown'));
const types = [...typeSet].sort();
types.forEach(t => {{
  ['filter-type', 'type-picker'].forEach(id => {{
    const opt = document.createElement('option');
    opt.value = t.replace(/ /g, '-');
    opt.textContent = t.replace(/\\b\\w/g, c => c.toUpperCase());
    document.getElementById(id).appendChild(opt.cloneNode(true));
  }});
}});

// Populate campaign filter from campaign_name field
const campaigns = [...new Set(ALL_EMAILS.map(e => e.campaign_name).filter(Boolean))].sort();
campaigns.forEach(name => {{
  const opt = document.createElement('option');
  opt.value = name;
  opt.textContent = name.length > 60 ? name.slice(0, 60) + '…' : name;
  document.getElementById('filter-campaign').appendChild(opt);
}});

// Set default date range: last 30 days
function toISO(d) {{ return d.toISOString().split('T')[0]; }}
const today = new Date();
const d30 = new Date(today); d30.setDate(today.getDate() - 30);
document.getElementById('filter-to').value = toISO(today);
document.getElementById('filter-from').value = toISO(d30);

// ── Pipeline ────────────────────────────────────────────────────
function fmt$(n) {{
  if (n >= 1e6) return '$' + (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + Math.round(n/1e3) + 'K';
  return '$' + n.toLocaleString();
}}

const pipelineCampaignNames = Object.keys(PIPELINE_DATA).sort();
pipelineCampaignNames.forEach(name => {{
  const opt = document.createElement('option');
  opt.value = name;
  opt.textContent = name.length > 60 ? name.slice(0, 60) + '…' : name;
  document.getElementById('pipeline-filter-campaign').appendChild(opt);
}});

const pipelineTypeSet = new Set(Object.values(PIPELINE_DATA).map(d => d.content_type).filter(Boolean));
[...pipelineTypeSet].sort().forEach(t => {{
  const opt = document.createElement('option');
  opt.value = t.replace(/ /g, '-');
  opt.textContent = t.replace(/\\b\\w/g, c => c.toUpperCase());
  document.getElementById('pipeline-filter-type').appendChild(opt);
}});

// Default range: fixed program start date through today — matches what's baked into PIPELINE_DATA
document.getElementById('pipeline-filter-from').value = '{pipeline_start_date}';
document.getElementById('pipeline-filter-to').value = toISO(today);

function selectPipelineCampaigns(from, to, type, campaign) {{
  return Object.entries(PIPELINE_DATA).filter(([name, d]) => {{
    if (campaign && name !== campaign) return false;
    if (from && d.send_date < from) return false;
    if (to && d.send_date > to) return false;
    if (type && (d.content_type || '').replace(/ /g, '-') !== type) return false;
    return true;
  }});
}}

// Honest, non-causal labels for contact-level signal tiers — never "caused" or "attributed".
const TIER_LABELS = {{ directly_followed: 'Directly followed', no_signal: 'No qualifying signal' }};
const TIER_RANK = {{ directly_followed: 1, no_signal: 0 }};
const TIER_CLASS = {{ directly_followed: 'tier-directly', no_signal: 'tier-none' }};
const SIGNAL_WINDOW_DAYS = 90; // mirrors pipeline.py's SIGNAL_WINDOW_DAYS

// Shows created-vs-click dates and the day delta driving the tier verdict.
function tierTimingLabel(o) {{
  if (!o.created_date || !o.click_date_used) return '';
  const created = new Date(o.created_date + 'T00:00:00Z');
  const clicked = new Date(o.click_date_used + 'T00:00:00Z');
  const days = Math.round((created - clicked) / 86400000);
  const base = `Created ${{o.created_date}} · Clicked ${{o.click_date_used}}`;
  if (days < 0) return `${{base}} (${{Math.abs(days)}}d before click)`;
  if (days > SIGNAL_WINDOW_DAYS) return `${{base}} (+${{days}}d, outside ${{SIGNAL_WINDOW_DAYS}}d window)`;
  return `${{base}} (+${{days}}d)`;
}}

// Merge N campaigns into one rollup: dedupe matched contacts by email and
// opportunities by id so a contact/deal touched by multiple campaigns in the
// selected range is only counted once. When the same opportunity shows up
// under more than one campaign with different signal tiers, keep the
// strongest tier found (directly_followed > no_signal).
function aggregatePipeline(entries) {{
  const oppById = new Map();
  const emailSet = new Set();
  let totalEngaged = 0;

  entries.forEach(([, d]) => {{
    totalEngaged += d.total_engaged;
    (d.matched_emails || []).forEach(e => emailSet.add(e));
    (d.opportunities || []).forEach(o => {{
      const existing = oppById.get(o.id);
      if (!existing) {{
        oppById.set(o.id, {{...o}});
      }} else {{
        existing.contact_level = existing.contact_level || o.contact_level;
        existing.post_send = existing.post_send || o.post_send;
        if ((TIER_RANK[o.signal_tier] ?? -1) > (TIER_RANK[existing.signal_tier] ?? -1)) {{
          existing.signal_tier = o.signal_tier;
          existing.corroborated = o.corroborated;
          existing.click_date_used = o.click_date_used;
          existing.click_date_source = o.click_date_source;
          existing.email_name = o.email_name;
        }}
      }}
    }});
  }});

  const opps = [...oppById.values()];
  const contactOpps = opps.filter(o => o.contact_level);
  const accountOpps = opps.filter(o => !o.contact_level);

  const rollup = list => {{
    const open = list.filter(o => !o.is_closed);
    const won = list.filter(o => o.is_won);
    const post = list.filter(o => o.post_send && !o.is_closed);
    return {{
      open_count: open.length, open_value: open.reduce((s, o) => s + o.amount, 0),
      won_count: won.length, won_value: won.reduce((s, o) => s + o.amount, 0),
      post_count: post.length, post_value: post.reduce((s, o) => s + o.amount, 0),
    }};
  }};

  const c = rollup(contactOpps);
  const a = rollup(accountOpps);
  const topOpps = contactOpps.filter(o => !o.is_closed)
    .sort((x, y) => y.amount - x.amount).slice(0, 10);
  const topAccountOpps = accountOpps.filter(o => !o.is_closed)
    .sort((x, y) => y.amount - x.amount).slice(0, 10);

  const tierCounts = {{ directly_followed: 0, no_signal: 0 }};
  contactOpps.forEach(o => {{ if (tierCounts.hasOwnProperty(o.signal_tier)) tierCounts[o.signal_tier]++; }});

  return {{
    campaignCount: entries.length,
    total_engaged: totalEngaged,
    total_matched: emailSet.size,
    contact_open_count: c.open_count, contact_open_value: c.open_value,
    contact_won_count: c.won_count, contact_won_value: c.won_value,
    contact_post_count: c.post_count, contact_post_value: c.post_value,
    account_open_count: a.open_count, account_open_value: a.open_value,
    account_won_count: a.won_count, account_won_value: a.won_value,
    account_post_count: a.post_count, account_post_value: a.post_value,
    top_opps: topOpps,
    top_account_opps: topAccountOpps,
    tier_counts: tierCounts,
  }};
}}

function renderPipeline() {{
  const from = document.getElementById('pipeline-filter-from').value;
  const to = document.getElementById('pipeline-filter-to').value;
  const type = document.getElementById('pipeline-filter-type').value;
  const campaign = document.getElementById('pipeline-filter-campaign').value;
  const el = document.getElementById('pipeline-content');

  const entries = selectPipelineCampaigns(from, to, type, campaign);
  if (!entries.length) {{
    el.innerHTML = '<div class="pipeline-empty">No pipeline data for the selected filters.</div>';
    return;
  }}

  const d = aggregatePipeline(entries);
  const matchPct = d.total_engaged ? Math.round(d.total_matched / d.total_engaged * 100) : 0;

  const postContact = d.contact_post_count > 0
    ? `<div class="pipeline-card-post">★ ${{d.contact_post_count}} opps (${{fmt$(d.contact_post_value)}}) created post-send</div>` : '';
  const postAccount = d.account_post_count > 0
    ? `<div class="pipeline-card-post">★ ${{d.account_post_count}} opps (${{fmt$(d.account_post_value)}}) created post-send</div>` : '';

  const emailCell = name => name
    ? `<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{name}}">${{name}}</td>`
    : '<td style="color:#94a3b8">—</td>';

  let oppsRows = '';
  d.top_opps.forEach(o => {{
    const postBadge = o.post_send ? '<span class="post-send-badge">POST-SEND</span>' : '';
    const timing = tierTimingLabel(o);
    const tierBadge = o.signal_tier
      ? `<span class="pipeline-tier-chip ${{TIER_CLASS[o.signal_tier]}}">${{TIER_LABELS[o.signal_tier]}}</span>` +
        (timing ? `<div class="pipeline-tier-timing">${{timing}}</div>` : '')
      : '';
    oppsRows += `<tr>
      <td>${{o.account}}${{postBadge}}</td>
      <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{o.name}}</td>
      <td>${{o.stage}}</td>
      <td>${{tierBadge}}</td>
      ${{emailCell(o.email_name)}}
      <td style="text-align:right;font-weight:700">${{fmt$(o.amount)}}</td>
    </tr>`;
  }});
  if (!oppsRows) oppsRows = '<tr><td colspan="6" style="color:#94a3b8">No open contact-level opportunities found.</td></tr>';

  let accountOppsRows = '';
  d.top_account_opps.forEach(o => {{
    const postBadge = o.post_send ? '<span class="post-send-badge">POST-SEND</span>' : '';
    accountOppsRows += `<tr>
      <td>${{o.account}}${{postBadge}}</td>
      <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{o.name}}</td>
      <td>${{o.stage}}</td>
      ${{emailCell(o.email_name)}}
      <td style="text-align:right;font-weight:700">${{fmt$(o.amount)}}</td>
    </tr>`;
  }});
  if (!accountOppsRows) accountOppsRows = '<tr><td colspan="5" style="color:#94a3b8">No open account-level opportunities found.</td></tr>';

  const scopeLabel = d.campaignCount === 1
    ? `Sent ${{entries[0][1].send_date}}`
    : `${{d.campaignCount}} campaigns in range`;

  const tierLine = `
    <span class="pipeline-tier-chip tier-directly">${{d.tier_counts.directly_followed}} directly followed</span>
    <span class="pipeline-tier-chip tier-none">${{d.tier_counts.no_signal}} no qualifying signal</span>
  `;

  el.innerHTML = `
    <div class="pipeline-meta">
      ${{scopeLabel}} &nbsp;·&nbsp;
      <strong>${{d.total_matched}}</strong> unique contacts matched in Salesforce (${{d.total_engaged}} total click touches, ${{matchPct}}%)
    </div>
    <div class="pipeline-meta">${{tierLine}}</div>
    <div class="pipeline-cards">
      <div class="pipeline-card">
        <div class="pipeline-card-label">Contact-Level Open Pipeline</div>
        <div class="pipeline-card-value">${{fmt$(d.contact_open_value)}}</div>
        <div class="pipeline-card-sub">${{d.contact_open_count}} opportunities</div>
        ${{postContact}}
      </div>
      <div class="pipeline-card">
        <div class="pipeline-card-label">Account-Level Open Pipeline</div>
        <div class="pipeline-card-value">${{fmt$(d.account_open_value)}}</div>
        <div class="pipeline-card-sub">${{d.account_open_count}} opportunities</div>
        ${{postAccount}}
      </div>
      <div class="pipeline-card">
        <div class="pipeline-card-label">Won Revenue (contact-level)</div>
        <div class="pipeline-card-value">${{fmt$(d.contact_won_value)}}</div>
        <div class="pipeline-card-sub">${{d.contact_won_count}} closed-won deals</div>
      </div>
    </div>

    <p class="pipeline-section-title">Top Open Opportunities (contact-level)</p>
    <div class="pipeline-table-wrap">
      <table>
        <thead><tr><th>Account</th><th>Opportunity</th><th>Stage</th><th>Signal</th><th>Email Clicked</th><th style="text-align:right">Amount</th></tr></thead>
        <tbody>${{oppsRows}}</tbody>
      </table>
    </div>

    <p class="pipeline-section-title">Top Open Opportunities (account-level)</p>
    <div class="pipeline-table-wrap">
      <table>
        <thead><tr><th>Account</th><th>Opportunity</th><th>Stage</th><th>Email Clicked</th><th style="text-align:right">Amount</th></tr></thead>
        <tbody>${{accountOppsRows}}</tbody>
      </table>
    </div>
    <p class="pipeline-disclaimer">
      Engagement here means clicked — opens no longer count anywhere in Pipeline Association, including which contacts/accounts pull in opportunities at all.
      ★ POST-SEND = opportunity created any time after the email send date (no day limit).
      Signal tiers apply to contact-level opportunities only, using a 90-day attribution window from the contact's click. Opportunities created more than 365 days before the click are excluded entirely as too old to plausibly relate.
      <strong>Directly followed</strong> = the opp was created within 90 days after the contact's click, with a rep Task/Event tying the contact or account to that window;
      <strong>No qualifying signal</strong> = everything else — outside the 90-day window, or inside it with no corroborating rep activity found — still shown, never hidden.
      Account-level opportunities are not tiered. These are association signals, not causal attribution — no tier means the email "caused" or should be credited with the deal.
      All figures use Opportunity_Amount__c. Matched contacts and pipeline value are deduplicated across every campaign in the selected range; "total click touches" is summed per campaign and is not deduplicated.
    </p>
  `;
}}

renderPipeline();

function pct(v) {{ return (v * 100).toFixed(1) + '%'; }}

function agg(emails) {{
  let sent=0, delivered=0, opens=0, clicks=0, bounced=0, unsubscribed=0;
  emails.forEach(e => {{
    sent += e.sent; delivered += e.delivered; opens += e.opens;
    clicks += e.clicks; bounced += e.bounced; unsubscribed += e.unsubscribed;
  }});
  return {{
    count: emails.length, sent, delivered, opens, clicks, bounced, unsubscribed,
    delivered_rate: sent ? delivered/sent : 0,
    open_rate: delivered ? opens/delivered : 0,
    click_rate: delivered ? clicks/delivered : 0,
    ctor: opens ? clicks/opens : 0,
    unsub_rate: delivered ? unsubscribed/delivered : 0,
    bounce_rate: sent ? bounced/sent : 0,
  }};
}}

function filterEmails(from, to, type, campaign) {{
  return ALL_EMAILS.filter(e => {{
    if (!e.send_date) return false;
    if (from && e.send_date < from) return false;
    if (to && e.send_date > to) return false;
    if (type && e.content_type.replace(/ /g,'-') !== type) return false;
    if (campaign && e.campaign_name !== campaign) return false;
    return true;
  }});
}}

function priorRange(from, to) {{
  const f = new Date(from), t = new Date(to);
  const days = Math.round((t-f)/(1000*86400));
  const pf = new Date(f); pf.setDate(pf.getDate()-days);
  const pt = new Date(f); pt.setDate(pt.getDate()-1);
  return [toISO(pf), toISO(pt)];
}}

function setCard(valId, deltaId, value, priorValue, invert) {{
  document.getElementById(valId).textContent = pct(value);
  if (priorValue === null) {{ document.getElementById(deltaId).textContent = ''; return; }}
  const delta = value - priorValue;
  const arrow = delta >= 0 ? '↑' : '↓';
  const isGood = invert ? delta <= 0 : delta >= 0;
  const cls = (delta >= 0 ? 'up-' : 'down-') + (isGood ? 'good' : 'bad');
  const el = document.getElementById(deltaId);
  el.textContent = arrow + ' ' + pct(Math.abs(delta)) + ' vs prev period';
  el.className = 'metric-delta ' + cls;
}}

function applyFilters() {{
  const from = document.getElementById('filter-from').value;
  const to = document.getElementById('filter-to').value;
  const type = document.getElementById('filter-type').value;
  const campaign = document.getElementById('filter-campaign').value;

  const current = filterEmails(from, to, type, campaign);
  const [pf, pt] = priorRange(from, to);
  const prior = filterEmails(pf, pt, type, campaign);

  const c = agg(current);
  const p = prior.length ? agg(prior) : null;

  setCard('val-delivered','delta-delivered', c.delivered_rate, p?.delivered_rate??null, false);
  setCard('val-open','delta-open', c.open_rate, p?.open_rate??null, false);
  setCard('val-click','delta-click', c.click_rate, p?.click_rate??null, false);
  setCard('val-ctor','delta-ctor', c.ctor, p?.ctor??null, false);
  setCard('val-unsub','delta-unsub', c.unsub_rate, p?.unsub_rate??null, true);
  setCard('val-bounce','delta-bounce', c.bounce_rate, p?.bounce_rate??null, true);

  updateAiSummary(type, campaign);

  // Update summary table
  const byType = {{}};
  current.forEach(e => {{
    const k = e.content_type || 'unknown';
    if (!byType[k]) byType[k] = [];
    byType[k].push(e);
  }});
  const tbody = document.getElementById('summary-tbody');
  tbody.innerHTML = '';
  Object.entries(byType)
    .filter(([k]) => k !== 'unknown')
    .sort((a,b) => b[1].length - a[1].length)
    .forEach(([ct, emails]) => {{
      const a = agg(emails);
      const anchor = ct.replace(/ /g,'-');
      const orCls = a.open_rate >= 0.385 ? 'good' : a.open_rate >= 0.2975 ? 'ok' : 'bad';
      const crCls = a.click_rate >= 0.033 ? 'good' : a.click_rate >= 0.0255 ? 'ok' : 'bad';
      const ctCls = a.ctor >= 0.11 ? 'good' : a.ctor >= 0.085 ? 'ok' : 'bad';
      tbody.innerHTML += `<tr class="data-row" data-target="${{anchor}}" onclick="selectType('${{anchor}}'); switchView('analyzer', document.querySelector('.nav-item:nth-child(2)'))">
        <td class="type-cell">${{ct}}</td>
        <td>${{a.count}}</td>
        <td>${{a.sent.toLocaleString()}}</td>
        <td><span class="pct-pill ${{orCls}}">${{pct(a.open_rate)}}</span></td>
        <td><span class="pct-pill ${{crCls}}">${{pct(a.click_rate)}}</span></td>
        <td><span class="pct-pill ${{ctCls}}">${{pct(a.ctor)}}</span></td>
      </tr>`;
    }});
  tbody.innerHTML += `<tr class="benchmark-row"><td colspan="3">B2B SaaS benchmark</td><td>35%</td><td>3%</td><td>10%</td></tr>`;
}}

function updateAiSummary(typeFilter, campaignFilter) {{
  // Campaign takes priority, then content type, then overall
  let key, note = '';
  if (campaignFilter) {{
    key = 'campaign::' + campaignFilter;
    if (!AI_SUMMARIES[key]) {{
      key = 'overall';
      note = 'No pre-generated summary for this campaign. Re-run the report to generate one.';
    }}
  }} else if (typeFilter) {{
    key = typeFilter.replace(/-/g,' ');
    if (!AI_SUMMARIES[key]) key = 'overall';
  }} else {{
    key = 'overall';
  }}

  const data = AI_SUMMARIES[key] || {{"summary": "—", "recommendations": []}};
  document.getElementById('ai-summary-text').textContent = data.summary || '—';

  const recsEl = document.getElementById('ai-summary-recs');
  recsEl.innerHTML = '';
  (data.recommendations || []).forEach(r => {{
    const li = document.createElement('li');
    li.textContent = r;
    recsEl.appendChild(li);
  }});

  document.getElementById('ai-summary-note').textContent = note;
}}

function selectType(anchor) {{
  document.querySelectorAll('.playbook-panel').forEach(p => p.style.display = 'none');
  const panel = document.getElementById('panel-' + anchor);
  if (panel) panel.style.display = '';
  const info = PLAYBOOK[anchor];
  if (info) {{
    document.getElementById('playbook-title').childNodes[0].textContent = info.title + ' ';
    document.getElementById('playbook-count').textContent = info.count ? info.count + ' emails' : '';
  }}
  document.getElementById('type-picker').value = anchor;
  document.querySelectorAll('.data-row').forEach(r => r.classList.remove('active'));
  const row = document.querySelector(`.data-row[data-target="${{anchor}}"]`);
  if (row) row.classList.add('active');
}}

// Init
applyFilters();
selectType('{first_anchor}');
document.getElementById('type-picker').value = '{first_anchor}';
</script>
</body>
</html>"""


def generate_report(
    *,
    days: int = 365,
    push: bool = True,
    token: Optional[str] = None,
) -> str:
    print("Fetching current period emails (last 365 days)…")
    current = fetch_emails(days=365, token=token)

    print("Fetching prior period emails (365–730 days ago)…")
    all_730 = fetch_emails(days=730, token=token)
    prior = [e for e in all_730 if e not in current]

    current_groups = _group_by_type(current)
    prior_groups = _group_by_type(prior)

    print("Running analyzer…")
    playbook = build_playbook(days=days, token=token)

    # Generate AI summaries per content type + overall
    print("\nGenerating AI summaries…")
    all_current = [e for e in current if e.content_type]
    all_prior = [e for e in prior if e.content_type]
    ov_cur = _aggregate(all_current)
    ov_pri = _aggregate(all_prior) if all_prior else None

    ai_summaries = {}
    print("  [overall]…")
    try:
        ai_summaries["overall"] = _generate_ai_summary("All content types", ov_cur, ov_pri)
        print("    ✓")
    except Exception as e:
        print(f"    ✗ {e}")
        ai_summaries["overall"] = {"summary": "", "recommendations": []}

    for ct, emails in sorted(current_groups.items(), key=lambda x: -len(x[1])):
        if ct == "unknown" or len(emails) < 5:
            continue
        print(f"  [{ct}]…")
        try:
            prior_emails = prior_groups.get(ct, [])
            ai_summaries[ct] = _generate_ai_summary(
                ct.title(), _aggregate(emails),
                _aggregate(prior_emails) if prior_emails else None
            )
            print("    ✓")
        except Exception as e:
            print(f"    ✗ {e}")
            ai_summaries[ct] = {"summary": "", "recommendations": []}

    # Per-campaign summaries
    campaign_groups: dict[str, list[EmailRecord]] = defaultdict(list)
    for e in current:
        if e.campaign_name:
            campaign_groups[e.campaign_name].append(e)
    prior_campaign_groups: dict[str, list[EmailRecord]] = defaultdict(list)
    for e in prior:
        if e.campaign_name:
            prior_campaign_groups[e.campaign_name].append(e)

    for campaign, emails in sorted(campaign_groups.items(), key=lambda x: -len(x[1])):
        if len(emails) < 3:
            continue
        key = f"campaign::{campaign}"
        print(f"  [campaign: {campaign}]…")
        try:
            prior_emails = prior_campaign_groups.get(campaign, [])
            ai_summaries[key] = _generate_ai_summary(
                campaign, _aggregate(emails),
                _aggregate(prior_emails) if prior_emails else None
            )
            print("    ✓")
        except Exception as e:
            print(f"    ✗ {e}")
            ai_summaries[key] = {"summary": "", "recommendations": []}

    # Pipeline association (requires Salesforce credentials)
    pipeline_data: dict = {}
    PIPELINE_AVAILABLE = _PIPELINE_IMPORT_OK and bool(os.environ.get("SF_USERNAME"))
    if PIPELINE_AVAILABLE:
        print("\nGenerating pipeline association data…")
        pipeline_cutoff = datetime.strptime(PIPELINE_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        # Every campaign sent on/after PIPELINE_START_DATE qualifies — fixed
        # cutoff, not a rolling window. all_730 (2yr lookback) comfortably
        # covers it as long as the cutoff is less than 2 years in the past.
        campaign_id_map: dict[str, list[str]] = defaultdict(list)
        campaign_content_type: dict[str, str] = {}
        for e in all_730:
            if not (e.campaign_name and e.campaign_ids and e.send_date):
                continue
            if e.send_date < pipeline_cutoff:
                continue
            for cid in e.campaign_ids:
                if cid not in campaign_id_map[e.campaign_name]:
                    campaign_id_map[e.campaign_name].append(cid)
            if e.content_type and e.campaign_name not in campaign_content_type:
                campaign_content_type[e.campaign_name] = e.content_type

        qualifying_campaigns = sorted(campaign_id_map.items())
        print(f"  {len(qualifying_campaigns)} campaigns sent on/after {PIPELINE_START_DATE} qualify")

        pipeline_start_time = time.monotonic()
        for campaign_name, campaign_ids in qualifying_campaigns:
            print(f"  [{campaign_name}]…")
            try:
                # Use the first (most recent) campaign ID for this campaign
                result = analyze_campaign_pipeline(campaign_ids[0], hs_token=token)
                data = result.to_dict()
                data["content_type"] = campaign_content_type.get(campaign_name)
                pipeline_data[campaign_name] = data
                print(f"    ✓ {result.total_matched} contacts matched")
            except Exception as e:
                print(f"    ✗ {e}")
        pipeline_elapsed = time.monotonic() - pipeline_start_time

        n = len(qualifying_campaigns)
        avg = pipeline_elapsed / n if n else 0
        print(
            f"\nPipeline association: {len(pipeline_data)}/{n} campaigns succeeded "
            f"in {pipeline_elapsed:.1f}s ({avg:.1f}s/campaign avg)"
        )
    else:
        print("\nSkipping pipeline (SF credentials not configured).")

    rows = _build_summary_rows(current_groups, prior_groups)
    generated_at = datetime.now(tz=timezone.utc)
    html = _render_html(
        all_730, rows, playbook, current_groups, prior_groups, ai_summaries, generated_at,
        pipeline_data, PIPELINE_START_DATE,
    )

    output_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"\nReport written to {output_path}")

    if push:
        _git_push(output_path)

    return output_path


def _git_push(filepath: str) -> None:
    repo_dir = os.path.dirname(filepath)
    try:
        subprocess.run(["git", "add", "index.html"], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Update report {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=repo_dir,
            check=True,
        )
        subprocess.run(["git", "push"], cwd=repo_dir, check=True)
        print("Pushed to GitHub — dashboard will update in ~30 seconds.")
    except subprocess.CalledProcessError as e:
        print(f"Git push failed: {e}")


if __name__ == "__main__":
    generate_report()
