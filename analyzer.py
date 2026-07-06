"""
SendSmart Analyzer — Step 2.

Pulls email data from hubspot_client.py, groups by content_type, calls Claude
to identify patterns, and returns a structured playbook per content type.
"""

import json
import os
from collections import defaultdict
from typing import Optional

import anthropic
from dotenv import load_dotenv

from hubspot_client import EmailRecord, fetch_emails

load_dotenv()

MIN_SAMPLE_SIZE = 5

_INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "confidence": {"type": "string", "enum": ["strong", "moderate", "none"]},
        "key_stat": {"type": "string"},
        "reasoning": {"type": "string"},
        "action": {"type": "string"},
    },
    "required": ["headline", "confidence", "key_stat", "reasoning", "action"],
    "additionalProperties": False,
}

_PLAYBOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "insights": {"type": "array", "items": _INSIGHT_SCHEMA},
        "top_performing_examples": {
            "type": "array",
            "items": {"type": "string"},
        },
        "data_quality_note": {"type": "string"},
    },
    "required": [
        "executive_summary",
        "insights",
        "top_performing_examples",
        "data_quality_note",
    ],
    "additionalProperties": False,
}


def _build_prompt(content_type: str, emails: list[EmailRecord]) -> str:
    # Sort by open rate descending so top performers are easy to reference
    sorted_emails = sorted(emails, key=lambda e: e.open_rate, reverse=True)

    lines = [
        f"You are analyzing {len(emails)} '{content_type}' marketing emails sent by a B2B SaaS company.",
        "",
        "Here is the email data (sorted by open rate, highest first):",
        "",
    ]

    for e in sorted_emails:
        send_date = e.send_date.strftime("%Y-%m-%d") if e.send_date else "unknown"
        lines.append(
            f"- Subject: {e.subject!r} | Sent: {e.sent:,} | Open rate: {e.open_rate:.1%} "
            f"| Click rate: {e.click_rate:.1%} | Date: {send_date}"
        )

    lines += [
        "",
        "Based ONLY on the patterns visible in this data, produce a playbook with these fields:",
        "  executive_summary: 2-4 sentences synthesizing what the top-performing emails have in "
        "common and what to do differently next time. This is a takeaway, not a restatement of "
        "the raw numbers — someone who already saw the table should learn something from this.",
        "  insights: A list of individual findings, each covering ONE specific pattern (e.g. a "
        "subject line wording pattern, subject length, a CTA/content theme, a send-day or "
        "send-time pattern). For each insight, set:",
        "    - headline: one short, specific sentence stating the finding.",
        "    - confidence: 'strong' if the pattern is clear and consistent across multiple emails, "
        "'moderate' if directional but caveated (small sample, one outlier, conflicting signal), "
        "or 'none' if you checked for a pattern along this dimension and the data does NOT "
        "support one — say so explicitly rather than omitting the insight.",
        "    - key_stat: the single most relevant number backing this insight (a rate, a count, "
        "or a percentage-point gap).",
        "    - reasoning: 1-3 sentences of supporting detail and caveats.",
        "    - action: for 'strong' insights, one concrete 'try this next' recommendation. For "
        "'moderate' or 'none' insights, use an empty string.",
        "  You MUST include at least one insight for subject line wording, one for subject length, "
        "and one for send timing — use 'none' confidence for any of these where the data doesn't "
        "support a conclusion, rather than skipping it. Add further insights for CTA/content "
        "themes or any other pattern you find.",
        "  top_performing_examples: List 2-3 actual subject lines from the top-performing emails.",
        "  data_quality_note: One consolidated note covering sample size, audience-size skew, and "
        "any subject lines repeated across multiple sends/segments — whatever caveats apply here.",
        "",
        "IMPORTANT: Only report patterns actually supported by this data. Do not add generic "
        "email best-practices that are not evidenced here.",
        "",
        "Return ONLY valid JSON matching the schema — no markdown fences, no extra keys.",
    ]

    return "\n".join(lines)


