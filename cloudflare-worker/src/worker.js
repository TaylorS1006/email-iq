/**
 * Public backend for Reputation's Co-write feature.
 *
 * Proxies Claude + HubSpot calls for the published dashboard
 * (https://taylors1006.github.io/Reputation/), since GitHub Pages can't
 * run a server and the API keys can't live in a publicly-served page.
 * Every route requires the X-Cowrite-Key header to match the
 * COWRITE_PASSPHRASE secret — that's the only thing standing between this
 * endpoint and anyone on the internet, since GitHub Pages itself has no
 * login.
 *
 * Mirrors (in JS, since Workers can't run the project's Python) the same
 * logic as, respectively: hubspot_client.fetch_lists, hubspot_client.
 * fetch_emails + hubspot_client._parse_content_type, and analyzer.
 * _build_prompt / _analyze_content_type. Keep these in sync if those
 * change — see cloudflare-worker/README.md.
 */

const HUBSPOT_BASE = "https://api.hubapi.com";
const ANTHROPIC_BASE = "https://api.anthropic.com";
const COWRITE_MODEL = "claude-sonnet-4-6";
const MIN_SAMPLE_SIZE = 5;

const CONTENT_TYPES = new Set([
  "webinar", "in-person event", "micro event", "product release", "newsletter",
  "content", "blog", "case study", "announcement", "survey", "onboarding", "virtual event",
]);
const TYPE_ALIASES = {
  "in person event": "in-person event",
  "in-person": "in-person event",
  "virtual": "virtual event",
  "micro-event": "micro event",
  "product launch": "product release",
  "release": "product release",
  "case-study": "case study",
  "awareness": "announcement",
};

const INSIGHT_SCHEMA = {
  type: "object",
  properties: {
    headline: { type: "string" },
    confidence: { type: "string", enum: ["strong", "moderate", "none"] },
    key_stat: { type: "string" },
    reasoning: { type: "string" },
    action: { type: "string" },
  },
  required: ["headline", "confidence", "key_stat", "reasoning", "action"],
};

const PLAYBOOK_SCHEMA = {
  type: "object",
  properties: {
    executive_summary: { type: "string" },
    insights: { type: "array", items: INSIGHT_SCHEMA },
    top_performing_examples: { type: "array", items: { type: "string" } },
    data_quality_note: { type: "string" },
  },
  required: ["executive_summary", "insights", "top_performing_examples", "data_quality_note"],
};

function parseContentType(name) {
  for (const part of (name || "").split("|")) {
    const raw = part.trim().toLowerCase();
    if (CONTENT_TYPES.has(raw)) return raw;
    if (TYPE_ALIASES[raw]) return TYPE_ALIASES[raw];
  }
  return null;
}

function jsonResponse(obj, cors, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { ...cors, "content-type": "application/json" },
  });
}

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Cowrite-Key",
  };
}

async function fetchLists(hsToken) {
  const lists = [];
  let offset = 0;
  while (true) {
    const resp = await fetch(`${HUBSPOT_BASE}/crm/v3/lists/search`, {
      method: "POST",
      headers: { Authorization: `Bearer ${hsToken}`, "content-type": "application/json" },
      body: JSON.stringify({ query: "", offset, count: 250 }),
    });
    if (resp.status === 403) {
      throw new Error(
        "HubSpot returned 403 fetching lists — the private app token likely needs " +
        "the 'crm.lists.read' scope added, then must be regenerated."
      );
    }
    if (!resp.ok) throw new Error(`HubSpot lists fetch failed: ${resp.status} ${await resp.text()}`);
    const body = await resp.json();
    const page = body.lists || [];
    for (const lst of page) lists.push({ id: String(lst.listId), name: lst.name || "" });
    if (!page.length || !body.hasMore) break;
    offset += page.length;
  }
  lists.sort((a, b) => a.name.localeCompare(b.name));
  return lists;
}

