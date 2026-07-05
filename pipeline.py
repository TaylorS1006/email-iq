"""
SendSmart Pipeline Association.

For a given HubSpot email campaign, finds contacts who CLICKED (opens no
longer qualify as engagement anywhere in this module), matches them to
Salesforce, and rolls up associated pipeline and revenue — both account-level
(any open opp at an account with a clicked contact) and contact-level (that
specific person is on the deal).

Contact-level opportunities are further sorted into signal tiers, aligned to
Medallion's existing 60-day attribution window:
  - "directly_followed"        — the opportunity was created within 60 days
                                  after the contact's click, AND a rep
                                  Task/Event ties the contact or account to
                                  that window (corroborated).
  - "followed_uncorroborated"  — same 60-day match, but no rep Task/Event
                                  found in that window.
  - "no_signal"                — the opp falls outside the 60-day window.
Account-level opportunities are untouched by this tiering — they remain
"any open opp at a matched account," independent of window/rep signal.

These are association signals, not causal attribution — no tier implies the
email "caused" or should be credited with the deal.

Uses:
  - HubSpot Email Events API v1 for clicked contacts and, where available,
    per-event click timestamps
  - Salesforce simple_salesforce for Contact, Opportunity, Task, and Event
    queries

Internal @medallion.co addresses are excluded throughout.
"""

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from simple_salesforce import Salesforce

load_dotenv()

INTERNAL_DOMAIN = "medallion.co"

# Aligns with Medallion's existing 60-day attribution window.
SIGNAL_WINDOW_DAYS = 60

# Grace period after an opportunity's creation during which a rep Task/Event
# still counts as corroborating (reps often log the touch just after creating
# the record, not strictly before).
CORROBORATION_GRACE_DAYS = 2

# Honest, non-causal labels for contact-level signal tiers — never "caused"
# or "attributed".
TIER_LABELS = {
    "directly_followed": "Directly followed",
    "followed_uncorroborated": "Followed, uncorroborated",
    "no_signal": "No qualifying signal",
}


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class EngagedContact:
    email: str


@dataclass
class SFContact:
    contact_id: str
    name: str
    email: str
    account_id: str
    account_name: str


@dataclass
class Opportunity:
    opp_id: str
    name: str
    stage: str
    opp_type: Optional[str]
    amount: float
    is_closed: bool
    is_won: bool
    created_date: str      # ISO date string YYYY-MM-DD
    close_date: str
    account_id: str
    account_name: str
    contact_level: bool    # True = this specific contact is on the deal
    contact_id: Optional[str] = None     # contact-level only — the OppContactRole contact
    contact_role: Optional[str] = None
    created_post_send: bool = False      # created any time after send date (no day limit)

    # Contact-level signal tiering (None for account-level opportunities).
    signal_tier: Optional[str] = None       # "directly_followed" | "followed_uncorroborated" | "no_signal"
    corroborated: Optional[bool] = None     # only set when signal_tier is one of the two "followed" tiers
    click_date_used: Optional[str] = None   # ISO date of the click signal, if the contact clicked at all
    click_date_source: Optional[str] = None  # "hubspot_click_event" | "campaign_send_date_fallback"


