# Changelog

All notable changes to the `specreel` CLI. The hosted cloud service is versioned
separately (it ships from its own deploy, not from this package).

This project follows [semantic versioning](https://semver.org/) loosely while
pre-1.0: minor bumps carry features and fixes, and the `trace.zip` input contract
is what we treat as sacred.

## [Unreleased]

### Added
- **Customer-facing showcase** (`--showcase` / `showcase: true`). One build, two
  renders: the full gallery stays the engineering health view, and `site/showcase/`
  is a curated, brandable gallery for customers — only flows marked `public: true`
  that are passing, no failure states, provenance ("verified · build · date") kept.
  Excluded flows are absent from the directory, not hidden — players embed their
  frames, so CSS-hiding in a shared page would still ship the bytes. Brand it with
  `showcase_title/tagline/accent/logo/css`; the flow slug `showcase` is now reserved.
- **Narrated MP4s.** `--voice --mp4` now muxes the studio voiceover into the video
  as a real audio track — the shareable artifact finally talks. A narration line
  that outruns its step extends that step's screen time instead of being cut
  mid-sentence; without clips (or without ffprobe) the MP4 ships silent as before.
- **Voiceover clip cache.** Synthesized clips are cached on disk keyed on
  (model, voice, instructions, words) — CI re-renders every build, and unchanged
  steps no longer re-bill. `SPECREEL_TTS_CACHE=<dir>` to relocate, `=off` to disable.
  Synthesis also fans out over a few threads with one retry per clip.
- **`--tts-instructions`** (/ `tts_instructions:`) — delivery notes for the narrator
  on `gpt-`* TTS models; defaults to a calm product-demo narrator.

### Changed
- **Paid narration is heard by default.** A demo built with `--voice` opens with the
  sound toggle on (audio still starts only on the viewer's Play click) and labels it
  "🔊 Voiceover"; the browser-voice picker hides when studio audio is present. The
  viewer's on/off choice and picked voice persist per browser.
- **Failures are audible.** The voice narrates a failed step as "This step failed:
  …, the reason" instead of reading it like a passing one; masked secrets (`•••`)
  are spoken as "the hidden value" and url schemes are dropped for the ear.
- The two players (single demo + bundle) now share one narration engine — they had
  drifted apart as hand-maintained copies.

### Fixed
- Turning the sound toggle off mid-step now also stops an in-flight studio clip
  (it used to keep playing to the end).
- An intermittently silent step under the browser voice: Chrome can drop an
  utterance queued synchronously after `cancel()`; the player now breathes 60ms.

## [0.2.0] — 2026-08-23

The first release since the initial publish. Everything below shipped to `main`
over the intervening weeks; 0.1.0 predates all of it.

### Added
- **Apps behind a login.** `recommend` and the render path can sign in first, so a
  logged-in app can be crawled, demoed and monitored. Credentials arrive via env,
  are masked in captions, and the sign-in steps are trimmed out of the finished
  demo so a shared gallery never shows the account.
- **GIF export** (`--gif`) — the format that actually embeds in a README.
- **Real motion in the video.** Each step carries the frames recorded *during* it
  rather than one still, plus a click cursor + ripple, browser chrome showing the
  page's URL, and title/outro cards.
- **Capture coverage guard.** Every build warns when a flow's opening or outcome
  was never recorded; `--strict` fails CI on it. The engine can't invent a frame
  the test didn't capture, so it says so instead of shipping a truncated demo.
- **`--quality high|medium|low`** — the file-size knob (frames kept per step).

### Changed
- **Scaffolds stop inventing locators.** `recommend` only emits selectors it
  actually saw, and leaves a `TODO` where a human has to decide — a made-up name
  failed on the first click.
- **Failures say why.** A failed step reports the reason instead of vanishing.
- **Better crawls**, tuned against a six-site sweep: hidden fields are skipped,
  generated code is safe to run, pages that navigate themselves no longer abort
  the scan, and AI curation retries instead of degrading on a transient error.
- **NL flows** scroll visibly, settle after navigation, and humanize key presses.
- Clicking the player toggles play/pause.

### Fixed
- The opening frame of a demo showed the *previous* page (a nav paints hundreds of
  ms after `goto` resolves); steps now pin to their settled frame.
- Demos ran one beat behind their captions, and could end before the outcome.
- Browser chrome showed an embedded iframe's URL instead of the page's.
- Explicit `waitForTimeout` calls no longer surface as demo steps.

## [0.1.0] — 2026-07-07

Initial public release: `trace.zip` → HTML player + MP4, batch galleries,
`specreel.yml` config, opt-in BYO-key AI narration, `publish`, `recommend`,
`init`, `summary`, the GitHub Action, and the MCP server.