async function fetchEmailsForType(contentType, hsToken, days) {
  const cutoff = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  const sentStates = new Set(["PUBLISHED", "AUTOMATED"]);
  const emails = [];
  let after = null;

  while (true) {
    const params = new URLSearchParams({ limit: "100", sort: "-publishDate" });
    if (after) params.set("after", after);
    const resp = await fetch(`${HUBSPOT_BASE}/marketing/v3/emails?${params}`, {
      headers: { Authorization: `Bearer ${hsToken}` },
    });
    if (!resp.ok) throw new Error(`HubSpot emails fetch failed: ${resp.status} ${await resp.text()}`);
    const body = await resp.json();

    let stop = false;
    for (const email of body.results || []) {
      if (!sentStates.has(email.state)) continue;
      const sendDateRaw = email.publishDate || email.sendDate;
      const sendDate = sendDateRaw ? new Date(sendDateRaw) : null;
      if (sendDate && sendDate < cutoff) { stop = true; break; }
      if (parseContentType(email.name) !== contentType) continue;

      const counters = {};
      for (const cid of email.allEmailCampaignIds || []) {
        const statsResp = await fetch(`${HUBSPOT_BASE}/email/public/v1/campaigns/${cid}`, {
          headers: { Authorization: `Bearer ${hsToken}` },
        });
        if (!statsResp.ok) continue;
        const stats = await statsResp.json();
        for (const [k, v] of Object.entries(stats.counters || {})) {
          if (typeof v === "number") counters[k] = (counters[k] || 0) + v;
        }
      }

      const sent = counters.sent || 0;
      const delivered = counters.delivered || sent;
      emails.push({
        subject: email.subject || "",
        sent,
        open_rate: delivered ? (counters.open || 0) / delivered : 0,
        click_rate: delivered ? (counters.click || 0) / delivered : 0,
        send_date: sendDateRaw || null,
      });
    }
    if (stop) break;
    after = body.paging && body.paging.next && body.paging.next.after;
    if (!after) break;
  }
  return emails;
}

function buildPrompt(contentType, emails) {
  const sorted = [...emails].sort((a, b) => b.open_rate - a.open_rate);
  const lines = [
    `You are analyzing ${emails.length} '${contentType}' marketing emails sent by a B2B SaaS company.`,
    "",
    "Here is the email data (sorted by open rate, highest first):",
    "",
  ];
  for (const e of sorted) {
    const date = e.send_date ? new Date(e.send_date).toISOString().slice(0, 10) : "unknown";
    lines.push(
      `- Subject: ${JSON.stringify(e.subject)} | Sent: ${e.sent.toLocaleString()} ` +
      `| Open rate: ${(e.open_rate * 100).toFixed(1)}% | Click rate: ${(e.click_rate * 100).toFixed(1)}% | Date: ${date}`
    );
  }
  lines.push(
    "",
    "Based ONLY on the patterns visible in this data, produce a playbook with these fields:",
    "  executive_summary: 2-4 sentences synthesizing what the top-performing emails have in " +
      "common and what to do differently next time. This is a takeaway, not a restatement of " +
      "the raw numbers — someone who already saw the table should learn something from this.",
    "  insights: A list of individual findings, each covering ONE specific pattern (e.g. a " +
      "subject line wording pattern, subject length, a CTA/content theme, a send-day or " +
      "send-time pattern). For each insight, set:",
    "    - headline: one short, specific sentence stating the finding.",
    "    - confidence: 'strong' if the pattern is clear and consistent across multiple emails, " +
      "'moderate' if directional but caveated (small sample, one outlier, conflicting signal), " +
      "or 'none' if you checked for a pattern along this dimension and the data does NOT " +
      "support one — say so explicitly rather than omitting the insight.",
    "    - key_stat: the single most relevant number backing this insight (a rate, a count, " +
      "or a percentage-point gap).",
    "    - reasoning: 1-3 sentences of supporting detail and caveats.",
    "    - action: for 'strong' insights, one concrete 'try this next' recommendation. For " +
      "'moderate' or 'none' insights, use an empty string.",
    "  You MUST include at least one insight for subject line wording, one for subject length, " +
      "and one for send timing — use 'none' confidence for any of these where the data doesn't " +
      "support a conclusion, rather than skipping it. Add further insights for CTA/content " +
      "themes or any other pattern you find.",
    "  top_performing_examples: List 2-3 actual subject lines from the top-performing emails.",
    "  data_quality_note: One consolidated note covering sample size, audience-size skew, and " +
      "any subject lines repeated across multiple sends/segments — whatever caveats apply here.",
    "",
    "IMPORTANT: Only report patterns actually supported by this data. Do not add generic " +
      "email best-practices that are not evidenced here."
  );
  return lines.join("\n");
}

