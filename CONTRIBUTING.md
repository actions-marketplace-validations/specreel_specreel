# Contributing to Specreel

Thanks for wanting to help! A few ground rules keep the open-core model honest.

## Licensing of contributions

- The Specreel CLI (`specreel.py`, `specreel_mcp.py`, `docs/`, `examples/`) is
  **AGPL-3.0-or-later** (see `LICENSE`).
- By submitting a contribution you certify the
  [Developer Certificate of Origin](https://developercertificate.org/) — sign your
  commits with `git commit -s`.
- Contributions are additionally granted to the maintainer under a **contributor
  license agreement**: you allow the maintainer to relicense your contribution as
  part of Specreel's commercial offerings (this is what lets the AGPL core and the
  hosted cloud coexist). <!-- TODO(legal): replace with a formal CLA document +
  CLA-assistant bot before accepting external PRs. -->
- The hosted service (Specreel Cloud) is a separate **proprietary** codebase and
  not open to external contribution.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest Pillow Flask
.venv/bin/python -m pytest -q          # the full suite must stay green
```

- The CLI must stay **single-file and stdlib-only** at runtime (Pillow only for
  `--mp4`). New surfaces are thin adapters over it, never logic forks.
- Never require changes to a user's tests — any `trace.zip` from any Playwright
  language binding must keep working.
- AI features are always opt-in + bring-your-own-key, with graceful degradation.
- Add a test with every behavior change; `specreel doctor` and the demo player
  contract tests in `tests/` show the style.

## Reporting bugs

Open a GitHub issue with the CLI version, a redacted trace if possible, and the
output of `specreel doctor <your-traces-dir>`. Security issues: see `SECURITY.md`.
