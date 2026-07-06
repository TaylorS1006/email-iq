"""
Persona classification + per-email bucketing for the Playbook page.

Pipeline:
  1. Fetch DELIVERED/OPEN/CLICK recipient sets per campaign (HubSpot events).
  2. Classify every delivered contact into a persona via job_function_1,
     falling back to jobtitle keyword matching for the catch-all bucket.
  3. Bucket each EmailRecord's delivered/opens/clicks counts per persona and
     build a synthetic EmailRecord (recomputed rates) analyzer.py can run
     the existing Claude analysis against unchanged.

Tunable classification data lives in persona_config.py, not here.
"""

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Optional

from hubspot_client import EmailRecord, fetch_contacts_by_email, fetch_event_recipients
from persona_config import JOBTITLE_PERSONA_KEYWORDS, REAL_PERSONAS, UNCLASSIFIED

_REAL_PERSONA_SET = set(REAL_PERSONAS)

# Word-boundary regex per persona, built once from JOBTITLE_PERSONA_KEYWORDS.
# Plain substring matching would let short acronyms like "cto"/"coo" false-
# positive inside unrelated words (e.g. "cto" inside "director") — \b avoids
# that while still matching multi-word phrases like "chief financial".
_KEYWORD_PATTERNS: dict[str, re.Pattern] = {
    persona: re.compile(r"\b(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\b")
    for persona, keywords in JOBTITLE_PERSONA_KEYWORDS.items()
}


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


@dataclass
class PersonaCounts:
    delivered: int = 0
    opens: int = 0
    clicks: int = 0
    clean_count: int = 0     # delivered contacts classified via job_function_1 directly
    fallback_count: int = 0  # delivered contacts classified via the jobtitle fallback


@dataclass
class PersonaData:
    # email_id -> persona -> PersonaCounts, for building persona-scoped EmailRecords
    email_persona_counts: dict[str, dict[str, PersonaCounts]] = field(default_factory=dict)
    # persona -> {"clean_pct": float, "fallback_pct": float, "total_contacts": int}
    persona_confidence: dict[str, dict] = field(default_factory=dict)
    overall_unclassified_pct: float = 0.0


def classify_contact(job_function_1: Optional[str], jobtitle: Optional[str]) -> tuple[str, str]:
    """
    Returns (persona, source) where source is one of:
      "job_function_1"    — clean signal, used as-is
      "jobtitle_fallback"  — job_function_1 was blank/catch-all, jobtitle keyword matched
      "unclassified"        — neither signal produced a persona
    """
    if job_function_1 in _REAL_PERSONA_SET:
        return job_function_1, "job_function_1"

    # job_function_1 missing or OTHER_PROVIDER_BLANK — try the jobtitle fallback.
    if jobtitle:
        lowered = jobtitle.lower()
        for persona, pattern in _KEYWORD_PATTERNS.items():
            if pattern.search(lowered):
                return persona, "jobtitle_fallback"

    return UNCLASSIFIED, "unclassified"


def _persona_email_record(base: EmailRecord, counts: PersonaCounts) -> EmailRecord:
    """Build a synthetic EmailRecord scoped to one persona's recipients."""
    return dataclasses.replace(
        base,
        sent=counts.delivered,
        delivered=counts.delivered,
        opens=counts.opens,
        unique_opens=counts.opens,
        clicks=counts.clicks,
        unique_clicks=counts.clicks,
        bounced=0,
        unsubscribed=0,
        open_rate=_rate(counts.opens, counts.delivered),
        click_rate=_rate(counts.clicks, counts.delivered),
        click_to_open_rate=_rate(counts.clicks, counts.opens),
        bounce_rate=0.0,
        unsubscribe_rate=0.0,
        delivered_rate=1.0 if counts.delivered else 0.0,
    )


def persona_groups_for(
    groups: dict[str, list[EmailRecord]],
    persona_data: PersonaData,
    persona: str,
) -> dict[str, list[EmailRecord]]:
    """
    Build a content_type -> list[EmailRecord] dict scoped to one persona,
    suitable for analyzer._build_playbook_from_groups. Emails with zero
    delivered contacts in this persona are dropped (nothing to analyze).
    """
    result: dict[str, list[EmailRecord]] = {}
    for content_type, emails in groups.items():
        scoped = []
        for e in emails:
            counts = persona_data.email_persona_counts.get(e.email_id, {}).get(persona)
            if counts and counts.delivered > 0:
                scoped.append(_persona_email_record(e, counts))
        if scoped:
            result[content_type] = scoped
    return result


