# Quickstart

Two paths — pick the one that matches you:

- **Path A — "I have an app URL"** (no tests, no code): everything happens in the
  cloud dashboard.
- **Path B — "I have a Playwright suite"**: everything happens in your terminal/CI.

---

## Path A — from a URL to a monitored demo (dashboard, no code)

**1. Sign up** at your Specreel Cloud (`/signup`). Verify your email when the link
arrives (a banner reminds you).

**2. Click "Start from a URL"** on the dashboard. Paste your app's URL — a staging
site is ideal. If the pages you care about are behind a login, paste a session
cookie too (copy it from your browser's devtools → Application → Cookies).

**3. Refine the suggested flows.** Specreel crawls the app and proposes flows
(forms, search, key pages). On the review screen you can:
- **untick** any flow you don't want,
- **add a flow in plain English** — e.g. *"log in, then open the dashboard and
  confirm it loads"*,
- **refine the whole set in English** — e.g. *"focus on checkout, drop the
  marketing pages"*.

**4. Create the project.** You pick how the flows stay proven:
- **Run in Specreel** (recommended if you don't have a suite yet) — we keep the
  flows here and run the browsers.
- **Add to CI** — for your engineering team: commit a script + GitHub Action.
- **I'll do it myself** — skip, publish from the CLI later.

**5. Run it.** On the project's **Hosted runs** page (or the Scenarios card), press
**Run now**. Specreel replays your flows in a real browser, renders the demos, and
publishes them as your project's live gallery.

**6. Look at what you got:**
- **Share URL** — `…/g/<you>/<project>/`: the gallery of watchable demos. Send it.
- **Scenarios** — each flow with a status chip (*stable / broken / healing*). Edit
  one in English and it regenerates; add `{{EMAIL}}`-style **Variables** for logins
  and test data (mark them secret to mask them).
- **Monitoring** — pick a schedule (hourly/6h/daily) and add a Slack webhook or
  alert email under **Alerts**. From then on: a flow that breaks opens an incident
  and pings you, with a demo of the failure attached. Use **Send a test alert** to
  check the wiring.
- **Review links** — a tokenized URL anyone can open (no account) to watch,
  comment, and approve.

> A red flow right after onboarding is normal — it usually means the flow needs a
> tweak (a login, a click to reveal a hidden element). Open the run, read the log,
> use **Analyze with AI**, or edit the scenario's English description.

---

## Path B — "I have a Playwright suite" (CLI)

**1. Turn tracing on** so Playwright records screenshots (that's what makes demos
watchable):

**JS/TS** — in `playwright.config.ts`:
```ts
use: { trace: 'on' }
```
**Python (pytest-playwright):** run with `--tracing on`, or in code:
```python
context.tracing.start(screenshots=True, snapshots=True)
# ... your test ...
context.tracing.stop(path="trace.zip")
```

**2. Preflight** (optional but saves head-scratching):
```bash
specreel doctor test-results
```

**3. Render a gallery** — one demo per `trace.zip`, plus an index:
```bash
specreel test-results/ -o site/ --bundle
# -> site/index.html        the gallery (one card per flow)
# -> site/<flow>/demo.html  a self-contained player per flow
# -> site/gallery.html      ONE portable file — email it, Slack it, open from file://
```
(Single trace instead: `specreel trace.zip -o out/ --title "Sign up"`.)

**4. Share it** — pick one:
```bash
specreel publish site/ --to cloud \
  --cloud-url https://app.specreel.dev --token scl_xxx --project my-app
# -> a stable URL + monitoring/alerts on every publish  (token: dashboard → API tokens)

specreel publish site/ --to ghpages    # or GitHub Pages
```
…or just send `site/gallery.html`.

**5. Automate it** so demos regenerate on every green build — the one-step
[GitHub Action](github-action.md):
```yaml
- uses: specreel/specreel@v1
  with: { traces: test-results, publish: cloud,
          cloud-url: ${{ secrets.SPECREEL_CLOUD_URL }},
          token: ${{ secrets.SPECREEL_CLOUD_TOKEN }}, project: my-app }
```

---

## Level up (both paths)

- **Friendlier captions**: `--ai` rewrites captions into sales-engineer narration
  ([AI narration](ai-narration.md), BYO `ANTHROPIC_API_KEY`).
- **Studio voiceover**: `--voice` pre-renders neural TTS audio per step
  (BYO `OPENAI_API_KEY`).
- **Titles, themes, visibility**: [`specreel.yml`](configuration.md).
- **Everything the cloud does** (incidents, review links, analytics, plans):
  [Using Specreel Cloud](cloud.md).
