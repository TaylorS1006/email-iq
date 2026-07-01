"""
Smoke test: pull the 5 most recent sent emails and print them.

Run with:
    python test_client.py
"""

from hubspot_client import fetch_emails


def main():
    print("Fetching last 90 days of sent marketing emails (limit 5)...\n")
    emails = fetch_emails(days=90, limit=5)

    if not emails:
        print("No emails returned. Check your token and that emails exist in this date range.")
        return

    for i, e in enumerate(emails, 1):
        send_date = e.send_date.strftime("%Y-%m-%d") if e.send_date else "unknown"
        print(f"{'─' * 60}")
        print(f"  #{i}  {e.name or '(no name)'}")
        print(f"  Subject   : {e.subject}")
        print(f"  Type      : {e.email_type or 'n/a'}")
        print(f"  Sent date : {send_date}")
        print(f"  Sent      : {e.sent:,}")
        print(f"  Delivered : {e.delivered:,}")
        print(f"  Opens     : {e.unique_opens:,}  ({e.open_rate:.1%} open rate)")
        print(f"  Clicks    : {e.unique_clicks:,}  ({e.click_rate:.1%} click rate)")
        print(f"  CTOR      : {e.click_to_open_rate:.1%}")
        print()

    print(f"{'─' * 60}")
    print(f"Total records returned: {len(emails)}")


if __name__ == "__main__":
    main()
