"""
HubSpot marketing email client.

Email list: Marketing Email v3 API  (/marketing/v3/emails)
Stats:      Email Campaigns v1 API  (/email/public/v1/campaigns/{id})

The v3 statistics endpoints are not available on all HubSpot plans; the v1
campaigns API is the reliable source for send/open/click counts.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE = "https://api.hubapi.com"

# Internal addresses excluded from recipient-facing fetches (matches
# pipeline.py's INTERNAL_DOMAIN — kept as a separate constant here since
# pipeline.py's tiering logic is deliberately isolated from this module).
_INTERNAL_DOMAIN = "medallion.co"

# Emails that have actually been sent (batch) or are live (automated)
_SENT_STATES = {"PUBLISHED", "AUTOMATED"}

# Canonical content types parsed from the pipe-delimited naming convention:
# "Audience | Segment | Type | Stage | Description"
CONTENT_TYPES = {
    "webinar",
    "in-person event",
    "micro event",
    "product release",
    "newsletter",
    "content",
    "blog",
    "case study",
    "announcement",
    "survey",
    "onboarding",
    "virtual event",
}

# Aliases to normalize variations seen in email names
_TYPE_ALIASES: dict[str, str] = {
    "in person event": "in-person event",
    "in-person": "in-person event",
    "virtual": "virtual event",
    "micro-event": "micro event",
    "product launch": "product release",
    "release": "product release",
    "case-study": "case study",
    "awareness": "announcement",
}


def _parse_content_type(name: str) -> Optional[str]:
    """Extract the content type from a pipe-delimited email name.

    Scans all pipe segments (not just position 3) to handle naming convention
    variations. Returns a canonical CONTENT_TYPES value, or None if no
    recognized type is found.
    """
    for part in name.split("|"):
        raw = part.strip().lower()
        if raw in CONTENT_TYPES:
            return raw
        if raw in _TYPE_ALIASES:
            return _TYPE_ALIASES[raw]
    return None


@dataclass
class EmailRecord:
    email_id: str
    name: str
    subject: str
    email_type: Optional[str]        # HubSpot send type: BATCH_EMAIL, AUTOMATED_EMAIL, etc.
    content_type: Optional[str]      # Parsed from name convention: webinar, newsletter, etc.
    campaign_name: Optional[str]     # HubSpot campaign name (e.g. "Elevate 2026")
    send_date: Optional[datetime]
    campaign_ids: list[str] = field(default_factory=list)

    sent: int = 0
    delivered: int = 0
    opens: int = 0
    unique_opens: int = 0
    clicks: int = 0
    unique_clicks: int = 0
    bounced: int = 0
    unsubscribed: int = 0

    open_rate: float = 0.0          # unique opens / delivered
    click_rate: float = 0.0         # unique clicks / delivered
    click_to_open_rate: float = 0.0 # unique clicks / unique opens
    bounce_rate: float = 0.0        # bounced / sent
    unsubscribe_rate: float = 0.0   # unsubscribed / delivered
    delivered_rate: float = 0.0     # delivered / sent


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return s


def _parse_dt(value: Optional[Union[str, int]]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def _fetch_campaign_stats(session: requests.Session, campaign_id: str) -> dict:
    """Fetch send/open/click counters for one campaign from the v1 API."""
    r = session.get(f"{_BASE}/email/public/v1/campaigns/{campaign_id}")
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def _merge_stats(records: list[dict]) -> dict:
    """Sum counter fields across multiple campaign records."""
    totals: dict[str, int] = {}
    for rec in records:
        for k, v in rec.get("counters", {}).items():
            if isinstance(v, (int, float)):
                totals[k] = totals.get(k, 0) + int(v)
    return totals


def _build_record(email: dict, stats: dict) -> EmailRecord:
    # stats is already the merged counters dict (keys: sent, delivered, open, click, ...)
    sent = int(stats.get("sent", 0))
    delivered = int(stats.get("delivered", sent))
    opens = int(stats.get("open", 0))
    unique_opens = opens          # v1 API doesn't expose unique opens separately
    clicks = int(stats.get("click", 0))
    unique_clicks = clicks        # same for clicks
    bounced = int(stats.get("bounce", 0))
    unsubscribed = int(stats.get("unsubscribed", 0))

    name = email.get("name", "")
    return EmailRecord(
        email_id=str(email.get("id", "")),
        name=name,
        subject=email.get("subject", ""),
        email_type=email.get("type"),
        content_type=_parse_content_type(name),
        campaign_name=email.get("campaignName") or None,
        send_date=_parse_dt(email.get("publishDate") or email.get("sendDate")),
        campaign_ids=email.get("allEmailCampaignIds", []),
        sent=sent,
        delivered=delivered,
        opens=opens,
        unique_opens=unique_opens,
        clicks=clicks,
        unique_clicks=unique_clicks,
        bounced=bounced,
        unsubscribed=unsubscribed,
        open_rate=_rate(unique_opens, delivered),
        click_rate=_rate(unique_clicks, delivered),
        click_to_open_rate=_rate(unique_clicks, unique_opens),
        bounce_rate=_rate(bounced, sent),
        unsubscribe_rate=_rate(unsubscribed, delivered),
        delivered_rate=_rate(delivered, sent),
    )


# processingType -> human label. MANUAL lists are hand-curated (static);
# DYNAMIC lists re-evaluate their filter continuously (active/smart);
# SNAPSHOT lists are a one-time static capture of a dynamic list.
_LIST_TYPE_LABELS = {
    "MANUAL": "static",
    "DYNAMIC": "active",
    "SNAPSHOT": "static snapshot",
}


def _list_summary(lst: dict) -> dict:
    """
    Extract the subset of a HubSpot list object useful for Co-write context.

    Keys are camelCase to match cloudflare-worker/src/worker.js's
    summarizeList() — the same Co-write frontend JS (COWRITE_JS in
    report.py) consumes this response from both the local Flask dev server
    and the published Cloudflare Worker, so the two must stay shape-identical.
    """
    extra = lst.get("additionalProperties", {}) or {}
    size = extra.get("hs_list_size")
    return {
        "id": str(lst.get("listId")),
        "name": lst.get("name", ""),
        "size": int(size) if size is not None else None,
        "listType": _LIST_TYPE_LABELS.get(lst.get("processingType"), None),
        "lastRecordAddedAt": extra.get("hs_last_record_added_at"),
        "updatedAt": lst.get("updatedAt"),
    }


def fetch_lists(*, token: Optional[str] = None) -> list[dict]:
    """
    Return HubSpot contact lists as [{"id", "name", "size", "list_type",
    "last_record_added_at", "updated_at"}, ...], sorted by name.

    Uses the CRM Lists v3 search endpoint (POST /crm/v3/lists/search with an
    empty query, which matches every list regardless of object type). This
    requires the private app token to have the `crm.lists.read` scope —
    HubSpot introduced this as a separate scope from the older v1 Contact
    Lists API, so an existing token that only has contact/email scopes will
    likely need it added under Settings → Integrations → Private Apps →
    (this app) → Scopes, followed by regenerating the token.

    HubSpot includes hs_list_size, hs_last_record_added_at, processingType,
    and updatedAt in the default response (no extra per-list call needed) —
    that's member count, a recency/"warmth" signal, and static-vs-active
    type. Any field that's missing/empty on a given list comes back None so
    callers can fall back to name-only rather than guessing.

    Raises PermissionError with HubSpot's raw response text if the token
    lacks the scope, so the caller can surface an actionable message.
    """
    token = token or os.environ["HUBSPOT_ACCESS_TOKEN"]
    session = _session(token)

    lists: list[dict] = []
    offset = 0
    while True:
        resp = session.post(
            f"{_BASE}/crm/v3/lists/search",
            json={"query": "", "offset": offset, "count": 250},
        )
        if resp.status_code == 403:
            raise PermissionError(
                "HubSpot returned 403 fetching lists — the private app token "
                "likely needs the 'crm.lists.read' scope added, then must be "
                f"regenerated. Raw response: {resp.text}"
            )
        resp.raise_for_status()
        body = resp.json()

        page = body.get("lists", [])
        for lst in page:
            lists.append(_list_summary(lst))

        if not page or not body.get("hasMore"):
            break
        offset += len(page)

    return sorted(lists, key=lambda l: l["name"].lower())


def fetch_event_recipients(
    campaign_id: str,
    event_type: str,
    *,
    token: Optional[str] = None,
) -> set[str]:
    """
    Return the set of recipient email addresses with an event of `event_type`
    (e.g. "DELIVERED", "OPEN", "CLICK") for one campaign, via the same
    /email/public/v1/events endpoint pipeline.py's _get_clicked_emails uses
    for CLICK. Excludes internal @medallion.co addresses.
    """
    token = token or os.environ["HUBSPOT_ACCESS_TOKEN"]
    session = _session(token)

    recipients: set[str] = set()
    offset = None
    while True:
        params: dict = {"campaignId": campaign_id, "eventType": event_type, "limit": 1000}
        if offset:
            params["offset"] = offset
        r = session.get(f"{_BASE}/email/public/v1/events", params=params)
        r.raise_for_status()
        data = r.json()
        for ev in data.get("events", []):
            email = ev.get("recipient", "").lower()
            if not email or email.endswith(f"@{_INTERNAL_DOMAIN}"):
                continue
            recipients.add(email)
        if not data.get("hasMore"):
            break
        offset = data.get("offset")

    return recipients


def fetch_contacts_by_email(
    emails: list[str],
    *,
    token: Optional[str] = None,
    properties: Optional[list[str]] = None,
) -> dict[str, dict]:
    """
    Batch-fetch contact properties by email address via
    POST /crm/v3/objects/contacts/batch/read (idProperty=email), chunked by
    100 per HubSpot's batch limit.

    Returns {email.lower(): {property_name: value, ...}}. Emails with no
    matching contact are simply absent from the result.
    """
    token = token or os.environ["HUBSPOT_ACCESS_TOKEN"]
    session = _session(token)
    properties = properties or ["jobtitle"]

    results: dict[str, dict] = {}
    unique_emails = list({e.lower() for e in emails if e})

    for batch in (unique_emails[i:i + 100] for i in range(0, len(unique_emails), 100)):
        resp = session.post(
            f"{_BASE}/crm/v3/objects/contacts/batch/read",
            json={
                "idProperty": "email",
                "properties": properties,
                "inputs": [{"id": email} for email in batch],
            },
        )
        resp.raise_for_status()
        for record in resp.json().get("results", []):
            props = record.get("properties", {})
            email = (props.get("email") or "").lower()
            if email:
                results[email] = props

    return results


def fetch_emails(
    *,
    token: Optional[str] = None,
    days: int = 90,
    email_type: Optional[str] = None,
    content_type: Optional[str] = None,
    include_automated: bool = True,
    limit: Optional[int] = None,
) -> list[EmailRecord]:
    """
    Return sent marketing emails from the last `days` days.

    Args:
        token: HubSpot private app token. Defaults to HUBSPOT_ACCESS_TOKEN env var.
        days: Look-back window. Pass 0 for all time.
        email_type: Filter by HubSpot send type, e.g. "BATCH_EMAIL".
        content_type: Filter by parsed content type, e.g. "webinar". See CONTENT_TYPES.
        include_automated: Include AUTOMATED_EMAIL entries (default True).
        limit: Cap total results (useful for testing).
    """
    token = token or os.environ["HUBSPOT_ACCESS_TOKEN"]
    session = _session(token)

    cutoff: Optional[datetime] = None
    if days > 0:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    params: dict = {"limit": 100, "sort": "-publishDate"}
    if email_type:
        params["type"] = email_type

    records: list[EmailRecord] = []
    after: Optional[str] = None
    done = False

    while not done:
        if after:
            params["after"] = after
        else:
            params.pop("after", None)

        resp = session.get(f"{_BASE}/marketing/v3/emails", params=params)
        resp.raise_for_status()
        body = resp.json()

        for email in body.get("results", []):
            state = email.get("state", "")
            if state not in _SENT_STATES:
                continue
            if not include_automated and email.get("type") == "AUTOMATED_EMAIL":
                continue

            send_date = _parse_dt(email.get("publishDate") or email.get("sendDate"))
            if cutoff and send_date and send_date < cutoff:
                done = True
                break

            campaign_ids = email.get("allEmailCampaignIds") or []
            stat_records = []
            for cid in campaign_ids:
                try:
                    stat_records.append(_fetch_campaign_stats(session, cid))
                except requests.HTTPError as exc:
                    print(f"  [warn] stats fetch failed for campaign {cid}: {exc}")

            stats = _merge_stats(stat_records)
            record = _build_record(email, stats)
            if content_type and record.content_type != content_type.lower():
                continue
            records.append(record)

            if limit and len(records) >= limit:
                return records

        if not done:
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after:
                done = True

    return records
