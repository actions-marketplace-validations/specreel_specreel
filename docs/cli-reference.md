# CLI reference

Specreel has one default command (render a trace or a directory) plus a few verbs.

## Render — `specreel.py <trace|dir> [options]`
- `<trace>` — a `trace.zip` (single demo) **or** a directory to scan (gallery/batch).
- `-o, --out PATH` — output directory (default `specreel-out`).
- `--title TITLE` — demo title (single-trace mode).
- `--mp4` — also export `demo.mp4` (needs Pillow + ffmpeg).
- `--bundle` — also emit a single self-contained `gallery.html` (batch mode).
- `--theme dark|light` — color theme (default `dark`; or set `theme:` in config).
- `--config PATH` — path to `specreel.yml` (auto-discovered in CWD / the traces dir otherwise).
- `--ai` — opt-in [AI narration](ai-narration.md) (needs `ANTHROPIC_API_KEY`).
- `--ai-model MODEL` — model for `--ai` (default `claude-opus-4-8`; `claude-haiku-4-5` is cheaper).
- `--api-key KEY` — Anthropic key (else read from `ANTHROPIC_API_KEY`).
- `--notify URL` — Slack incoming-webhook to post a build summary to (batch).
- `--url URL` — public gallery URL to include in the notification/links.
- `--voice [NAME]` — **studio voiceover**: pre-render neural TTS narration per step and
  embed it (BYO `OPENAI_API_KEY`). Default voice `nova` (also alloy/echo/fable/onyx/
  shimmer/…). The player prefers this audio and falls back to the browser voice per step.
- `--tts-model MODEL` — TTS model for `--voice` (default `gpt-4o-mini-tts`; `tts-1`
  cheaper, `tts-1-hd` higher fidelity).
- `--tts-key KEY` — OpenAI key for `--voice` (else `OPENAI_API_KEY`).
  Config equivalents: `voice:` / `tts_model:` in `specreel.yml`.

> Studio voiceover pairs well with `--ai` (it narrates the friendly prose). It costs a
> few cents/flow and adds audio bytes to the HTML; the free browser-voice default needs
> no key and no audio files.

```bash
python specreel.py trace.zip -o out/ --title "Checkout"
python specreel.py test-results/ -o site/ --bundle --ai --theme light
```

### Secrets in captions
Typed values into secret fields are masked (`••••••••`) so they never leak into a
shareable demo. Detection is conservative — it keys off the field's selector/label
matching a curated keyword set (`password`, `passcode`, `otp`, `cvv`, `ssn`, …), so
`input[name=password]`, `get_by_label("Passcode")`, etc. are all covered. A secret
field located by a **placeholder with no keyword** (e.g. `get_by_placeholder("at
least 8 characters")`) can't be detected from the trace alone — locate such fields by
**`type=password`** (e.g. `page.locator("input[type=password]")`) so masking applies.

## `recommend <url>` — suggest + scaffold flows for a new app
Crawl a running app, propose demo-worthy flows, and write a runnable Playwright
script. See [recommend](recommend.md).
- `--max N` — max pages to crawl (default 12).
- `--browser` — render pages with Playwright first (for client-rendered SPAs).
- `--wait MS` — render wait in `--browser` mode (default 1200).
- `--cookie "k=v; ..."` — Cookie header for a **logged-in crawl** (static + browser).
- `--header "K: V"` — extra request header (repeatable), e.g. an `Authorization` bearer.
- `--lang py|js` — scaffold language (auto-detected from `package.json` otherwise).
- `-o FILE` — scaffold path (default `specreel_flows.<lang>`).
- `--ai` / `--api-key` — curate & rename the suggestions with AI.
- `--json` — emit suggestions + scaffold as JSON on stdout (progress/errors to stderr,
  no file written). Used by Specreel Cloud's onboarding wizard.

```bash
python specreel.py recommend http://localhost:3000 --max 10
```

