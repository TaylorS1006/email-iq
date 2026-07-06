"""
Persona segmentation config — tune this file, not the code that uses it.

Primary signal is the HubSpot contact property `job_function_1` ("Job
Function"), a clean 6-value enum: the 5 REAL_PERSONAS below plus a catch-all
"Other / Provider / Blank". Contacts landing in that catch-all (or missing
the property entirely) get a jobtitle keyword fallback pass — see
persona_data.classify_contact.
"""

# The 5 personas selectable in the Playbook page's dropdown. Order here is
# the order they appear in the UI (after "All").
REAL_PERSONAS: list[str] = [
    "Operations",
    "Finance",
    "Founder / CEO / President",
    "Compliance / IT",
    "HR / Admin",
]

# job_function_1's catch-all enum value — NOT a persona someone would filter
# to on purpose, so it's excluded from the dropdown.
OTHER_PROVIDER_BLANK = "Other / Provider / Blank"

# Bucket for contacts that fell through both job_function_1 and the jobtitle
# keyword fallback below. Also not a dropdown option — surfaced only as an
# aggregate caveat percentage on the "All" view.
UNCLASSIFIED = "Other/unclassified"

# Fallback jobtitle keyword matching, used ONLY for contacts whose
# job_function_1 is blank or OTHER_PROVIDER_BLANK. Matched as case-insensitive
# WHOLE-WORD/PHRASE matches against the raw jobtitle string (persona_data.py
# wraps each keyword in \b...\b) — plain substring matching would false-
# positive short acronyms like "cto"/"coo" inside unrelated words (e.g. "cto"
# is a substring of "director"). Dict iteration order matters — the first
# persona with a matching keyword wins. Founder/CEO/President is deliberately
# LAST: its keywords ("president", "ceo") are broad enough to appear inside
# more specific titles like "Vice President Operations" or "AVP Finance",
# which should land in the functional bucket, not here — put more specific/
# senior keywords ahead of generic ones if a title could plausibly match more
# than one bucket.
JOBTITLE_PERSONA_KEYWORDS: dict[str, list[str]] = {
    "Finance": [
        "cfo",
        "chief financial",
        "controller",
        "finance",
        "financial",
        "accounting",
        "accounts payable",
        "accounts receivable",
        "revenue cycle",
        "billing",
        "bookkeeper",
    ],
    "Compliance / IT": [
        "cio",
        "chief information",
        "cto",
        "chief technology",
        "compliance",
        "information technology",
        "cybersecurity",
        "security",
        "software engineer",
        "devops",
        "developer",
        "it manager",
        "it director",
    ],
    "HR / Admin": [
        "hr",
        "human resources",
        "people ops",
        "people operations",
        "talent",
        "office manager",
        "administrative assistant",
        "executive assistant",
        "recruiter",
        "recruiting",
    ],
    "Operations": [
        "coo",
        "chief operating",
        "operations",
        "practice manager",
        "practice administrator",
        "administrator",
        "credentialing specialist",
    ],
    "Founder / CEO / President": [
        "ceo",
        "chief executive",
        "president",
        "founder",
        "owner",
        "chairman",
        "co-founder",
    ],
}
