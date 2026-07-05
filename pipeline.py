"""
SendSmart Pipeline Association.

For a given HubSpot email campaign, finds engaged contacts (opens + clicks),
matches them to Salesforce, and rolls up associated pipeline and revenue.

Uses:
  - HubSpot Email Events API v1 for engaged contacts
  - Salesforce simple_salesforce for Contact + Opportunity queries

Internal @medallion.co addresses are excluded throughout.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from simple_salesforce import Salesforce

load_dotenv()

INTERNAL_DOMAIN = "medallion.co"


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
    contact_role: Optional[str] = None
    created_post_send: bool = False


@dataclass
class PipelineResult:
    campaign_id: str
    campaign_subject: str
    send_date: str

    total_engaged: int         # excluding internal
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
                }
                for o in top_opps
            ],
        }


# ─── HubSpot ─────────────────────────────────────────────────────────────────

def _get_engaged_emails(campaign_id: str, hs_token: str) -> tuple[set[str], str, str]:
    """
    Returns (external_emails, subject, send_date_iso).
    Excludes internal @medallion.co addresses.
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

    # Get engaged contacts
    emails: set[str] = set()
    for event_type in ["OPEN", "CLICK"]:
        offset = None
        while True:
            params = {"campaignId": campaign_id, "eventType": event_type, "limit": 1000}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{base}/email/public/v1/events", headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            for ev in data.get("events", []):
                email = ev.get("recipient", "").lower()
                if email and not email.endswith(f"@{INTERNAL_DOMAIN}"):
                    emails.add(email)
            if not data.get("hasMore"):
                break
            offset = data.get("offset")

    return emails, subject, send_date


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
                contact_role=r.get("Role"),
                created_post_send=created > send_date,
            ))
    return opps


# ─── Main entry point ─────────────────────────────────────────────────────────

def analyze_campaign_pipeline(
    campaign_id: str,
    hs_token: Optional[str] = None,
) -> PipelineResult:
    hs_token = hs_token or os.environ["HUBSPOT_ACCESS_TOKEN"]

    print(f"Fetching engaged contacts for campaign {campaign_id}…")
    emails, subject, send_date = _get_engaged_emails(campaign_id, hs_token)
    print(f"  {len(emails)} external engaged contacts | send date: {send_date}")

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

    # Top open opps (contact-level, sorted by amount)
    top = sorted([o for o in result.contact_opps if not o.is_closed],
                 key=lambda o: -o.amount)[:5]
    if top:
        print("  ── Top Open Opportunities (contact-level) ──")
        for o in top:
            post = " ★" if o.created_post_send else ""
            print(f"     ${o.amount:>10,.0f}  {o.stage:<28} {o.account_name}{post}")
    print()
    print("  ★ = created after email send date — stronger signal")
    print("  Note: labeled 'associated', not attributed")
    print("=" * 65)


if __name__ == "__main__":
    # Test with Michigan IMLC campaign
    result = analyze_campaign_pipeline("400454846")
    print_pipeline_report(result)
