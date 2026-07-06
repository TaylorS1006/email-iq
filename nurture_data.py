"""
Nurture inventory + audience-segment sizing for the Nurture page.

Two data sources feed this page, and they're fundamentally different in
reliability:

  1. KNOWN_NURTURES below — a manually-maintained snapshot of what actually
     exists in HubSpot's Workflows tool. The HubSpot Automation API (v4
     /automation/v4/flows, legacy v3 /automation/v3/workflows) exposes
     workflow definitions but NOT live enrollment counts or step-level email
     engagement — confirmed by hand against this portal, every enrollment/
     performance endpoint tried (v3 .../performance, v2 .../enrollments,
     v4 guessed equivalents) 404s. So unlike the rest of this app, this list
     is static and needs re-verifying against the HubSpot UI periodically,
     not re-pulled automatically.

  2. build_nurture_segments() — a live HubSpot CRM search pull, same
     reliability tier as the rest of this app's contact data.

Because (1) isn't live, this page cannot and does not compute a coverage
percentage — see report._render_nurture_section's coverage_text, which is a
plain-language statement, not a ratio.
"""

from dataclasses import dataclass
from typing import Optional

from hubspot_client import fetch_contact_count, fetch_property_options
from persona_config import REAL_PERSONAS

# Below this, a persona x lifecycle-stage segment isn't a meaningful
# nurture-building opportunity on its own — matches the "100+ contacts"
# judgment call from the scoping pass.
NURTURE_OPPORTUNITY_MIN_SIZE = 100


@dataclass
class KnownNurture:
    name: str
    status: str  # "active" | "disabled"
    step_count: int
    email_count: int
    trigger_description: str


# Manually confirmed against the HubSpot Automation API on 2026-07-06 (v4
# flow IDs 1740060449 / 368702991, mapped to legacy v3 workflow IDs 98016157
# / 42221621 via migrationStatus.flowId). Re-verify in HubSpot's Workflows
# list before trusting this for anything time-sensitive.
KNOWN_NURTURES: list[KnownNurture] = [
    KnownNurture(
        name="Branched 2026 Onboarding Nurture",
        status="active",
        step_count=48,
        email_count=20,
        trigger_description=(
            'List-based enrollment: contacts with import_source_flag = "No Show" '
            'or "Attended", updated recently. Unenrolls on 3 specific form fills.'
        ),
    ),
    KnownNurture(
        name="Industry Nurture Series",
        status="disabled",
        step_count=75,
        email_count=32,
        trigger_description="Manual enrollment — built and content-complete, currently switched off.",
    ),
]


@dataclass
class NurtureSegment:
    persona: str
    lifecycle_stage: str  # display label, not the raw enum value
    count: int


def build_nurture_segments(*, token: Optional[str] = None) -> list[NurtureSegment]:
    """
    Contact counts for every (persona x lifecycle stage) combination that has
    at least one contact, sorted largest-first. Personas are REAL_PERSONAS —
    the same 5-persona taxonomy Playbook uses, excluding the "Other /
    Provider / Blank" catch-all for the same reason it's excluded there (see
    persona_config.py) — it's not a persona anyone would target a nurture at.

    Lifecycle stage labels are fetched live rather than hardcoded: the enum
    values are portal-specific numeric IDs for custom stages and aren't
    stable across HubSpot accounts.
    """
    stage_options = fetch_property_options("contacts", "lifecyclestage", token=token)

    segments: list[NurtureSegment] = []
    for persona in REAL_PERSONAS:
        for stage_value, stage_label in stage_options.items():
            count = fetch_contact_count(
                token=token,
                filters=[
                    {"propertyName": "job_function_1", "operator": "EQ", "value": persona},
                    {"propertyName": "lifecyclestage", "operator": "EQ", "value": stage_value},
                ],
            )
            if count > 0:
                segments.append(NurtureSegment(persona=persona, lifecycle_stage=stage_label, count=count))

    segments.sort(key=lambda s: s.count, reverse=True)
    return segments


def best_insight_for_persona(persona_playbooks: dict, persona: str) -> Optional[dict]:
    """
    Pick the single best insight already generated for this persona by
    analyzer.build_persona_playbooks (the same insight objects rendered on
    the Playbook page) — reused as-is, not regenerated. This is why every
    nurture-angle suggestion on this page carries the "informed by one-off
    send performance" caveat: these insights come from individual campaign
    sends, not from any actual nurture-sequence data.

    Prefers a strong-confidence insight over moderate over none, across all
    of that persona's content types. Returns None if the persona has no
    playbook data at all (e.g. too few sends to analyze).
    """
    groups = persona_playbooks.get(persona, {})
    rank_order = {"strong": 0, "moderate": 1, "none": 2}
    candidates: list[tuple[int, dict, str]] = []
    for content_type, data in groups.items():
        for insight in data.get("insights", []):
            rank = rank_order.get(insight.get("confidence"), 3)
            candidates.append((rank, insight, content_type))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, best_insight, best_ct = candidates[0]
    return {**best_insight, "source_content_type": best_ct}
