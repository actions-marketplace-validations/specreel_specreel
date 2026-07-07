# Security policy

## Reporting a vulnerability

Please email **security@specreel.dev** (or hello@specreel.dev) with details —
do **not** open a public issue for security reports. You'll get an acknowledgment
within 72 hours. Coordinated disclosure appreciated; we'll credit you unless you
prefer otherwise.

## Scope notes for researchers

- Demo galleries embed **screenshots of the app under test** — anything visible on
  screen during a test run is in the artifact. Specreel masks values typed into
  secret-looking fields (`password`/`otp`/`cvv`/…) in captions, but cannot redact
  pixels; treat gallery visibility (public/private/review links) as the boundary.
- The cloud's server-side crawler and hosted runs enforce an SSRF guard
  (private/loopback/metadata addresses are refused unless explicitly allowed for
  self-hosting) — bypasses are in scope and very welcome reports.
- API tokens, review links, invites, and email tokens are stored **hashed**
  (SHA-256) and shown once.

## Supported versions

Pre-1.0: only the latest release receives fixes.
