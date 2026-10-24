# Deploying the dashboard

**Current deployment (2026-10-24):** Worker `signal2care-api` and D1 database `signal2care` are live at
`https://signal2care-api.sentaigo.workers.dev` in the Cloudflare account the owner approved. Preview URLs are off. The
dashboard is not published yet; it needs the GitHub Pages steps in section 2.

Two pieces: a Cloudflare Worker with a D1 database (free tier), and the static dashboard on GitHub Pages.
Nothing below needs a card. Check which Cloudflare account you are logged into before you create anything.

## 1. Cloudflare Worker and D1

```bash
cd cloudflare
npm install
npx wrangler login            # opens a browser; approve the account you want to use
npx wrangler whoami           # confirm the account name
npx wrangler d1 create signal2care
```

Copy the `database_id` it prints into `cloudflare/wrangler.toml`. Then:

```bash
npm run db:remote             # applies schema.sql to the remote database
openssl rand -hex 24 | npx wrangler secret put INGEST_TOKEN     # prints nothing; save the value below
openssl rand -hex 24 | npx wrangler secret put DASH_TOKEN
npm run deploy                # prints https://signal2care-api.<your-subdomain>.workers.dev
```

`wrangler secret put` reads the value from stdin, so generate each token once, keep a copy, and put the same values in `.env`:

```
CLOUD_URL=https://signal2care-api.<your-subdomain>.workers.dev
INGEST_TOKEN=<same value as the Worker secret>
DASH_TOKEN=<same value as the Worker secret>
DASHBOARD_URL=https://laukik07.github.io/Service2Care
```

`ALLOWED_ORIGINS` in `wrangler.toml` must include the origin that serves the dashboard.

## 2. GitHub Pages

The dashboard is the `dashboard/` folder. Publish it from a `gh-pages` branch:

```bash
git subtree split --prefix dashboard -b gh-pages
git push origin gh-pages
```

Then in the repository: Settings → Pages → Build and deployment → Deploy from a branch → `gh-pages` / root. The site appears at `https://<owner>.github.io/<repo>/`. Anyone without a key sees the recorded run.

## 3. Restart the brain and open the dashboard

Restart `scripts/serve.sh` so it reads the new `.env`, then text `demo link` to the Signal2Care number and open the link it sends.

## Rotating keys
Change `DASH_TOKEN` (Worker secret and `.env`) to invalidate every previously shared link. Change `INGEST_TOKEN` the same way to cut off the brain.