def _analyze_content_type(
    client: anthropic.Anthropic,
    content_type: str,
    emails: list[EmailRecord],
) -> dict:
    prompt = _build_prompt(content_type, emails)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        output_config={"format": {"type": "json_schema", "schema": _PLAYBOOK_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _build_playbook_from_groups(
    client: anthropic.Anthropic,
    groups: dict[str, list["EmailRecord"]],
    *,
    label: str = "",
) -> dict[str, dict]:
    """
    Given content_type -> emails groups (already fetched/filtered by the
    caller — e.g. persona-scoped synthetic EmailRecords), run the same
    per-content-type Claude analysis build_playbook uses. Types with fewer
    than MIN_SAMPLE_SIZE emails are marked 'insufficient_data' without an
    API call, same as the unsegmented path.
    """
    prefix = f"[{label}] " if label else ""
    playbook: dict[str, dict] = {}

    for content_type, group in sorted(groups.items(), key=lambda x: -len(x[1])):
        if content_type == "unknown":
            continue

        if len(group) < MIN_SAMPLE_SIZE:
            print(
                f"{prefix}[{content_type}] Insufficient data ({len(group)} emails, need {MIN_SAMPLE_SIZE}+)"
            )
            playbook[content_type] = {
                "status": "insufficient_data",
                "sample_count": len(group),
                "minimum_required": MIN_SAMPLE_SIZE,
            }
            continue

        print(f"{prefix}[{content_type}] Analyzing {len(group)} emails…")
        try:
            result = _analyze_content_type(client, content_type, group)
            result["sample_count"] = len(group)
            playbook[content_type] = result
            print(f"  ✓ Done")
        except Exception as exc:
            print(f"  ✗ Error: {exc}")
            playbook[content_type] = {"status": "error", "error": str(exc)}

    return playbook


def build_playbook(
    *,
    days: int = 365,
    token: Optional[str] = None,
) -> dict[str, dict]:
    """
    Fetch emails, group by content_type, analyze each type with Claude.

    Returns a dict keyed by content_type. Types with fewer than MIN_SAMPLE_SIZE
    emails are marked as 'insufficient_data' without an API call.
    """
    print(f"Fetching emails from the last {days} days…")
    emails = fetch_emails(days=days, token=token)
    print(f"  → {len(emails)} emails fetched")

    # Group by content_type; None goes into "unknown"
    groups: dict[str, list[EmailRecord]] = defaultdict(list)
    for e in emails:
        key = e.content_type or "unknown"
        groups[key].append(e)

    print(f"\nContent type breakdown:")
    for ct, group in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"  {ct:<20} {len(group):>3} emails")

    client = anthropic.Anthropic(api_key=token or os.environ["ANTHROPIC_API_KEY"])

    return _build_playbook_from_groups(client, groups)


def build_persona_playbooks(
    persona_email_groups: dict[str, dict[str, list[EmailRecord]]],
    *,
    token: Optional[str] = None,
) -> dict[str, dict[str, dict]]:
    """
    Run the same per-content-type Claude analysis once per persona, against
    already persona-scoped emails (see persona_data.persona_groups_for —
    callers build `persona_email_groups` once and reuse it both here and for
    rendering, since analyzer.py stays agnostic of persona_config internals).

    Returns {persona: {content_type: result_or_status}}.
    """
    client = anthropic.Anthropic(api_key=token or os.environ["ANTHROPIC_API_KEY"])

    playbooks: dict[str, dict[str, dict]] = {}
    for persona, persona_groups in persona_email_groups.items():
        print(f"\n[persona: {persona}] {sum(len(v) for v in persona_groups.values())} eligible emails across {len(persona_groups)} content types")
        playbooks[persona] = _build_playbook_from_groups(client, persona_groups, label=f"persona: {persona}")

    return playbooks


def _print_playbook(playbook: dict[str, dict]) -> None:
    print("\n" + "=" * 70)
    print("SENDSMART PLAYBOOK")
    print("=" * 70)

    for content_type, data in sorted(playbook.items()):
        print(f"\n{'─' * 70}")
        print(f"  {content_type.upper()}")
        print(f"{'─' * 70}")

        if "status" in data:
            if data["status"] == "insufficient_data":
                print(f"  ⚠ Insufficient data ({data['sample_count']} emails, need {data['minimum_required']}+)")
            else:
                print(f"  ✗ Error: {data.get('error', 'unknown')}")
            continue

        print(f"  Sample size: {data.get('sample_count', '?')} emails\n")
        print(f"  Executive summary:")
        print(f"    {data['executive_summary']}\n")
        for insight in data.get("insights", []):
            print(f"  [{insight['confidence'].upper()}] {insight['headline']}")
            print(f"    Key stat: {insight['key_stat']}")
            print(f"    {insight['reasoning']}")
            if insight.get("action"):
                print(f"    Try this next: {insight['action']}")
            print()
        print(f"  Top-performing subject lines:")
        for ex in data.get("top_performing_examples", []):
            print(f"    • {ex}")
        print(f"\n  Data quality note: {data['data_quality_note']}")

    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    playbook = build_playbook(days=365)
    _print_playbook(playbook)
