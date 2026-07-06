"""
Local dev server for Reputation's Co-write feature.

Co-write is a multi-turn chat tool grounded in Analyzer playbook data. It
needs a live backend on every message (Claude API) and on load (HubSpot
Lists API), which the rest of this project doesn't have — report.py only
ever runs as a one-shot batch job that bakes a static index.html for
GitHub Pages. Rather than ship API keys to the browser on a public site,
Co-write only exists here: run `python cowrite_server.py`, it renders the
dashboard once (with Co-write enabled) to a local, gitignored file, and
serves it on localhost while proxying Claude/HubSpot calls server-side.

Not part of the daily GitHub Actions job — report.py's default
generate_report() call (enable_cowrite=False) never includes Co-write, so
the published dashboard is unaffected.
"""

import os

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file

from analyzer import MIN_SAMPLE_SIZE, _analyze_content_type
from hubspot_client import fetch_emails, fetch_lists
from report import generate_report

load_dotenv()

COWRITE_MODEL = "claude-sonnet-4-6"
DEV_HTML_FILENAME = "_cowrite_dev.html"
DEV_HTML_PATH = os.path.join(os.path.dirname(__file__), DEV_HTML_FILENAME)

app = Flask(__name__)


@app.get("/")
def index():
    return send_file(DEV_HTML_PATH)


@app.get("/api/audience-lists")
def audience_lists():
    try:
        return jsonify(fetch_lists())
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/refresh-playbook")
def refresh_playbook():
    content_type = request.args.get("content_type", "").strip()
    if not content_type:
        return jsonify({"error": "content_type is required"}), 400

    try:
        emails = fetch_emails(days=365, content_type=content_type)
        if len(emails) < MIN_SAMPLE_SIZE:
            return jsonify({
                "status": "insufficient_data",
                "sample_count": len(emails),
                "minimum_required": MIN_SAMPLE_SIZE,
            })
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        result = _analyze_content_type(client, content_type, emails)
        result["sample_count"] = len(emails)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/chat")
def chat():
    body = request.get_json(force=True)
    system = body.get("system", "")
    messages = body.get("messages", [])
    if not messages:
        return jsonify({"error": "messages is required"}), 400

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=COWRITE_MODEL,
            max_tokens=1500,
            system=system,
            messages=messages,
        )
        text = next(b.text for b in response.content if b.type == "text")
        return jsonify({"reply": text})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    print(
        "Rendering the dashboard with Co-write enabled — this hits live "
        "HubSpot/Salesforce/Anthropic APIs just like report.py, so it can "
        "take a few minutes...\n"
    )
    generate_report(push=False, enable_cowrite=True, output_filename=DEV_HTML_FILENAME)
    print(f"\nServing at http://127.0.0.1:5055 (reading {DEV_HTML_FILENAME}, not index.html)")
    app.run(port=5055, debug=False)