@dataclass
class PipelineResult:
    campaign_id: str
    campaign_subject: str
    send_date: str

    total_engaged: int         # clicked contacts, excluding internal (opens no longer count)
    total_matched: int
    match_rate: float

    account_opps: list[Opportunity] = field(default_factory=list)
    contact_opps: list[Opportunity] = field(default_factory=list)
    matched_contacts: list[SFContact] = field(default_factory=list)

    @property
    def all_opps(self) -> list[Opportunity]:
        seen = {o.opp_id for o in self.contact_opps}
        return self.contact_opps + [o for o in self.account_opps if o.opp_id not in seen]

    def _sum(self, opps, *, closed=None, won=None, post_send=None, contact_level=None):
        subset = opps
        if closed is not None:
            subset = [o for o in subset if o.is_closed == closed]
        if won is not None:
            subset = [o for o in subset if o.is_won == won]
        if post_send is not None:
            subset = [o for o in subset if o.created_post_send == post_send]
        if contact_level is not None:
            subset = [o for o in subset if o.contact_level == contact_level]
        return subset, sum(o.amount for o in subset)

    def to_dict(self) -> dict:
        co = self.contact_opps
        ao = self.account_opps
        co_open  = [o for o in co if not o.is_closed]
        co_won   = [o for o in co if o.is_won]
        co_post  = [o for o in co if o.created_post_send and not o.is_closed]
        ao_open  = [o for o in ao if not o.is_closed]
        ao_won   = [o for o in ao if o.is_won]
        ao_post  = [o for o in ao if o.created_post_send and not o.is_closed]
        top_opps = sorted(co_open, key=lambda o: -o.amount)[:10]
        return {
            "send_date": self.send_date,
            "total_engaged": self.total_engaged,
            "total_matched": self.total_matched,
            "match_rate": round(self.match_rate, 4),
            # Matched contact emails and the full opportunity list (deduped within
            # this campaign via all_opps) are included so the dashboard can merge
            # multiple campaigns without double-counting a shared contact or deal.
            "matched_emails": sorted({c.email for c in self.matched_contacts}),
            "opportunities": [
                {
                    "id": o.opp_id,
                    "account": o.account_name,
                    "account_id": o.account_id,
                    "name": o.name,
                    "stage": o.stage,
                    "amount": o.amount,
                    "is_closed": o.is_closed,
                    "is_won": o.is_won,
                    "post_send": o.created_post_send,
                    "contact_level": o.contact_level,
                    "signal_tier": o.signal_tier,
                    "corroborated": o.corroborated,
                    "click_date_used": o.click_date_used,
                    "click_date_source": o.click_date_source,
                }
                for o in self.all_opps
            ],
            "contact_open_count": len(co_open),
            "contact_open_value": sum(o.amount for o in co_open),
            "contact_won_count": len(co_won),
            "contact_won_value": sum(o.amount for o in co_won),
            "contact_post_count": len(co_post),
            "contact_post_value": sum(o.amount for o in co_post),
            "account_open_count": len(ao_open),
            "account_open_value": sum(o.amount for o in ao_open),
            "account_won_count": len(ao_won),
            "account_won_value": sum(o.amount for o in ao_won),
            "account_post_count": len(ao_post),
            "account_post_value": sum(o.amount for o in ao_post),
            "top_opps": [
                {
                    "account": o.account_name,
                    "name": o.name,
                    "stage": o.stage,
                    "amount": o.amount,
                    "post_send": o.created_post_send,
                    "signal_tier": o.signal_tier,
                    "corroborated": o.corroborated,
                }
                for o in top_opps
            ],
        }


# ─── HubSpot ─────────────────────────────────────────────────────────────────

