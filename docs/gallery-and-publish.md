# Gallery & publishing

## The gallery
Point Specreel at a directory of traces and it renders a gallery:
```bash
specreel test-results/ -o site/ --bundle
```
- `site/index.html` — the gallery: one card per flow, each with a **demo** + **test**
  pill (the same artifact), a real end-state thumbnail, duration, and a freshness badge.
  A **filter box** (type to match titles) + **All / Fresh / Failing** quick filters sit
  above the grid for large galleries.
- `site/<flow>/demo.html` — a self-contained player per flow.
- `site/gallery.html` — **one portable file**: the whole gallery + every player
  inlined, hash-routed, opens straight from `file://`. The "send a demo" artifact.
- `site/manifest.json` — machine-readable index (per-flow stats, `public`, `sig`, `healed`).

### Players
Each player has Prev / Play / Next, keyboard arrows, a step list, and:
- a **🔊 read-aloud** toggle. Two modes:
  - **Free / default** — narration via the browser Web Speech API (no audio files). It
    auto-selects the best *natural/neural* voice your browser ships (Edge and Chrome
    have excellent free ones) instead of the robotic system default, with a small
    **voice picker** to choose. Quality is browser/OS-dependent.
  - **Studio voiceover** (`--voice`) — pre-render real **neural TTS** audio per step
    (OpenAI TTS, BYO `OPENAI_API_KEY`) and embed it in the player. When present, the
    player plays the studio audio (and the "Play all" tour waits for each clip to
    finish) and falls back to the browser voice per step on any gap. See the CLI ref.
- **click-to-zoom + drag-to-pan** on the frame, to inspect detail (click again to reset);
- in the bundle, a **▶ Play all flows** tour that chains every flow back-to-back.

(Auto-zoom *to the active element* per step is still deferred — vanilla Playwright
traces don't carry element rectangles, only DOM snapshots; the zoom above is manual.)

### Freshness / "updated" badge
On each build, Specreel diffs every flow's step signature against the previous
`manifest.json`. A flow that **changed but still passes** gets an amber
`⟳ updated` badge — the UI moved, the test stayed green, the demo refreshed. A CI
self-healing step can also force it via `healed: true` in [config](configuration.md).

## Customer-facing showcase
One build serves two audiences. The gallery above is the **engineering view**: every
flow, failures front and center, build metadata — a health dashboard. `--showcase`
(or `showcase: true` in [config](configuration.md)) additionally emits
`site/showcase/`, the **customer view**: only flows marked `public: true` that are
**passing**, with no failure states or test jargon — just titled demos, a search box,
and the one thing worth keeping from CI: a *"verified · build N · date"* provenance
badge, because "re-verified on every release" is the pitch.

```bash
specreel test-results/ -o site/ --showcase
#   site/index.html            <- full gallery (internal)
#   site/showcase/index.html   <- curated gallery (customer-facing)
```

Why a separate directory instead of a filter? The players **embed their frames** —
a failing or internal flow "hidden" with CSS in a shared page would still ship its
video bytes, flow names, and error captions to anyone who views source. Curation
happens at generation time: what's excluded is *absent*, not unlinked. For the same
reason the showcase directory is rebuilt from scratch each time — a flow that was
public last build but is failing or private now doesn't linger.

Semantics:
- A **failing public flow is dropped** for that build (the CLI says so). If *no*
  public flow passes, no showcase is emitted at all.
- `site/showcase/manifest.json` lists only what's shown, with no health fields.
- With `bundle: true`, the showcase also gets its own curated `gallery.html`.
- The top-level `manifest.json` gains `"showcase": true` so tooling (and Specreel
  Cloud) knows the build carries one.

Brand it with the `showcase_*` keys ([configuration](configuration.md)): headline,
tagline, accent color, an embedded logo, or a raw CSS file for full control.

### Set it up — pick your path

**Open-source CLI (static hosting, you control the URLs):**
1. In `specreel.yml`, set `showcase: true` and mark the customer-ready flows
   `public: true`.
2. Render: `specreel test-results/ -o site/` — the curated render lands in
   `site/showcase/`.
3. Share it, either way:
   - *Two URLs from one publish* — `specreel publish site/ --to ghpages` serves the
     full gallery at `/` and the curated one at `/showcase/`; share the latter.
   - *Public host gets the showcase only* —
     `specreel publish site/showcase --to dir:/var/www/demos`, so the internal view
     never leaves CI.

**Specreel Cloud (one URL, membership does the gating):**
1. Same config — `showcase: true` + `public: true` flows. Hosted runs honor your
   `specreel.yml`; CLI publishes carry the showcase automatically.
2. Publish (`--to cloud`) or let a scheduled run do it.
3. Project page → **Public gallery view → showcase**. Visitors at
   `/g/<org>/<project>/` now get only the curated render; org members keep the full
   gallery at the same URL. Details:
   [cloud](cloud.md#customer-facing-showcase-public-gallery-view).

## Publishing
Deploy a generated gallery to a real URL and get an `<iframe>` embed snippet:

```bash
# GitHub Pages (needs a GitHub remote): clean single-commit gh-pages force-push
specreel publish site/ --to ghpages
#   pushed gh-pages -> origin
#   URL (enable once: Settings → Pages → Branch: gh-pages /root):
#   https://<you>.github.io/<repo>/

# Or copy into any static webroot / synced folder
specreel publish site/ --to dir:/var/www/demos

# Or push to Specreel Cloud — a hosted gallery with a dashboard + analytics
specreel publish site/ --to cloud --project my-app \
  --cloud-url https://app.specreel.dev --token scl_xxx
```

Prefer zero setup? Just **send `site/gallery.html`** — it's one self-contained file.
For the hosted option (private galleries, view analytics, no CI), see [Specreel Cloud](cloud.md).

To publish automatically on every green build, use the
[GitHub Action](github-action.md).

## Notifications
Post a build summary to Slack (BYO incoming webhook):
```bash
specreel test-results/ -o site/ --notify "$SLACK_WEBHOOK" --url https://acme.github.io/app/
```
or set `notify_webhook:` / `public_url:` in `specreel.yml`, or `SPECREEL_SLACK_WEBHOOK`.
