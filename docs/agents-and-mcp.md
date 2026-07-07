# Use it from an AI coding tool (MCP & skill)

You don't have to learn the CLI. Specreel can be driven by an AI coding tool
(Claude Code, Cursor, Claude Desktop), so you can just ask: *"turn my app into a
demo gallery."* Each surface is a thin wrapper over the same engine.

## Install
```bash
pip install "specreel[mcp]"     # gives you `specreel` and `specreel-mcp`
# to capture browser flows, also:
pip install playwright && playwright install chromium
```

## Option 1 — the MCP server
Exposes Specreel as tools the agent can call: `recommend`, `render`, `render_one`,
`publish`, `summary`, `init_config`. Wire it into your tool:

```json
{
  "mcpServers": {
    "specreel": { "command": "specreel-mcp" }
  }
}
```
(Claude Code: `.mcp.json` · Cursor: `~/.cursor/mcp.json` · Claude Desktop:
`claude_desktop_config.json`.) Full details: `integrations/mcp/README.md`.

## Option 2 — the Claude Code skill
Copy `integrations/claude-code/specreel/` into your project's `.claude/skills/`.
The agent then knows the whole workflow and picks the right path:
- running app, no tests → `recommend` → finish the flow TODOs → run → `render`;
- existing tests → run with tracing on → `render`;
- a single trace → one demo.

## What the agent does for a brand-new app
1. `recommend http://localhost:3000` → scaffolds `specreel_flows.py`.
2. Works with you to finish each flow's `TODO` (what "this worked" means).
3. Runs the scaffold → `test-results/*/trace.zip`.
4. `render test-results -o site --bundle` → the gallery.
5. `publish site --to ghpages` → a shareable URL.

## Guardrails
- Capturing flows runs a real browser against the URL — use a local/staging URL,
  and don't run mutating flows against production.
- You stay in the loop on the flow TODOs; only you know what "worked" means.
