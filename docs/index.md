# Specreel docs

Specreel turns recorded browser flows into **watchable, shareable demos that are
also your tests** — and keeps proving they still work. When a flow breaks, you get
an alert and a demo of the failure; when it passes, your demo is provably current.

> A demo and an end-to-end test are the same artifact: a recorded browser flow.
> Replay it = a demo. Share it = a link. Assert on it = a test.

## Pick your starting point

**① "I have an app URL and no tests."** → Use the **cloud dashboard**. Sign in →
**Start from a URL** → Specreel crawls your app and suggests flows → keep/drop them,
add more in plain English → it creates a monitored project you can **Run now** or
put on a schedule. No code required. *This is the most common path — follow
[Quickstart, Path A](quickstart.md).*

**② "I already have a Playwright suite."** → Use the **CLI**. Your tests already
produce `trace.zip` files; point `specreel` at them and you get a demo gallery, then
publish it (cloud / GitHub Pages / a single portable file).
*Follow [Quickstart, Path B](quickstart.md#path-b--i-have-a-playwright-suite-cli).*

**③ "I want to run the whole thing myself."** → The cloud is self-hostable
(Flask + SQLite/Postgres, one Docker image). See **Run your own** in
[Using Specreel Cloud](cloud.md#run-your-own).

## The mental model (60 seconds)

```
 a recorded browser flow (Playwright trace)
        │
        ├── replayed  →  a watchable demo (captions, thumbnails, voiceover)
        ├── shared    →  a gallery at a stable URL (public or private)
        └── asserted  →  a test: green = "the demo is still true"
                              red = an incident + an alert + a demo of the failure
```

Three moving parts:

| Part | What it is | Where |
|---|---|---|
| **The CLI** (`specreel.py`) | Renders traces → demos; discovers flows; publishes. Single file, stdlib-only, open source. | your terminal / CI |
| **The cloud** | Hosts galleries, runs your flows on a schedule, opens incidents + alerts, review links, analytics. | the dashboard |
| **Scenarios** | Your flows as plain-English descriptions (with `{{VARIABLES}}` for logins/test data), grouped in folders. The cloud turns them into runnable flows. | Project → Scenarios |

## The two loops that matter

- **The freshness loop** — every run/publish regenerates the demos, so a demo can
  never be older than the last green run.
- **The monitoring loop** — a flow that *was* green and is now failing opens an
  **incident** and fires a **Slack/email alert** with a link to the demo of the
  failure. Recovery auto-resolves it.

## All pages

- [Quickstart](quickstart.md) — both paths, ~10 minutes each.
- [Using Specreel Cloud](cloud.md) — dashboard walkthrough: onboarding, scenarios &
  variables, hosted runs, monitoring & alerts, review links, plans, self-hosting.
- [Flow discovery](recommend.md) — how `recommend`/Start-from-a-URL finds flows,
  logged-in crawls, what the scaffold does and doesn't do.
- [Configuration](configuration.md) — `specreel.yml`: titles, themes, AI, voiceover.
- [Galleries & publishing](gallery-and-publish.md) — the player, the single-file
  bundle, publish targets, badges, embeds.
- [CLI reference](cli-reference.md) — every command and flag.
- [AI narration](ai-narration.md) — opt-in, BYO-key friendly captions + studio voiceover.
- [GitHub Action](github-action.md) — demos + monitoring from your CI.
- [Agents & MCP](agents-and-mcp.md) — drive Specreel from Claude Code / any MCP client.

## Principles (why it behaves the way it does)

- **Deterministic by default** — captions come from the trace, no AI, no network.
- **AI is always opt-in + bring-your-own-key** (narration, plain-English flows, analysis).
- **Your tests are never modified** — any `trace.zip` from JS/TS/Python/Java/.NET works.
- **Secrets are masked** — values typed into password/OTP/CVV-like fields render as `••••••••`.
