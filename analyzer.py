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

_PLAYBOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_line_patterns": {"type": "string"},
        "cta_patterns": {"type": "string"},
        "timing_patterns": {"type": "string"},
        "top_performing_examples": {
            "type": "array",
            "items": {"type": "string"},
        },
        "sample_size_note": {"type": "string"},
    },
    "required": [
        "subject_line_patterns",
        "cta_patterns",
        "timing_patterns",
        "top_performing_examples",
        "sample_size_note",
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
        "  subject_line_patterns: What subject line approaches correlate with higher open rates? "
        "Be specific (e.g. 'questions outperform statements', 'shorter subjects under 50 chars '). "
        "If the data does not support a clear pattern, say so explicitly.",
        "  cta_patterns: What call-to-action or content themes appear in high-performing emails? "
        "If the data is insufficient to determine this, say so explicitly.",
        "  timing_patterns: What send-day or send-time patterns are visible? "
        "If the data does not support a clear pattern, say so explicitly.",
        "  top_performing_examples: List 2-3 actual subject lines from the top-performing emails.",
        "  sample_size_note: Brief note on sample size and any data quality caveats.",
        "",
        "IMPORTANT: Only report patterns actually supported by this data. Do not add generic "
        "email best-practices that are not evidenced here. If the data is noisy or insufficient "
        "to support a pattern for a field, say so in that field.",
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
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": _PLAYBOOK_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


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

    playbook: dict[str, dict] = {}

    for content_type, group in sorted(groups.items(), key=lambda x: -len(x[1])):
        if content_type == "unknown":
            print(f"\nSkipping 'unknown' ({len(group)} emails — no content type parsed)")
            continue

        if len(group) < MIN_SAMPLE_SIZE:
            print(
                f"\n[{content_type}] Insufficient data ({len(group)} emails, need {MIN_SAMPLE_SIZE}+)"
            )
            playbook[content_type] = {
                "status": "insufficient_data",
                "sample_count": len(group),
                "minimum_required": MIN_SAMPLE_SIZE,
            }
            continue

        print(f"\n[{content_type}] Analyzing {len(group)} emails…")
        try:
            result = _analyze_content_type(client, content_type, group)
            result["sample_count"] = len(group)
            playbook[content_type] = result
            print(f"  ✓ Done")
        except Exception as exc:
            print(f"  ✗ Error: {exc}")
            playbook[content_type] = {"status": "error", "error": str(exc)}

    return playbook


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
        print(f"  Subject line patterns:")
        print(f"    {data['subject_line_patterns']}\n")
        print(f"  CTA patterns:")
        print(f"    {data['cta_patterns']}\n")
        print(f"  Timing patterns:")
        print(f"    {data['timing_patterns']}\n")
        print(f"  Top-performing subject lines:")
        for ex in data.get("top_performing_examples", []):
            print(f"    • {ex}")
        print(f"\n  Note: {data['sample_size_note']}")

    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    playbook = build_playbook(days=365)
    _print_playbook(playbook)
