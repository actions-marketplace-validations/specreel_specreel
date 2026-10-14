# Specreel Cloud

A hosted home for your galleries: publish to a stable per-project URL, control
public vs. private, and see view analytics — without wiring up CI. The OSS CLI
stays the engine; Cloud is where the galleries live.

> Status: v1. The core loop works end to end. Billing, SSO, and the production
> deploy are follow-ups.

## Two ways to onboard

**① From a URL (no tests yet).** In the dashboard, **Start from a URL** → paste your
app's URL (optionally a login cookie). The cloud crawls it (server-side, SSRF-guarded)
and suggests demo-worthy flows. On the **refine** screen you can:
- **Keep / drop** any suggested flow with a checkbox,
- **Add a flow in plain English** — "log in, then create a project named Acme and
  confirm it appears" → AI writes a runnable flow grounded in the crawled pages,
- **Refine the set in English** — "focus on checkout, drop the marketing pages" →
  AI reorders/renames/drops (never invents).

Then it creates the project and asks **how the flows should run**:
- **Run in Specreel** — edit scenarios here; hosted runs replay them (no GitHub).
- **Add to CI** — commit a scaffold + `specreel-monitor.yml` + two repo secrets
  (the engineering path).
- **I'll do it myself** — skip; publish from the CLI or come back later.

It's a smoke-level starting set you refine. The plain-English features need
`SPECREEL_CLOUD_AI_KEY` on the server (BYO-key); private/internal URLs are refused.

**② From an existing suite.** Create an API token and `publish --to cloud` (below).
Any project's **CI schedule** button regenerates the monitor workflow any time.

## Publish from the CLI
```bash
# 1. sign up and create an API token in the dashboard
# 2. push a gallery:
specreel publish site --to cloud --project my-app \
  --cloud-url https://app.specreel.dev --token scl_xxx
# -> https://app.specreel.dev/g/<org>/my-app/
```
`--cloud-url` and `--token` also read from `SPECREEL_CLOUD_URL` and
`SPECREEL_CLOUD_TOKEN`. Each publish creates a new build and updates the live URL.

## What you get
- **Stable share URLs** — `/(g)/<org>/<project>/` that stay put across builds.
- **Public or private** — private galleries require a logged-in member of your org.
- **View analytics** — per-project view counts (gallery + per-flow entry pages).
- **A dashboard** — projects, build history, visibility, and API tokens.
- **Monitoring + break alerts** — the cloud watches every publish and alerts when a
  flow breaks. See below.
- **Review & sign-off** — comments, build approval, and login-free review links.
- **Narrated captions, no key needed** — hosted runs narrate your steps into friendly
  prose automatically (the literal caption stays underneath as the source of truth).
  With the CLI you'd bring your own key via `--ai`; hosted, it's included.
- **Your branding** — set an accent color and logo per project (Project settings →
  Branding) and every hosted demo picks them up immediately, no republish.
- **A customer-facing showcase** — serve visitors the curated render while your team
  keeps the full health view. See below.

