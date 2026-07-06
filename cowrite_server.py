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

import base64
import io
import os
from urllib.parse import urlparse

import anthropic
import requests
from docx import Document
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from lxml import html as lxml_html

from analyzer import MIN_SAMPLE_SIZE, _analyze_content_type
from hubspot_client import fetch_emails, fetch_lists
from report import generate_report

load_dotenv()

COWRITE_MODEL = "claude-sonnet-4-6"
DEV_HTML_FILENAME = "_cowrite_dev.html"
DEV_HTML_PATH = os.path.join(os.path.dirname(__file__), DEV_HTML_FILENAME)

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
LINK_FETCH_TIMEOUT = 8
LINK_FETCH_MAX_BYTES = 2 * 1024 * 1024
LINK_TEXT_MAX_CHARS = 6000

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


def _extract_page_text(raw_html: str) -> tuple[str, str]:
    """Returns (title, body_text) from a page's HTML, dropping script/style content."""
    tree = lxml_html.fromstring(raw_html)
    title_els = tree.xpath("//title")
    title = title_els[0].text_content().strip() if title_els else ""
    for el in tree.xpath("//script | //style | //noscript"):
        el.drop_tree()
    body = tree.xpath("//body")
    text = body[0].text_content() if body else tree.text_content()
    text = " ".join(text.split())
    return title[:200], text[:LINK_TEXT_MAX_CHARS]


@app.post("/api/fetch-link")
def fetch_link():
    body = request.get_json(force=True)
    url = (body.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"error": "Only http/https links are supported"}), 400

    try:
        resp = requests.get(
            url,
            timeout=LINK_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CowriteBot/1.0)"},
            stream=True,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type:
            return jsonify({"error": f"Unsupported content type: {content_type or 'unknown'}"}), 415

        raw = resp.raw.read(LINK_FETCH_MAX_BYTES + 1, decode_content=True)
        if len(raw) > LINK_FETCH_MAX_BYTES:
            raw = raw[:LINK_FETCH_MAX_BYTES]
        title, text = _extract_page_text(raw)
        return jsonify({
            "url": url,
            "title": title,
            "domain": parsed.netloc.removeprefix("www."),
            "text": text,
        })
    except requests.Timeout:
        return jsonify({"error": "Request timed out"}), 504
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        return jsonify({"error": f"Page returned {status} (may require login or be blocked)"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/extract-file")
def extract_file():
    body = request.get_json(force=True)
    media_type = body.get("media_type") or ""
    data = body.get("data") or ""
    if media_type != DOCX_MEDIA_TYPE:
        return jsonify({"error": f"Unsupported media type for extraction: {media_type or 'unknown'}"}), 415

    try:
        doc = Document(io.BytesIO(base64.b64decode(data)))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return jsonify({"text": text})
    except Exception as exc:
        return jsonify({"error": f"Could not read .docx file: {exc}"}), 400


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
