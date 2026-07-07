# Gallery & publishing

## The gallery
Point Specreel at a directory of traces and it renders a gallery:
```bash
python specreel.py test-results/ -o site/ --bundle
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

## Publishing
Deploy a generated gallery to a real URL and get an `<iframe>` embed snippet:

```bash
# GitHub Pages (needs a GitHub remote): clean single-commit gh-pages force-push
python specreel.py publish site/ --to ghpages
#   pushed gh-pages -> origin
#   URL (enable once: Settings → Pages → Branch: gh-pages /root):
#   https://<you>.github.io/<repo>/

# Or copy into any static webroot / synced folder
python specreel.py publish site/ --to dir:/var/www/demos

# Or push to Specreel Cloud — a hosted gallery with a dashboard + analytics
python specreel.py publish site/ --to cloud --project my-app \
  --cloud-url https://app.specreel.dev --token scl_xxx
```

Prefer zero setup? Just **send `site/gallery.html`** — it's one self-contained file.
For the hosted option (private galleries, view analytics, no CI), see [Specreel Cloud](cloud.md).

To publish automatically on every green build, use the
[GitHub Action](github-action.md).

## Notifications
Post a build summary to Slack (BYO incoming webhook):
```bash
python specreel.py test-results/ -o site/ --notify "$SLACK_WEBHOOK" --url https://acme.github.io/app/
```
or set `notify_webhook:` / `public_url:` in `specreel.yml`, or `SPECREEL_SLACK_WEBHOOK`.