## Customer-facing showcase (Public gallery view)
One project, two audiences. Builds published with `showcase: true` in `specreel.yml`
carry a curated `showcase/` render — only flows marked `public: true` that are
passing, no failure states, provenance kept (see
[gallery & publishing](gallery-and-publish.md#customer-facing-showcase)). To make
that what the world sees:

1. Publish a build made with `showcase: true` (CLI or hosted runs — the runner
   honors your `specreel.yml`).
2. On the project page, set **Public gallery view → showcase** (admin-only).

From then on, non-members visiting `/g/<org>/<project>/` are **confined to the
curated render** — internal and failing flow files aren't merely unlinked, they
aren't fetchable at that URL at all. Org members always see the full gallery at the
same address, and review links (`/r/<token>`) stay full-view too: reviewers are the
audience failures *are* for. If the live build doesn't carry a showcase yet,
visitors get a friendly pending page instead of a leak.

The default (**full**) keeps today's behavior: the share URL serves the complete
gallery, failures included.

## Monitoring & break alerts (feature A)

Every `publish --to cloud` sends each flow's pass/fail. Build-over-build the cloud
tracks per-flow status: a flow that was green and is now failing **opens an
incident** (one per flow, no duplicates) and posts a Slack alert; when it passes
again the incident **auto-resolves** and posts a recovery message.

1. In the dashboard, open **Alerts** and paste a Slack
   [incoming webhook](https://api.slack.com/messaging/webhooks). Leave it blank to
   disable Slack and just keep the incident history.
2. Publish as usual. Incidents show on each project page (open/resolved, which
   build broke it, jump to the demo).

> 🔴 Flow *Checkout* broke in build #1482 (my-app). View the demo: …/checkout/demo.html

### Scheduled heartbeat (monitoring without new commits)

Publishes are the heartbeat, so run one on a schedule. A cron GitHub Action that
re-runs your suite against staging/prod and republishes gives the cloud a steady
pulse — if a flow breaks between commits, you still get the alert:

```yaml
# .github/workflows/specreel-monitor.yml
name: specreel-monitor
on:
  schedule: [{ cron: "0 */6 * * *" }]   # every 6 hours
  workflow_dispatch:
jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install playwright && playwright install --with-deps chromium
      - run: python your_flows.py            # re-run the suite -> test-results/*/trace.zip
        env: { BASE_URL: ${{ secrets.STAGING_URL }} }
      - run: specreel test-results -o site
      - run: |
          specreel publish site --to cloud --project my-app \
            --cloud-url ${{ secrets.SPECREEL_CLOUD_URL }} \
            --token ${{ secrets.SPECREEL_CLOUD_TOKEN }}
```

### Hosted runs (beta — no CI required)

Don't want to wire up CI? **Hosted runs** let the cloud run your flows for you. On a
project's **Hosted runs** page, set a target URL, paste/keep the flow script
(onboarding prefills it from the crawl), and either **Run now** or pick a schedule
(`hourly`/`6h`/`daily`). Each run replays the flows server-side, renders a gallery,
publishes it as the live build, and opens incidents + alerts on a break — exactly
like a publish, with zero GitHub setup.

- **Run now** works on any plan; **scheduling** needs Team or higher.
- Scheduling is driven by a cron hitting `POST /internal/run-due` (protected by
  `SPECREEL_CLOUD_INTERNAL_TOKEN`); self-hosters point any scheduler at it.
- This is the foundation (synchronous, single-process). A production browser farm
  (queued, isolated, resource-capped) is the next step — the lifecycle and the
  swappable executor (`runner.run_flows`) are already in place.

## Scenarios & variables (the authoring model)

A project's content is a set of **scenarios** — plain-English tests grouped in
folders — on `/app/projects/<id>/scenarios`:

- **Add a scenario in English** ("Log in with `{{EMAIL}}` / `{{PASSWORD}}`, then verify
  the dashboard shows 'Your Sites'") → we generate a runnable flow, grounded in the
  app's pages. Bulk-add one per line; edit re-generates; pause to mute a flaky one.
- **Every generated flow is checked in a real browser** right after it's saved (in the
  background — the scenario shows *checking in a browser…*, then **verified** or
  **first run failed** with the error). If the first run throws — or runs clean but
  the page it ends on shows it never reached the goal — the model rewrites the flow
  from what was actually on the page and the fixed version replaces it. The check signs
  in like a hosted run and fills `{{VARIABLES}}`, so it really does act on your app
  once — use a test account, as for hosted runs. Python and JS projects; operator
  switch `SPECREEL_CLOUD_VERIFY_FLOWS=0`.
- **Discover scenarios** re-runs the onboarding crawl against the project's target
  URL (signed in, if the project has stored sign-in) and offers only the flows your
  scenarios don't already cover — so coverage can grow as the app does, not just at
  onboarding.
- **Variables** are project-level (`{{NAME}}`), optionally **secret** (masked) —
  reusable test data and the clean way to handle logins. They're injected into every
  scenario at run time.
- Each hosted run assembles the enabled scenarios into one script (substituting the
  variables) and runs it. Each run gets a **detail page** — status, log, the captioned
  gallery, and an **"Analyze with AI"** root-cause for failures.
- **Run or schedule a single scenario.** Each scenario has a **Run** button (runs just
  that one, in isolation — it stores its own viewable gallery and updates only that
  scenario's status; the live whole-suite gallery is untouched) and an optional
  per-scenario **schedule** (hourly / 6h / daily; Team plan and up). Per-scenario
  schedules fire from the same cron tick as whole-suite runs.

Onboarding from a URL seeds these scenarios for you; from there you refine in English.

## Plans, team & billing

- **Plans** — `Free` (1 project, manual publish), `Team` ($39/mo, unlimited
  projects + scheduled monitoring + review), `Scale` ($149/mo, hosted runs
  included), `Enterprise` (custom). The Free **project limit is enforced**; publishing a
  second project returns `402` until you upgrade. (Pricing rationale lives in the
  pricing page.) Upgrades run through Stripe Checkout, which collects the
  billing address and an optional tax ID for invoices. "Invoices & billing" opens the
  Stripe portal for invoices and receipts, the card on file, and cancellation; a
  paying workspace that switches between Team and Scale confirms it there too (never
  a second subscription).
- **Team seats** — invite teammates from **Team** (owner/admin only). Members view
  private galleries, comment, and approve. **Reviewers via review links never need a
  seat** — that's the point of feature B. A user can belong to multiple workspaces
  and switch between them in the nav.
- **Email** — invites are emailed when SMTP is configured (`SPECREEL_SMTP_*`); in dev
  the invite link is shown in the UI instead.

## Review & sign-off (feature B)

Turn a gallery into a place work happens — including for people who'll never run
the CLI.

- **Comments** — leave notes on the live build from the project page.
- **Approve / sign-off** — mark a build approved (who + when shows in the history).
- **Review links** — create a tokenized `/(r)/<token>` URL from the project page.
  Anyone with the link can view the gallery, comment, and approve **without an
  account** — even if the project is private. Revoke a link any time.

## Custom domains

Serve your public demos on your own hostname instead of `/g/<workspace>/…`:

- **Workspace subdomain** (Team+): `<workspace>.specreel.dev` works as soon as
  you're on the plan — nothing to configure.
- **Bring your own domain** (Scale+): Settings → *Custom domain*, enter
  `demos.acme.com`, then add the DNS record it shows you (a CNAME at your DNS
  host). The TLS certificate issues automatically once the record resolves —
  hit **Re-check** to watch it go active.

On your domain, projects live at `https://demos.acme.com/<project>/` (one public
project? the root redirects straight to it; several? the root lists them).
Vanity hosts serve **public content only** — private projects, the dashboard,
and login stay on the main app URL. Downgrading a plan pauses the domain;
visitors are redirected to the canonical `/g/…` URL, so links never die.

### Operator setup (hosted or self-host, one-time)

The app resolves vanity hosts from `X-Forwarded-Host`; any proxy that owns DNS
and TLS can sit in front. The reference setup is Cloudflare (any plan — SaaS
custom hostnames include 100 free, then ~$0.10/hostname/mo):

1. **DNS** on the zone: keep the app host (e.g. `app.specreel.dev`) DNS-only →
   Cloud Run; add a proxied wildcard `*.specreel.dev` (`AAAA 100::` placeholder
   is fine — the Worker answers) and a proxied `edge.specreel.dev` the same way.
2. **SSL/TLS → Custom Hostnames**: enable Cloudflare for SaaS, set the
   **fallback origin** to `edge.specreel.dev`.
3. **Worker**: `cd cloud/edge && npx wrangler deploy`, set `ORIGIN_URL` to the
   Cloud Run service URL in `wrangler.jsonc`, and
   `npx wrangler secret put EDGE_TOKEN`.
4. **App env** (`cloud/env.deploy.yaml`): `SPECREEL_CLOUD_DOMAIN_SUFFIX`,
   `SPECREEL_CLOUD_DOMAIN_CNAME`, `SPECREEL_CLOUD_EDGE_TOKEN` (same value as
   the Worker secret), and — for automatic BYO provisioning —
   `SPECREEL_CLOUD_CF_TOKEN` (Zone → SSL and Certificates → Edit) +
   `SPECREEL_CLOUD_CF_ZONE`.

Without the Cloudflare token the feature runs in **manual mode**: the app stores
and serves the domain immediately, and your own proxy is responsible for
pointing it at the app with the edge headers.

## Run your own
Specreel Cloud runs from the repo (it's intentionally not shipped in the public
`specreel` package — open-core). Flask, with a dual-backend data layer:
```bash
# from the specreel repo:
pip install -r cloud/requirements.txt
SPECREEL_CLOUD_SECRET=dev python -m cloud.app    # SQLite, http://localhost:8800
```
**Database:** SQLite by default (great for local/self-host). For production, point
it at **Postgres / Supabase** — SQLite is then bypassed:
```bash
pip install "specreel[cloud]" "psycopg[binary]"
export SPECREEL_CLOUD_DB_URL="postgresql://…@…pooler.supabase.com:6543/postgres?sslmode=require"
python -m cloud.app
```
Deploy with the included Dockerfile (Cloud Run / Fly / Render). Full guide + the
object-storage / OAuth / billing seams are internal to the hosted service.