def _parse_event_dt(value) -> Optional[datetime]:
    """Parse a HubSpot event 'created' timestamp (epoch ms) to a UTC datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass
class EngagementDetail:
    clicked_emails: set[str]          # engagement = clicked, excluding internal — opens no longer count
    click_dates: dict[str, datetime]  # email -> earliest CLICK event datetime (UTC), only where HubSpot gave a parseable timestamp
    subject: str
    send_date: str                    # YYYY-MM-DD


def _get_clicked_emails(campaign_id: str, hs_token: str) -> EngagementDetail:
    """
    Fetches CLICK-only engagement for one campaign (opens are not fetched —
    they no longer factor into Pipeline Association at all). click_dates
    holds the earliest click timestamp per contact where HubSpot's event
    payload included a parseable 'created' field; contacts who clicked but
    lack a usable timestamp are still in clicked_emails, and callers fall
    back to the campaign send date for them. Excludes internal
    @medallion.co addresses throughout.
    """
    headers = {"Authorization": f"Bearer {hs_token}"}
    base = "https://api.hubapi.com"

    # Get email metadata (subject + send date)
    subject, send_date = "", ""
    after = None
    while True:
        params = {"limit": 100, "sort": "-publishDate"}
        if after:
            params["after"] = after
        r = requests.get(f"{base}/marketing/v3/emails", headers=headers, params=params)
        r.raise_for_status()
        body = r.json()
        for email in body.get("results", []):
            if campaign_id in str(email.get("allEmailCampaignIds", [])):
                subject = email.get("subject", "")
                raw_date = email.get("publishDate") or email.get("sendDate") or ""
                send_date = raw_date[:10]  # YYYY-MM-DD
                break
        if subject or not body.get("paging", {}).get("next", {}).get("after"):
            break
        after = body["paging"]["next"]["after"]

    # Get clicked contacts
    clicked: set = set()
    click_dates: dict = {}
    offset = None
    while True:
        params = {"campaignId": campaign_id, "eventType": "CLICK", "limit": 1000}
        if offset:
            params["offset"] = offset
        r = requests.get(f"{base}/email/public/v1/events", headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        for ev in data.get("events", []):
            email = ev.get("recipient", "").lower()
            if not email or email.endswith(f"@{INTERNAL_DOMAIN}"):
                continue
            clicked.add(email)
            ts = _parse_event_dt(ev.get("created"))
            if ts and (email not in click_dates or ts < click_dates[email]):
                click_dates[email] = ts
        if not data.get("hasMore"):
            break
        offset = data.get("offset")

    return EngagementDetail(clicked, click_dates, subject, send_date)


# ─── Salesforce ──────────────────────────────────────────────────────────────

def _sf_connect() -> Salesforce:
    return Salesforce(
        username=os.environ["SF_USERNAME"],
        password=os.environ["SF_PASSWORD"],
        security_token=os.environ["SF_SECURITY_TOKEN"],
        domain=os.environ.get("SF_DOMAIN", "login"),
    )


def _chunk(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def _match_contacts(sf: Salesforce, emails: set[str]) -> list[SFContact]:
    contacts = []
    for batch in _chunk(list(emails), 100):
        quoted = ", ".join(f"'{e}'" for e in batch)
        result = sf.query(
            f"SELECT Id, Name, Email, AccountId, Account.Name "
            f"FROM Contact WHERE Email IN ({quoted})"
        )
        for r in result["records"]:
            if not r.get("AccountId"):
                continue
            contacts.append(SFContact(
                contact_id=r["Id"],
                name=r["Name"],
                email=r["Email"].lower(),
                account_id=r["AccountId"],
                account_name=(r.get("Account") or {}).get("Name", ""),
            ))
    return contacts


def _fetch_account_opps(sf: Salesforce, account_ids: list[str], send_date: str) -> list[Opportunity]:
    opps = []
    for batch in _chunk(account_ids, 100):
        quoted = ", ".join(f"'{a}'" for a in batch)
        result = sf.query(
            f"SELECT Id, Name, StageName, Type, Opportunity_Amount__c, IsClosed, IsWon, "
            f"CreatedDate, CloseDate, AccountId, Account.Name "
            f"FROM Opportunity "
            f"WHERE AccountId IN ({quoted}) "
            f"AND Ignore_For_Reports_Dashboards__c = false "
            f"ORDER BY CreatedDate DESC LIMIT 500"
        )
        for r in result["records"]:
            created = (r.get("CreatedDate") or "")[:10]
            opps.append(Opportunity(
                opp_id=r["Id"],
                name=r["Name"],
                stage=r["StageName"],
                opp_type=r.get("Type"),
                amount=r.get("Opportunity_Amount__c") or 0,
                is_closed=r["IsClosed"],
                is_won=r["IsWon"],
                created_date=created,
                close_date=(r.get("CloseDate") or "")[:10],
                account_id=r["AccountId"],
                account_name=(r.get("Account") or {}).get("Name", ""),
                contact_level=False,
                created_post_send=created > send_date,
            ))
    return opps


def _fetch_contact_opps(sf: Salesforce, contact_ids: list[str], send_date: str) -> list[Opportunity]:
    opps = []
    seen: set[str] = set()
    for batch in _chunk(contact_ids, 100):
        quoted = ", ".join(f"'{c}'" for c in batch)
        result = sf.query(
            f"SELECT OpportunityId, ContactId, Role, Contact.Email, "
            f"Opportunity.Name, Opportunity.StageName, Opportunity.Type, "
            f"Opportunity.Opportunity_Amount__c, Opportunity.IsClosed, Opportunity.IsWon, "
            f"Opportunity.CreatedDate, Opportunity.CloseDate, "
            f"Opportunity.AccountId, Opportunity.Account.Name "
            f"FROM OpportunityContactRole "
            f"WHERE ContactId IN ({quoted}) "
            f"AND Opportunity.Ignore_For_Reports_Dashboards__c = false"
        )
        for r in result["records"]:
            oid = r["OpportunityId"]
            if oid in seen:
                continue
            seen.add(oid)
            o = r["Opportunity"]
            created = (o.get("CreatedDate") or "")[:10]
            opps.append(Opportunity(
                opp_id=oid,
                name=o["Name"],
                stage=o["StageName"],
                opp_type=o.get("Type"),
                amount=o.get("Opportunity_Amount__c") or 0,
                is_closed=o["IsClosed"],
                is_won=o["IsWon"],
                created_date=created,
                close_date=(o.get("CloseDate") or "")[:10],
                account_id=o["AccountId"],
                account_name=(o.get("Account") or {}).get("Name", ""),
                contact_level=True,
                contact_id=r["ContactId"],
                contact_role=r.get("Role"),
                created_post_send=created > send_date,
            ))
    return opps


def _fetch_rep_activity(sf: Salesforce, contact_ids: list[str], account_ids: list[str]) -> dict[str, list[str]]:
    """
    Looks up Task/Event activity corroborating a contact or account, per:
    WhatId IN (ContactId, AccountId) OR WhoId = ContactId.
    Returns a dict mapping each contact_id/account_id to the list of ISO
    dates (ActivityDate, falling back to CreatedDate) of activity tied to it.
    """
    dates_by_id: dict[str, list[str]] = {}

    def _record(id_: Optional[str], date_str: str):
        if id_ and date_str:
            dates_by_id.setdefault(id_, []).append(date_str)

    contact_id_set = set(contact_ids)
    all_ids = list({*contact_ids, *account_ids})
    if not all_ids:
        return dates_by_id

    for sobject in ("Task", "Event"):
        for batch in _chunk(all_ids, 100):
            contact_batch = [i for i in batch if i in contact_id_set]
            where = "WhatId IN (" + ", ".join(f"'{i}'" for i in batch) + ")"
            if contact_batch:
                where += " OR WhoId IN (" + ", ".join(f"'{c}'" for c in contact_batch) + ")"
            result = sf.query(
                f"SELECT WhatId, WhoId, ActivityDate, CreatedDate FROM {sobject} WHERE {where}"
            )
            for r in result["records"]:
                date_str = r.get("ActivityDate") or (r.get("CreatedDate") or "")[:10]
                _record(r.get("WhatId"), date_str)
                _record(r.get("WhoId"), date_str)

    return dates_by_id


def _apply_signal_tiers(
    contact_opps: list[Opportunity],
    contacts: list[SFContact],
    engagement: EngagementDetail,
    send_date: str,
    sf: Salesforce,
) -> None:
    """
    Mutates contact_opps in place, assigning signal_tier / corroborated /
    click_date_used / click_date_source.

    Every contact here already clicked — matching upstream (analyze_campaign_
    pipeline) is click-only, so there's no "opened but didn't click" case to
    filter out. An opp is a candidate "followed" tier when its CreatedDate
    falls within [click_date, click_date + SIGNAL_WINDOW_DAYS]. Per-contact
    click timestamps come from HubSpot event data where available; falls
    back to the campaign send date otherwise. Candidates are then
    corroborated against rep Task/Event activity tied to the contact or
    account within that same window (+ a small grace period after the opp's
    creation, since reps often log the touch just after creating the
    record). Opps outside the 60-day window land in "no_signal": still
    shown, just labeled as a weaker/no signal, never hidden.
    """
    email_by_contact_id = {c.contact_id: c.email for c in contacts}
    fallback_send_dt: Optional[datetime] = None
    try:
        fallback_send_dt = datetime.strptime(send_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    candidates: list[Opportunity] = []
    for o in contact_opps:
        email = email_by_contact_id.get(o.contact_id)
        if not email:
            o.signal_tier = "no_signal"
            continue

        click_dt = engagement.click_dates.get(email)
        if click_dt is not None:
            o.click_date_used = click_dt.date().isoformat()
            o.click_date_source = "hubspot_click_event"
        elif fallback_send_dt is not None:
            click_dt = fallback_send_dt
            o.click_date_used = fallback_send_dt.date().isoformat()
            o.click_date_source = "campaign_send_date_fallback"
        else:
            o.signal_tier = "no_signal"
            continue

        try:
            created_dt = datetime.strptime(o.created_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            o.signal_tier = "no_signal"
            continue

        window_end = click_dt + timedelta(days=SIGNAL_WINDOW_DAYS)
        if click_dt <= created_dt <= window_end:
            candidates.append(o)
        else:
            o.signal_tier = "no_signal"

    if not candidates:
        return

    contact_ids = list({o.contact_id for o in candidates if o.contact_id})
    account_ids = list({o.account_id for o in candidates if o.account_id})
    activity_dates = _fetch_rep_activity(sf, contact_ids, account_ids)

    for o in candidates:
        click_dt = datetime.fromisoformat(o.click_date_used).replace(tzinfo=timezone.utc)
        created_dt = datetime.strptime(o.created_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        grace_end = created_dt + timedelta(days=CORROBORATION_GRACE_DAYS)

        candidate_dates = activity_dates.get(o.contact_id, []) + activity_dates.get(o.account_id, [])
        corroborated = False
        for d in candidate_dates:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if click_dt <= dt <= grace_end:
                corroborated = True
                break

        o.corroborated = corroborated
        o.signal_tier = "directly_followed" if corroborated else "followed_uncorroborated"


# ─── Main entry point ─────────────────────────────────────────────────────────

def analyze_campaign_pipeline(
    campaign_id: str,
    hs_token: Optional[str] = None,
) -> PipelineResult:
    hs_token = hs_token or os.environ["HUBSPOT_ACCESS_TOKEN"]

    print(f"Fetching clicked contacts for campaign {campaign_id}…")
    engagement = _get_clicked_emails(campaign_id, hs_token)
    emails, subject, send_date = engagement.clicked_emails, engagement.subject, engagement.send_date
    print(f"  {len(emails)} external clicked contacts | send date: {send_date}")

    print("Matching to Salesforce contacts…")
    sf = _sf_connect()
    contacts = _match_contacts(sf, emails)
    match_rate = len(contacts) / len(emails) if emails else 0
    print(f"  {len(contacts)}/{len(emails)} matched ({match_rate:.0%})")

    account_ids = list({c.account_id for c in contacts})
    contact_ids = [c.contact_id for c in contacts]

    print("Fetching account-level opportunities…")
    account_opps = _fetch_account_opps(sf, account_ids, send_date)
    print(f"  {len(account_opps)} opps across {len(account_ids)} accounts")

    print("Fetching contact-level opportunities…")
    contact_opps = _fetch_contact_opps(sf, contact_ids, send_date)
    print(f"  {len(contact_opps)} unique opps with this contact on the deal")

    print(f"Applying {SIGNAL_WINDOW_DAYS}-day click-signal tiers + rep activity corroboration…")
    _apply_signal_tiers(contact_opps, contacts, engagement, send_date, sf)
    tier_counts = Counter(o.signal_tier for o in contact_opps)
    print(f"  {dict(tier_counts)}")

    return PipelineResult(
        campaign_id=campaign_id,
        campaign_subject=subject,
        send_date=send_date,
        total_engaged=len(emails),
        total_matched=len(contacts),
        match_rate=match_rate,
        account_opps=account_opps,
        contact_opps=contact_opps,
        matched_contacts=contacts,
    )


def print_pipeline_report(result: PipelineResult) -> None:
    r = result
    print()
    print("=" * 65)
    print("  PIPELINE ASSOCIATED WITH ENGAGED CONTACTS")
    print("=" * 65)
    print(f"  Campaign : {r.campaign_subject}")
    print(f"  Send date: {r.send_date}")
    print(f"  Contacts : {r.total_matched}/{r.total_engaged} matched ({r.match_rate:.0%})")
    print()

    def section(label, opps, contact_level=None):
        subset = opps
        if contact_level is not None:
            subset = [o for o in opps if o.contact_level == contact_level]
        open_o  = [o for o in subset if not o.is_closed]
        won_o   = [o for o in subset if o.is_won]
        post_o  = [o for o in subset if o.created_post_send and not o.is_closed]

        print(f"  ── {label} ──")
        print(f"     Open pipeline : {len(open_o):>3} opps  ${sum(o.amount for o in open_o):>12,.0f}")
        print(f"     Won            : {len(won_o):>3} opps  ${sum(o.amount for o in won_o):>12,.0f}")
        if post_o:
            print(f"     ★ Created post-send (open): {len(post_o)} opps  ${sum(o.amount for o in post_o):>10,.0f}")
            for o in sorted(post_o, key=lambda x: -x.amount)[:3]:
                print(f"       • {o.account_name}: {o.name[:45]} — ${o.amount:,.0f} [{o.stage}]")
        print()

    section("Contact-level (this person is on the deal)", result.contact_opps)
    section("Account-level (any opp at matched accounts)", result.account_opps)

    tier_counts = Counter(o.signal_tier for o in result.contact_opps)
    print("  ── Signal tiers (contact-level, 60-day click window) ──")
    for tier in ("directly_followed", "followed_uncorroborated", "no_signal"):
        print(f"     {TIER_LABELS[tier]:<28} {tier_counts.get(tier, 0)}")
    print()

    # Top open opps (contact-level, sorted by amount)
    top = sorted([o for o in result.contact_opps if not o.is_closed],
                 key=lambda o: -o.amount)[:5]
    if top:
        print("  ── Top Open Opportunities (contact-level) ──")
        for o in top:
            post = " ★" if o.created_post_send else ""
            tier = TIER_LABELS.get(o.signal_tier, "")
            print(f"     ${o.amount:>10,.0f}  {o.stage:<28} {o.account_name}{post}  [{tier}]")
    print()
    print("  ★ = created any time after email send date (no day limit)")
    print("  Signal tiers = association signals, not causal attribution — see TIER_LABELS")
    print("  Note: labeled 'associated', not attributed")
    print("=" * 65)


if __name__ == "__main__":
    # Test with Michigan IMLC campaign
    result = analyze_campaign_pipeline("400454846")
    print_pipeline_report(result)
