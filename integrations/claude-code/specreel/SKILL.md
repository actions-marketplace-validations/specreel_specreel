---
name: specreel
description: >
  Turn a web app or existing Playwright traces into a watchable, shareable demo
  gallery with Specreel. Use when the user wants product demos, wants to "show"
  their app, wants to turn their tests into demos, or says things like "make a
  demo of my app", "record a walkthrough", or "generate demos from my tests".
---

# Specreel — make demos from a web app or its tests

Specreel turns Playwright `trace.zip` files into a watchable demo gallery. A demo
and an end-to-end test are the same artifact, so the demos stay current with the
app. Your job is to run the right path for the user's situation.

## Setup (once)
```bash
pip install specreel        # add 'playwright' if you'll capture flows:
pip install playwright && playwright install chromium
```
If a Specreel MCP server is connected, prefer its tools (`recommend`, `render`,
`publish`, `summary`); otherwise use the `specreel` CLI below.

## Pick the path

### A. The user has a running app but no tests
1. Ask for (or detect) the app URL, e.g. `http://localhost:3000`.
2. `specreel recommend <url>` — this writes `specreel_flows.py` with one function
   per suggested flow (forms, search, navigation).
3. **Open `specreel_flows.py` and finish each flow's `TODO`s** — add the assertion
   or submit click that defines "this flow worked." Use safe sample data; never
   run mutating flows against production.
4. `python specreel_flows.py` — produces `test-results/<flow>/trace.zip`.
5. `specreel test-results -o site --bundle` — builds the gallery.

### B. The user already has Playwright tests
1. Make sure tracing is on: JS `use: { trace: 'on' }`, or pytest `--tracing on`.
2. Run their tests so `test-results/**/trace.zip` exists.
3. `specreel test-results -o site --bundle`.

### C. The user has a single trace.zip
`specreel <trace.zip> -o out/ --title "..."` → `out/demo.html`.

## Share it
- One file: send `site/gallery.html` (self-contained).
- A URL: `specreel publish site --to ghpages` (needs a GitHub remote; prints the
  Pages URL + an `<iframe>` embed). Or `--to dir:/path` to copy into a webroot.

## Nice-to-haves (mention if relevant)
- `--ai` rewrites captions into friendlier narration (needs `ANTHROPIC_API_KEY`;
  opt-in, ~<$0.01/flow; falls back to literal captions without a key).
- `--theme light`, a 🔊 read-aloud toggle and a "Play all" tour in the players.
- A starter config: `specreel init test-results` writes a `specreel.yml`
  (titles, which flows are public, theme).
- CI: drop `examples/github-workflows/specreel.yml` to publish on every green build.

## Guardrails
- Capturing flows runs a real browser against the URL — use a local/staging URL,
  and don't submit destructive actions against production.
- Keep the user in the loop on the flow `TODO`s: only they know what "worked" means.