## `scaffold --specs <json>` — assemble a script from a wizard spec
Build a Playwright scaffold from a JSON spec `{base_url, lang, items[]}`, where each
item is a discovered flow dict **or** `{"nl_prompt": "log in then…"}`. NL items are
turned into runnable flows by AI (BYO-key), grounded in the other flows; without a key
they're left as a `TODO` stub. Powers Specreel Cloud's onboarding wizard.
- `--api-key KEY` (or `ANTHROPIC_API_KEY`) — needed for `nl_prompt` items.
- `-o FILE` — output (default stdout).

## `curate --flows <json> --instruction "…"` — refine flows in plain English
Reorder / rename / **drop** discovered flows by a plain-English instruction (e.g.
"focus on checkout, drop the marketing pages"). AI, BYO-key; never invents flows.
Prints the new flow list as JSON.

## `doctor [path]` — preflight your traces
Sanity-check a traces dir (or a single `trace.zip`) before rendering: are traces
present, are they valid, do they have screencast frames (so the demo has visuals),
does `specreel.yml` parse? Exits non-zero if something's fatal — handy as a CI gate.
- `path` — a `trace.zip` or directory (default `test-results`).
- `--mp4` — also check MP4 export readiness (Pillow + ffmpeg).

```bash
python specreel.py doctor test-results
#   ✓ signup/trace.zip: 5 steps · 12 frames
#   ⚠ checkout/trace.zip: 4 steps but 0 screencast frames — enable screenshots in tracing
#   ready (with warnings) — 0 error(s), 1 warning(s)
```

## `init <traces>` — scaffold a `specreel.yml`
Discover traces under a directory and write a starter config whose slugs match
the gallery output.
- `-o FILE` — output path (default `specreel.yml`).

```bash
python specreel.py init test-results/
```

## `publish <site> --to <target>` — deploy a gallery
Deploy an already-generated gallery to a real URL + print an `<iframe>` embed.
See [publishing](gallery-and-publish.md#publishing).
- `--to ghpages` — clean single-commit `gh-pages` force-push → Pages URL (needs a GitHub remote).
- `--to dir:<path>` — copy into a static webroot / synced folder.
- `--to cloud` — upload to [Specreel Cloud](cloud.md) (`--cloud-url`/`--token`/`--project`,
  or `SPECREEL_CLOUD_URL` / `SPECREEL_CLOUD_TOKEN`).
- `--message MSG` — commit message for `ghpages`.

## `summary <site> [--url U] [--since OLD]` — markdown build summary
Print a per-flow markdown table from `manifest.json` (used by the PR-comment
workflow; handy in any CI). `--url` adds links. `--since <manifest.json>` adds a
**“Changes vs previous build”** block (regressed / recovered / added / removed) —
the PR-comment workflow fetches the live manifest and passes it automatically.

## `notes <site> [--since OLD]` — AI release notes (BYO-key)
Draft user-facing release notes from the gallery (and, with `--since`, from what
changed). Opt-in AI, BYO-key (`ANTHROPIC_API_KEY` / `--api-key`); degrades to a
non-zero exit if no key. Recovered flows read as fixes, regressed as known issues,
added as new.
- `--since <manifest.json>` — diff against a previous build.
- `--product NAME` — product name for the notes.
- `--ai-model MODEL` / `--api-key KEY` — model + key (default `claude-opus-4-8`).
- `-o FILE` — write to a file instead of stdout.

```bash
python specreel.py notes site --since prev/manifest.json --product "Acme" -o RELEASE.md
```

## Environment variables
- `ANTHROPIC_API_KEY` — key for `--ai` (narration, recommend curation).
- `OPENAI_API_KEY` — key for `--voice` (studio neural voiceover).
- `SPECREEL_SLACK_WEBHOOK` — default Slack webhook for `--notify`.
- `BASE_URL` — read by scaffolded `recommend` scripts to override the app URL.
