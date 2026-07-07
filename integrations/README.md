# Integrations — let an AI coding tool run Specreel

The same single-file engine, reachable three ways so non-developers can use it by
asking their AI tool instead of learning a CLI.

| Surface | What it is | For |
|---|---|---|
| `pip install specreel` | The CLI + `specreel`/`specreel-mcp` commands. | Everyone (the foundation). |
| [`mcp/`](mcp/README.md) | An MCP server exposing `recommend / render / publish / …`. | Claude Code, Cursor, Claude Desktop. |
| [`claude-code/`](claude-code/specreel/SKILL.md) | A Claude Code skill that orchestrates the whole loop from one prompt. | Claude Code users. |

Everything here is a thin adapter over `specreel.py` — no logic is duplicated.

## The 30-second story
A vibe coder opens their AI tool and says *"make a demo gallery of my app at
localhost:3000."* The agent (via the MCP tools or the skill) runs
`recommend` → fills in the flow TODOs with the user → runs the scaffold → `render`
→ `publish`, and hands back a shareable URL. The demos are real Playwright traces,
so they're also tests — the moat is intact, the CLI barrier is gone.

> Capturing browser flows needs Playwright (`pip install playwright && playwright
> install chromium`). That's the one unavoidable dependency; the agent installs it.
