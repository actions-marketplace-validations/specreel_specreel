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

Then it scaffolds the Playwright script and creates a monitored project, with a
**setup screen** (scaffold + a scheduled GitHub Action + the two repo secrets), or run
it via **hosted runs**. It's a smoke-level starting set you refine. The plain-English
features need `SPECREEL_CLOUD_AI_KEY` on the server (BYO-key); private/internal URLs
are refused.

**② From an existing suite.** Create an API token and `publish --to cloud` (below).
Any project's **Set up schedule** button regenerates the monitor workflow any time.

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
      - run: python specreel.py test-results -o site
      - run: |
          python specreel.py publish site --to cloud --project my-app \
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
  projects + scheduled monitoring + review), `Scale` ($149/mo + metered hosted
  runs), `Enterprise` (custom). The Free **project limit is enforced**; publishing a
  second project returns `402` until you upgrade. (Pricing rationale lives in the
  pricing page.) Upgrades run through Stripe Checkout; "Manage billing"
  opens the Stripe portal for cards, invoices, and cancellation.
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
