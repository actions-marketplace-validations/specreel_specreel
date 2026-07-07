#!/usr/bin/env python3
"""
Specreel MCP server — a thin adapter that lets an AI coding tool drive Specreel.

It exposes the Specreel CLI as MCP tools (recommend / render / publish / summary /
init). Each tool shells out to `python -m specreel`, so there is no logic here —
the single-file engine stays the one source of truth.

Install:  pip install "specreel[mcp]"
Run:      python -m specreel_mcp          (or: specreel-mcp, once installed)

Typical agent sequence for a brand-new app with no tests:
  1. recommend(url)            -> scaffolds specreel_flows.py
  2. (the agent edits the TODOs, then runs the scaffold to make traces)
  3. render("test-results")    -> builds the gallery
  4. publish("site", "ghpages")-> shares it
"""
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("specreel")


def _run(args, timeout=900):
    """Run a specreel CLI command in the current working directory."""
    try:
        p = subprocess.run([sys.executable, "-m", "specreel", *args],
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()
        return out[-6000:] if out else f"(exit {p.returncode}, no output)"
    except FileNotFoundError:
        return ("specreel is not installed in this environment. "
                "Run: pip install 'specreel[mcp]'")
    except subprocess.TimeoutExpired:
        return "specreel timed out."


@mcp.tool()
def recommend(url: str, max_pages: int = 12, lang: str = "py") -> str:
    """Crawl a RUNNING web app and scaffold suggested Playwright demo flows.

    Use this first when the user has an app but no end-to-end tests yet. It writes
    a runnable script (specreel_flows.<lang>) with one function per suggested flow.
    After it returns, edit the TODOs in that file, run it to produce
    test-results/*/trace.zip, then call `render`.

    url: base URL of the running app (e.g. http://localhost:3000).
    max_pages: how many pages to crawl.
    lang: 'py' or 'js' scaffold.
    """
    return _run(["recommend", url, "--max", str(max_pages), "--lang", lang])


@mcp.tool()
def render(traces_dir: str, out: str = "site", bundle: bool = True,
           ai: bool = False, theme: str = "dark") -> str:
    """Render a directory of Playwright trace.zip files into a demo gallery.

    traces_dir: a directory containing trace.zip files (e.g. 'test-results').
    out: output directory for the gallery.
    bundle: also emit a single self-contained gallery.html.
    ai: opt-in AI narration (needs ANTHROPIC_API_KEY in the environment).
    theme: 'dark' or 'light'.
    """
    args = [traces_dir, "-o", out, "--theme", theme]
    if bundle:
        args.append("--bundle")
    if ai:
        args.append("--ai")
    return _run(args)


@mcp.tool()
def render_one(trace_zip: str, out: str = "out", title: str = "") -> str:
    """Render a single trace.zip into one shareable demo (out/demo.html)."""
    args = [trace_zip, "-o", out]
    if title:
        args += ["--title", title]
    return _run(args)


@mcp.tool()
def publish(site: str, target: str = "ghpages") -> str:
    """Deploy a generated gallery to a real URL and print an embed snippet.

    site: the gallery directory (e.g. 'site').
    target: 'ghpages' (push to a gh-pages branch -> GitHub Pages URL; needs a
            GitHub remote) or 'dir:/path' (copy into a static webroot).
    """
    return _run(["publish", site, "--to", target])


@mcp.tool()
def summary(site: str, url: str = "") -> str:
    """Print a markdown build summary (per-flow pass/updated/failed) from a gallery."""
    args = ["summary", site]
    if url:
        args += ["--url", url]
    return _run(args)


@mcp.tool()
def init_config(traces_dir: str) -> str:
    """Scaffold a starter specreel.yml from a directory of traces."""
    return _run(["init", traces_dir])


def main():
    mcp.run()


if __name__ == "__main__":
    main()
