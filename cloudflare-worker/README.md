# Co-write's public backend

This is the live backend behind Co-write on the published dashboard
(`https://taylors1006.github.io/Reputation/`). GitHub Pages can only serve
static files, so the chat calls to Claude and the HubSpot Lists lookup run
here instead, on Cloudflare's free tier — the only thing gating this
endpoint from the rest of the internet is a shared passphrase (see below),
since the public dashboard itself has no login.

Local development (`cowrite_server.py`) doesn't use this at all — it's
only for the published site.

## One-time setup

1. Install [Node.js](https://nodejs.org) if you don't have it, then from
   this directory:
   ```
   cd cloudflare-worker
   npm install
   npx wrangler login
   ```
   That opens a browser to log into (or create) a free Cloudflare account.

2. Set the three secrets (never go in `wrangler.toml` or git):
   ```
   npx wrangler secret put ANTHROPIC_API_KEY
   npx wrangler secret put HUBSPOT_ACCESS_TOKEN
   npx wrangler secret put COWRITE_PASSPHRASE
   ```
   Use the same keys as your `.env`. `COWRITE_PASSPHRASE` is whatever you
   want people to type to unlock Co-write on the public site — pick
   something you're comfortable sharing with whoever should have access.

3. Deploy:
   ```
   npx wrangler deploy
   ```
   This prints a URL like `https://reputation-cowrite.<your-subdomain>.workers.dev`.

## Wiring it into the dashboard

1. Add that URL to `Rep/.env`:
   ```
   COWRITE_WORKER_URL=https://reputation-cowrite.<your-subdomain>.workers.dev
   ```
   (see `.env.example`). With this set, the next `python report.py` run
   bakes Co-write into `index.html`, pointed at the Worker.

2. Add the same value as a **repository variable** (not secret — it's just
   a URL, nothing sensitive) so the daily GitHub Actions job includes it
   too: repo → Settings → Secrets and variables → Actions → Variables tab
   → New repository variable → name `COWRITE_WORKER_URL`.

3. Push/run the report once. On the live site, clicking **Co-write** for
   the first time will prompt for the passphrase from step 2 above and
   remember it in the browser's local storage after that.

## If something breaks

- **HubSpot 403 on the audience list dropdown**: the private app token
  needs the `crm.lists.read` scope — add it under the app's Scopes tab in
  HubSpot, then regenerate the token and re-run `wrangler secret put
  HUBSPOT_ACCESS_TOKEN`.
- **"Refresh data" is slow or fails for a high-volume content type**:
  it fetches per-campaign stats from HubSpot one request at a time
  (mirroring `hubspot_client.fetch_emails`), and Cloudflare's free plan
  caps a single Worker invocation at 50 outbound subrequests. If a
  content type has more campaigns than that, refresh will error out —
  either upgrade the Workers plan (1000 subrequests) or treat this as a
  known limit for now.
- **Changed the prompt/schema in `analyzer.py`**: `worker.js`'s
  `buildPrompt`/`PLAYBOOK_SCHEMA`/`parseContentType` are hand-ported
  copies (Workers can't run the project's Python), not shared code —
  update them here too, or refresh-playbook on the public site will drift
  from what Analyzer actually shows.
