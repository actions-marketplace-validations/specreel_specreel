# Configuration (`specreel.yml`)

A `specreel.yml` in your working directory (or the traces directory, or passed
via `--config`) configures a gallery build. It's parsed by a tiny built-in YAML
subset — no PyYAML needed. Generate a starter with `specreel init <traces>`.

```yaml
title: Acme — Product Flows      # gallery heading / kicker
product_name: Acme               # AI narration says this instead of localhost URLs
quality: high              # motion: high | medium | low (file size tradeoff)
theme: dark                         # dark | light
ai: false                           # true (+ ANTHROPIC_API_KEY) to narrate captions
ai_model: claude-opus-4-8           # model for narration (haiku-4-5 is cheaper)
bundle: false                       # true to also emit the single-file gallery.html

public_url: https://acme.github.io/app/   # used in notifications / links
notify_webhook: https://hooks.slack.com/…  # Slack incoming webhook (or --notify)
analytics: '<script defer data-domain="x" src="https://plausible.io/js/script.js"></script>'

# Leading navigations to drop from every demo (e.g. an auth-login redirect)
setup_urls:
  - /test-api-key

flows:                              # keyed by flow slug (matches the gallery output)
  signup:
    title: Sign up
    public: true
  communications:
    title: Browse the document library
    public: true
  internal-admin:
    hidden: true                    # exclude from the gallery
  some-flow:
    healed: true                    # force the amber "updated" badge (e.g. a CI healer)
```

## Top-level keys
| Key | Type | Meaning |
|---|---|---|
| `title` | string | Gallery heading (the green kicker on the index). |
| `product_name` | string | AI narration uses this instead of raw `localhost` URLs. |
| `theme` | `dark` \| `light` | Color theme. CLI `--theme` overrides. |
| `ai` | bool | Enable AI narration (still needs a key). CLI `--ai` also enables. |
| `ai_model` | string | Narration model. |
| `bundle` | bool | Also emit `gallery.html`. CLI `--bundle` also enables. |
| `public_url` | string | Public gallery URL for notifications/links. |
| `notify_webhook` | string | Slack incoming-webhook URL. |
| `analytics` | string | Raw HTML injected into every page's `<head>` (BYO Plausible/GA/…). |
| `setup_urls` | list | `goto` steps to these URLs are trimmed from every demo. |
| `flows` | map | Per-flow overrides, keyed by slug. |

## Per-flow keys (`flows.<slug>`)
| Key | Type | Meaning |
|---|---|---|
| `title` | string | Friendly title (overrides the path-derived one). |
| `public` | bool | Tags the card `public` and marks it public in the manifest. |
| `hidden` | bool | Excludes the flow from the gallery entirely. |
| `healed` | bool | Forces the amber `⟳ updated` state (for a CI self-healing step). |

> Slugs come from the trace's directory name (Playwright names result dirs after
> the test). `specreel init` writes the exact slugs for you.
