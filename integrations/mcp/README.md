# Specreel MCP server

Lets an AI coding tool (Claude Code, Cursor, Claude Desktop, …) drive Specreel, so
a non-developer can say *"turn my app into a demo gallery"* and the agent runs the
whole loop. It's a thin adapter — every tool shells out to `python -m specreel`, so
the single-file engine stays the source of truth.

## Install
```bash
pip install "specreel[mcp]"
```
This gives you the `specreel-mcp` command (and `specreel`).

## Tools
| Tool | Does |
|---|---|
| `recommend(url, max_pages, lang)` | Crawl a running app → scaffold suggested Playwright flows. |
| `render(traces_dir, out, bundle, ai, theme)` | Render a `test-results/` dir into a gallery. |
| `render_one(trace_zip, out, title)` | Render a single trace into one demo. |
| `publish(site, target)` | Deploy a gallery (`ghpages` or `dir:/path`) + embed snippet. |
| `summary(site, url)` | Markdown build summary. |
| `init_config(traces_dir)` | Scaffold a `specreel.yml`. |

A typical agent run for a new app with no tests: `recommend` → (edit the scaffold's
TODOs and run it to make traces) → `render` → `publish`.

## Wire it into your tool

**Claude Code** — add to `.mcp.json` in your project (or `claude mcp add`):
```json
{
  "mcpServers": {
    "specreel": { "command": "specreel-mcp" }
  }
}
```

**Cursor** — `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "specreel": { "command": "specreel-mcp" }
  }
}
```

**Claude Desktop** — `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "specreel": { "command": "specreel-mcp" }
  }
}
```

If `specreel-mcp` isn't on PATH, use the full interpreter path, e.g.
`{ "command": "/path/to/venv/bin/specreel-mcp" }`, or
`{ "command": "python", "args": ["-m", "specreel_mcp"] }`.

> Running a scaffolded flow needs Playwright installed (`pip install playwright &&
> playwright install chromium`). The agent will do this; it's the one unavoidable
> dependency for capturing browser flows.

## Take it to another machine (bundles)

Two ways to package the server so you can move it to another machine — both are
build scripts that write to `dist/` (not committed).

### Portable bundle (any machine, any client)
```bash
bash scripts/make_mcp_bundle.sh        # -> dist/specreel-mcp-bundle.zip
```
Copy the zip to the other machine, unzip, and run `install.sh` (or `install.ps1`
on Windows). It creates a local venv, installs `specreel[mcp]` from the bundled
wheel (plus Playwright), and prints the `specreel-mcp` command and a ready
MCP-client config. Cross-platform; needs Python 3.8+ and network access.

### One-click `.mcpb` (Claude Desktop / Claude Code)
```bash
bash scripts/make_mcpb.sh              # -> dist/specreel.mcpb
```
Produces an official MCP Bundle (`.mcpb`) with the server and its dependencies
vendored in. Double-click it in Claude Desktop (or drag it into Settings) to
install — no manual config.

<!-- The .mcpb vendors a compiled dependency (pydantic-core), so it's specific to
the OS/arch it was built on. Build it on each target platform, or use the portable
bundle above for a cross-platform installer. -->
Note: the `.mcpb` vendors a compiled dependency, so build it on the target platform
(or per-platform); the portable bundle is the cross-platform option.
