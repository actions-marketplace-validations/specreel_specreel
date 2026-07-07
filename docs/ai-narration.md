# AI narration (opt-in, BYO-key)

By default, captions are **100% deterministic** — derived from the recorded
actions and assertions, no AI, no network. AI narration is an optional layer that
rewrites those captions into friendlier, sales-engineer-style lines.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python specreel.py test-results/ -o site/ --ai
python specreel.py trace.zip -o out/ --ai --ai-model claude-haiku-4-5   # cheaper
```
Or enable it in [`specreel.yml`](configuration.md):
```yaml
ai: true
ai_model: claude-opus-4-8
product_name: Acme     # narration says the product, not "localhost:3000"
```

## How it works
- One batched, structured-output request per flow to the Anthropic Messages API
  (`claude-opus-4-8` by default). Roughly **< $0.01 per flow**.
- The **literal caption stays the source of truth** — it's shown as a gray
  sub-line beneath the narration, and it's what the freshness signature hashes.
- **Graceful degrade:** no key, or any API error, and you simply get the literal
  captions (the build never fails because of narration).
- Secrets are masked: a `password`/`secret`/`otp`/… field never appears in plain
  text, in either the literal caption or the narration.

## Example
| Literal caption | AI narration |
|---|---|
| Click the "Communications" link | **Click into the Communications section** |
| Type "CEO" into the "Search documents…" field | **Type CEO into the search field** |
| Type "••••••••" into the "Create a password" field | **Set a secure password** |

## Cost & privacy
- **BYO-key**, metered, never bundled. Use `claude-haiku-4-5` for the cheapest runs.
- Only the step captions (+ optional `product_name`) are sent — not your
  screenshots or page content.