// Uses forced tool-use for structured output (stable/documented across API
// versions) rather than trying to replicate whatever the Python SDK's
// `output_config` param sends over the wire — analyzer.py itself is
// untouched and keeps using the SDK.
async function analyzeContentType(contentType, emails, anthropicKey) {
  const prompt = buildPrompt(contentType, emails);
  const resp = await fetch(`${ANTHROPIC_BASE}/v1/messages`, {
    method: "POST",
    headers: {
      "x-api-key": anthropicKey,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: COWRITE_MODEL,
      max_tokens: 4096,
      messages: [{ role: "user", content: prompt }],
      tools: [{ name: "submit_playbook", description: "Submit the analyzed playbook.", input_schema: PLAYBOOK_SCHEMA }],
      tool_choice: { type: "tool", name: "submit_playbook" },
    }),
  });
  if (!resp.ok) throw new Error(`Anthropic API error: ${resp.status} ${await resp.text()}`);
  const data = await resp.json();
  const toolUse = (data.content || []).find((b) => b.type === "tool_use");
  if (!toolUse) throw new Error("Claude did not return structured output");
  return toolUse.input;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const cors = corsHeaders(request.headers.get("Origin"));

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    const key = request.headers.get("X-Cowrite-Key") || "";
    if (key !== env.COWRITE_PASSPHRASE) {
      return jsonResponse({ error: "Unauthorized" }, cors, 401);
    }

    try {
      if (url.pathname === "/api/audience-lists" && request.method === "GET") {
        return jsonResponse(await fetchLists(env.HUBSPOT_ACCESS_TOKEN), cors);
      }

      if (url.pathname === "/api/refresh-playbook" && request.method === "GET") {
        const contentType = (url.searchParams.get("content_type") || "").trim();
        if (!contentType) return jsonResponse({ error: "content_type is required" }, cors, 400);

        const emails = await fetchEmailsForType(contentType, env.HUBSPOT_ACCESS_TOKEN, 365);
        if (emails.length < MIN_SAMPLE_SIZE) {
          return jsonResponse(
            { status: "insufficient_data", sample_count: emails.length, minimum_required: MIN_SAMPLE_SIZE },
            cors
          );
        }
        const result = await analyzeContentType(contentType, emails, env.ANTHROPIC_API_KEY);
        result.sample_count = emails.length;
        return jsonResponse(result, cors);
      }

      if (url.pathname === "/api/chat" && request.method === "POST") {
        const body = await request.json();
        if (!body.messages || !body.messages.length) {
          return jsonResponse({ error: "messages is required" }, cors, 400);
        }
        const resp = await fetch(`${ANTHROPIC_BASE}/v1/messages`, {
          method: "POST",
          headers: {
            "x-api-key": env.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
          },
          body: JSON.stringify({
            model: COWRITE_MODEL,
            max_tokens: 1500,
            system: body.system || "",
            messages: body.messages,
          }),
        });
        if (!resp.ok) return jsonResponse({ error: `Anthropic API error: ${resp.status} ${await resp.text()}` }, cors, 500);
        const data = await resp.json();
        const textBlock = (data.content || []).find((b) => b.type === "text");
        return jsonResponse({ reply: textBlock ? textBlock.text : "" }, cors);
      }

      return jsonResponse({ error: "Not found" }, cors, 404);
    } catch (err) {
      return jsonResponse({ error: String((err && err.message) || err) }, cors, 500);
    }
  },
};