def build_persona_data(
    emails: list[EmailRecord],
    *,
    token: Optional[str] = None,
) -> PersonaData:
    """
    Fetch recipients + engagement for every campaign backing `emails`,
    classify each delivered contact into a persona, and bucket per-email
    counts. `emails` should be exactly the set already feeding the Playbook
    page (e.g. `current` in report.generate_report) so persona data matches
    what's shown.
    """
    print(f"Fetching persona recipient data for {len(emails)} emails…")

    # campaign_id -> {"delivered": set, "opens": set, "clicks": set}
    campaign_events: dict[str, dict[str, set]] = {}

    def _events_for(campaign_id: str) -> dict[str, set]:
        if campaign_id not in campaign_events:
            campaign_events[campaign_id] = {
                "delivered": fetch_event_recipients(campaign_id, "DELIVERED", token=token),
                "opens": fetch_event_recipients(campaign_id, "OPEN", token=token),
                "clicks": fetch_event_recipients(campaign_id, "CLICK", token=token),
            }
        return campaign_events[campaign_id]

    # email_id -> {"delivered": set, "opens": set, "clicks": set} unioned
    # across all of that email's campaign_ids
    per_email_recipients: dict[str, dict[str, set]] = {}
    all_delivered: set[str] = set()

    for e in emails:
        if not e.campaign_ids:
            continue
        merged = {"delivered": set(), "opens": set(), "clicks": set()}
        for cid in e.campaign_ids:
            try:
                ev = _events_for(cid)
            except Exception as exc:
                print(f"  [warn] event fetch failed for campaign {cid}: {exc}")
                continue
            merged["delivered"] |= ev["delivered"]
            merged["opens"] |= ev["opens"]
            merged["clicks"] |= ev["clicks"]
        per_email_recipients[e.email_id] = merged
        all_delivered |= merged["delivered"]

    print(f"  → {len(all_delivered)} unique delivered contacts across {len(campaign_events)} campaigns")

    print("Classifying contacts by job_function_1 / jobtitle…")
    contact_props = fetch_contacts_by_email(
        list(all_delivered), token=token, properties=["email", "job_function_1", "jobtitle"]
    )

    classification: dict[str, tuple[str, str]] = {}
    for email in all_delivered:
        props = contact_props.get(email, {})
        classification[email] = classify_contact(props.get("job_function_1"), props.get("jobtitle"))

    # Global per-persona confidence + overall unclassified %
    source_tally: dict[str, dict[str, int]] = {p: {"job_function_1": 0, "jobtitle_fallback": 0} for p in REAL_PERSONAS}
    unclassified_count = 0
    for persona, source in classification.values():
        if persona == UNCLASSIFIED:
            unclassified_count += 1
        elif source in ("job_function_1", "jobtitle_fallback"):
            source_tally[persona][source] += 1

    persona_confidence: dict[str, dict] = {}
    for persona in REAL_PERSONAS:
        clean = source_tally[persona]["job_function_1"]
        fallback = source_tally[persona]["jobtitle_fallback"]
        total = clean + fallback
        persona_confidence[persona] = {
            "clean_pct": round(100 * clean / total, 1) if total else 0.0,
            "fallback_pct": round(100 * fallback / total, 1) if total else 0.0,
            "total_contacts": total,
        }

    overall_unclassified_pct = (
        round(100 * unclassified_count / len(all_delivered), 1) if all_delivered else 0.0
    )
    print(f"  → overall unclassified: {overall_unclassified_pct}%")

    # Per-email, per-persona counts
    email_persona_counts: dict[str, dict[str, PersonaCounts]] = {}
    for e in emails:
        recip = per_email_recipients.get(e.email_id)
        if not recip:
            continue
        per_persona: dict[str, PersonaCounts] = {}
        for persona in REAL_PERSONAS:
            persona_delivered = {
                email for email in recip["delivered"] if classification.get(email, (None, None))[0] == persona
            }
            if not persona_delivered:
                continue
            counts = PersonaCounts(
                delivered=len(persona_delivered),
                opens=len(persona_delivered & recip["opens"]),
                clicks=len(persona_delivered & recip["clicks"]),
            )
            for email in persona_delivered:
                if classification[email][1] == "job_function_1":
                    counts.clean_count += 1
                else:
                    counts.fallback_count += 1
            per_persona[persona] = counts
        if per_persona:
            email_persona_counts[e.email_id] = per_persona

    return PersonaData(
        email_persona_counts=email_persona_counts,
        persona_confidence=persona_confidence,
        overall_unclassified_pct=overall_unclassified_pct,
    )
