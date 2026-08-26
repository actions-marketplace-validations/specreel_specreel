#!/usr/bin/env python3
"""
specreel — Phase 1 spike

Turn a Playwright trace.zip into a *watchable* demo: an HTML walkthrough
(and optional MP4) with plain-English captions auto-derived from the
recorded actions and assertions. No AI. Works on any trace.zip regardless
of the language the test was written in (JS/TS/Python/Java/.NET).

Usage:
    specreel path/to/trace.zip -o out/ --title "Sign up flow"
    specreel path/to/trace.zip -o out/ --mp4
"""
import argparse, base64, hashlib, html, io, json, os, re, shutil, subprocess, sys, tempfile, time, zipfile
from html.parser import HTMLParser

# ----------------------------------------------------------------------------
# 1. Parse the trace
# ----------------------------------------------------------------------------

def load_events(trace_dir):
    events = []
    for fn in os.listdir(trace_dir):
        if fn.endswith(".trace"):
            with open(os.path.join(trace_dir, fn), encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
    return events


# actions that are plumbing, not user-meaningful demo steps
SKIP_METHODS = {"newPage", "newContext", "close", "setContent", "waitForLoadState",
                "addInitScript", "setViewportSize", "tracingStart", "tracingStop",
                "waitForTimeout", "waitForEventInfo",
                # Playwright ≥1.53 emits waits as `__waitInfo__` (replaces
                # waitForEventInfo). Leaving it through humanize's fallback
                # captions the step as literally "__waitInfo__" (AI then invents
                # "Wait a moment while things load" on top).
                "__waitInfo__",
                # Element/network plumbing — not demo-worthy. Outcome waits
                # (`waitForURL`, `waitForFunction`) are kept: chat replies and
                # scorecard results must be visible steps or the demo ends on
                # Send/Run before the UI catches up.
                "waitForSelector", "waitForResponse",
                "waitForRequest",
                # Read/scroll helpers — steal frame windows from the real click/
                # fill they sandwich, and caption as gibberish ("innerText").
                "scrollIntoViewIfNeeded", "innerText", "textContent",
                "inputValue", "getAttribute", "evaluate", "evaluateHandle",
                "evaluateExpression", "evalOnSelector", "evalOnSelectorAll"}


def _is_main_frame(snap):
    """Only the top-level frame's URL belongs in the browser chrome. Pages embed
    third-party iframes (Stripe, analytics, chat widgets) whose snapshots would
    otherwise surface as the page's address."""
    return bool(snap.get("isMainFrame")) and not str(
        snap.get("frameUrl") or "").startswith("about:")


def final_snapshot_url(events):
    """The last MAIN-FRAME URL the trace recorded — where the flow ended."""
    last = ""
    for e in events:
        if e.get("type") == "frame-snapshot":
            snap = e.get("snapshot") or {}
            if _is_main_frame(snap):
                last = snap["frameUrl"]
    return last


def _snapshot_urls(events):
    """callId -> the page URL when that call ran, from frame-snapshot events.
    Prefer the AFTER snapshot (post-navigation) so a click that navigates
    reports where it landed, not where it started."""
    urls = {}
    for e in events:
        if e.get("type") != "frame-snapshot":
            continue
        snap = e.get("snapshot") or {}
        cid, name = snap.get("callId"), snap.get("snapshotName", "")
        if not cid or not _is_main_frame(snap):
            continue
        if name.startswith("after@") or cid not in urls:
            urls[cid] = snap["frameUrl"]
    return urls


def build_steps(events):
    befores = {e["callId"]: e for e in events if e.get("type") == "before"}
    afters = {e["callId"]: e for e in events if e.get("type") == "after"}
    snap_urls = _snapshot_urls(events)
    frames = sorted(
        [e for e in events if e.get("type") == "screencast-frame"],
        key=lambda e: e["timestamp"],
    )
    steps = []
    for cid, b in befores.items():
        method = b.get("method", "")
        # Skip known plumbing + any Playwright protocol/internal method
        # (`__waitInfo__`, `__abort__`, …) so new PW versions don't leak raw
        # protocol names into demo captions.
        if method in SKIP_METHODS or method.startswith("__"):
            continue
        a = afters.get(cid, {})
        steps.append({
            "method": method,
            "params": b.get("params", {}),
            "start": b.get("startTime"),
            "end": a.get("endTime", b.get("startTime")),
            "error": a.get("error"),
            # where the action actually landed (clicks/checks record their
            # resolved x,y) — powers the player's cursor + click ripple
            "point": a.get("point"),
            # the page URL at this step (browser-chrome pill). From the trace's
            # snapshots, so a click that navigates shows its destination.
            "url": snap_urls.get(cid),
        })
    steps.sort(key=lambda s: s["start"] if s["start"] is not None else 0)
    return steps, frames


def _same_selector(a, b):
    return a.get("params", {}).get("selector") == b.get("params", {}).get("selector")


def coalesce_steps(steps):
    """Merge UI micro-steps into one demo-meaningful step.

    Today: a `fill` immediately followed by `press Enter` on the same selector
    is how you submit a field — render it as a single "Submit ..." step rather
    than two ("Type ..." then "Press Enter"). The merged step keeps the typed
    value and spans both timings so its result frame shows the post-submit state.
    """
    out = []
    i = 0
    while i < len(steps):
        s = steps[i]
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if (s["method"] == "fill" and nxt and nxt["method"] == "press"
                and str(nxt["params"].get("key", "")).lower() == "enter"
                and _same_selector(s, nxt)):
            merged = dict(s)
            merged["method"] = "submit"
            merged["end"] = nxt["end"]
            merged["error"] = s.get("error") or nxt.get("error")
            out.append(merged)
            i += 2
            continue
        out.append(s)
        i += 1
    return out


def attach_frames(steps, frames):
    """Pick the screencast frame that best shows each step's *result* state.

    Each step pins to its SETTLED result — the last frame before the NEXT step
    begins. A step's visible outcome persists until the next action, so the frame
    just before that action shows it fully rendered. This matters most for the
    opening navigation: a page paints hundreds of ms after `goto` resolves, so a
    frame picked right after the action would still show the *previous* page —
    the last-frame-before-next rule shows the loaded destination the caption
    promises. (For a fast fill/click the settled frame is well past the paint, so
    this never renders a step "one beat behind" its caption.)

    The FINAL step always pins to the trace's last frame: tests often stop tracing
    the instant the last action resolves, so the closest-to-endTime frame can
    predate the outcome entirely — the last captured frame is the truest end
    state we have. Consecutive duplicate frames are nudged to avoid a stutter.
    """
    n = len(steps)
    prev_sha = None
    for i, s in enumerate(steps):
        if frames and i == n - 1:
            s["frame"] = frames[-1]["sha1"]
            prev_sha = s["frame"]
            continue
        end = s["end"] if s["end"] is not None else s["start"]
        nxt = next((steps[j]["start"] for j in range(i + 1, n)
                    if steps[j].get("start") is not None), None)
        best = None
        if frames and nxt is not None:
            # the settled result: the last frame before the next step starts
            # (at/after this step's end when such a frame exists).
            window = ([fr for fr in frames if end <= fr["timestamp"] < nxt]
                      or [fr for fr in frames if fr["timestamp"] < nxt])
            if window:
                best = max(window, key=lambda fr: fr["timestamp"])
        if best is None and frames:
            # no next step (or no frame before it): first frame a paint-lag past
            # this step's end, else the closest available.
            PAINT_LAG = 100  # ms
            after = ([fr for fr in frames if fr["timestamp"] >= end + PAINT_LAG]
                     or [fr for fr in frames if fr["timestamp"] >= end])
            best = (min(after, key=lambda fr: fr["timestamp"]) if after
                    else max(frames, key=lambda fr: fr["timestamp"]))
        # showing the prior step's exact frame again reads as a stutter — prefer a
        # distinct frame within this step's window if one exists.
        if best and best["sha1"] == prev_sha and nxt is not None:
            distinct = [fr for fr in frames
                        if end <= fr["timestamp"] < nxt and fr["sha1"] != prev_sha]
            if distinct:
                best = max(distinct, key=lambda fr: fr["timestamp"])
        s["frame"] = best["sha1"] if best else None
        prev_sha = s["frame"]


def capture_coverage(steps, frames):
    """Did the trace actually capture both ends of the flow?

    Returns {"opening": bool, "outcome": bool, "issues": [str, ...]}. This is the
    systematic guard against the recurring "missing first/last step" failure:
    the engine pins the best available frame to each end (attach_frames), but it
    can't invent a frame the test never recorded. This detects that gap so a build
    can WARN (always) or FAIL (--strict) instead of silently shipping a truncated
    demo. Pure + testable.

    - outcome: a frame exists at/after the last action's end — else the demo ends
      on the pre-action state (the classic "missing last step").
    - opening: a frame exists at/after the first step resolved — else the demo
      opens on a blank/previous page (the "missing first step").
    """
    if not steps or not frames:
        return {"opening": True, "outcome": True, "issues": []}   # empties: other checks handle
    ts = [fr["timestamp"] for fr in frames]
    first, last = steps[0], steps[-1]
    first_end = first["end"] if first["end"] is not None else first["start"]
    last_end = last["end"] if last["end"] is not None else last["start"]
    opening = first_end is None or any(t >= first_end for t in ts)
    outcome = last_end is None or any(t >= last_end for t in ts)
    issues = []
    if not opening:
        issues.append("opening step has no frame after it resolved — the demo may "
                      "open on a blank or previous page")
    if not outcome:
        issues.append("no frame after the last action — the demo ends before the "
                      "outcome; add a short wait (~1s) at the end of the test")
    return {"opening": opening, "outcome": outcome, "issues": issues}


# Frames embedded per step at each quality level. The screencast records ~8fps
# continuously; playing the frames inside each step's window turns the demo from
# a slideshow into actual motion (typing types, pages load). More frames = bigger
# demo.html — the quality knob is that tradeoff, and "high" is the default
# because the demo IS the product.
QUALITY_FRAMES = {"high": 14, "medium": 6, "low": 1}
DEFAULT_QUALITY = "high"


def extract_viewport(events):
    """The recorded viewport (from context-options) — needed to convert a click
    point's CSS px into a % position over the screenshot. None if absent."""
    for e in events:
        if e.get("type") == "context-options":
            vp = (e.get("options") or {}).get("viewport") or {}
            if vp.get("width") and vp.get("height"):
                return {"width": vp["width"], "height": vp["height"]}
    return None


def attach_clip_frames(steps, frames, max_per_step=14):
    """Give each step a mini-clip: the screencast frames recorded while the step
    was happening, ending on its settled frame (attach_frames' pick). Runs after
    attach_frames. Consecutive duplicate frames collapse; when a step recorded
    more than max_per_step distinct frames, keep the first + last and evenly
    sample the middle (drops the least motion). max_per_step<=1 = stills mode.

    Sets s["clip"] (sha1 list) and s["clip_dts"] (ms to hold before each frame,
    first always 0; real gaps clamped to 60..700ms so long waits don't stall
    playback and bursts stay visible)."""
    n = len(steps)
    for i, s in enumerate(steps):
        final = s.get("frame")
        if max_per_step <= 1 or not frames:
            s["clip"] = [final] if final else []
            s["clip_dts"] = [0] if final else []
            continue
        start = s["start"] if s["start"] is not None else s["end"]
        nxt = (steps[i + 1]["start"] if i + 1 < n
               and steps[i + 1].get("start") is not None else None)
        if start is None:
            window = []
        elif nxt is not None:
            window = [f for f in frames if start <= f["timestamp"] < nxt]
        else:
            window = [f for f in frames if f["timestamp"] >= start]
        shas, ts = [], []
        for f in window:
            if shas and shas[-1] == f["sha1"]:
                continue
            shas.append(f["sha1"])
            ts.append(f["timestamp"])
        if final and (not shas or shas[-1] != final):
            shas.append(final)
            ts.append((ts[-1] if ts else (start or 0)) + 120)
        if len(shas) > max_per_step:
            keep = {0, len(shas) - 1}
            mid = max_per_step - 2
            for k in range(1, mid + 1):
                keep.add(round(k * (len(shas) - 1) / (mid + 1)))
            idx = sorted(keep)
            shas = [shas[j] for j in idx]
            ts = [ts[j] for j in idx]
        dts, prev = [], None
        for t in ts:
            dts.append(0 if prev is None else int(max(60, min(700, t - prev))))
            prev = t
        s["clip"] = shas
        s["clip_dts"] = dts


# ----------------------------------------------------------------------------
# 2. Humanize selectors, actions, and assertions  (the "captions" engine)
# ----------------------------------------------------------------------------

# Trailing tokens that are UI-chrome noise, dropped from target names when other
# words remain ("save-btn" -> "save", "menu-icon" -> "menu", but "btn" stays "btn").
NAME_NOISE = {"btn", "button", "icon", "ico", "link", "el", "elem", "element",
              "wrapper", "container", "ctrl", "control", "cmp", "comp", "node"}


def humanize_name(raw):
    """Turn a machine identifier (testid, id) into readable words.

    `todo-count` -> `todo count`, `save-btn` -> `save`. De-noises a trailing chrome
    word but never empties the name. Domain-agnostic on purpose — true DOM-aware
    naming (look up an element's nearby label in the snapshot) is deferred; the
    opt-in AI pass (Phase 3) already covers marketing-grade phrasing.
    """
    words = re.sub(r"[-_]+", " ", raw).strip().split()
    if len(words) > 1 and words[-1].lower() in NAME_NOISE:
        words = words[:-1]
    return " ".join(words)


def humanize_selector(sel):
    """Turn Playwright internal selector syntax into a friendly target name."""
    if not sel:
        return "the element"
    # Pull the most descriptive token out of a possibly chained selector.
    role = re.search(r'internal:role=([a-z]+)', sel)
    name = re.search(r'name="([^"]*)"', sel)
    placeholder = re.search(r'placeholder="([^"]*)"', sel)
    testid = re.search(r'data-testid="([^"]*)"', sel)
    has_text = re.search(r'has-text="([^"]*)"', sel)
    label = re.search(r'internal:label="([^"]*)"', sel)
    text = re.search(r'internal:text="([^"]*)"', sel)

    role_word = role.group(1) if role else None
    # role at the end of a chain wins (e.g. ...>>role=checkbox)
    chain_roles = re.findall(r'internal:role=([a-z]+)', sel)
    if chain_roles:
        role_word = chain_roles[-1]

    label_text = next((m.group(1) for m in [name, has_text, label, text] if m), None)

    if placeholder:
        return f'the "{placeholder.group(1)}" field'
    if role_word and label_text:
        nice = {"checkbox": "checkbox", "link": "link", "button": "button",
                "textbox": "field", "listitem": "item"}.get(role_word, role_word)
        return f'the "{label_text}" {nice}'
    if role_word:
        return f"the {role_word}"
    if testid:
        return f"the {humanize_name(testid.group(1))}"
    if label_text:
        return f'"{label_text}"'
    if sel.startswith("#"):
        return f"the {sel[1:]} element"
    return "the element"


def short_url(u):
    """Tidy a URL for a caption. For localhost / an IP:port (a dev server), show just
    the path — "Open /signup" reads far better than "Open 127.0.0.1:53074/signup".
    Real domains keep their host."""
    raw = re.sub(r"^https?://", "", u or "").rstrip("/")
    host = raw.split("/", 1)[0]
    hostname = host.split(":", 1)[0]
    is_local = (hostname in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
                or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", hostname))
    if is_local:
        path = raw[len(host):] or "/"
        return path
    return raw


def _expected_str(params):
    """The human value out of an expect's expectedText: a literal string, or a
    regex source with re.escape's backslashes stripped ("Dylan\\ Roy" → "Dylan Roy")."""
    et = params.get("expectedText") or []
    if not (et and isinstance(et[0], dict)):
        return None
    if et[0].get("string") is not None:
        return et[0]["string"]
    rs = et[0].get("regexSource")
    return re.sub(r"\\(.)", r"\1", rs) if rs else None


def predicate(expression, params):
    exp = expression or ""
    if exp.endswith("count"):
        return f'shows {params.get("expectedNumber", "?")} item(s)'
    if exp.endswith("have.title"):
        val = _expected_str(params)
        return f'title is "{val}"' if val else "has the expected title"
    if exp.endswith("have.url"):
        val = _expected_str(params)
        return f'URL is "{val}"' if val else "has the expected URL"
    if exp.endswith("text") or exp.endswith("contain.text"):
        et = params.get("expectedText") or []
        val = et[0].get("string") if et and isinstance(et[0], dict) else None
        return f'reads "{val}"' if val else "has the expected text"
    if exp.endswith("value"):
        return "has the expected value"
    if "class" in exp:
        et = params.get("expectedText") or []
        val = et[0].get("string") if et and isinstance(et[0], dict) else ""
        if "completed" in (val or ""):
            return "is marked complete"
        return f'has class "{val}"' if val else "has the expected class"
    if exp.endswith("visible"):
        return "is visible"
    if exp.endswith("enabled"):
        return "is enabled"
    if exp.endswith("checked"):
        return "is checked"
    return exp.replace("to.", "").replace(".", " ") or "matches"


# Curated, substring-safe secret keywords. Deliberately conservative: every entry
# is unlikely to appear inside an innocent word, so masking a non-secret field is
# rare. We intentionally exclude ambiguous short tokens like "pin" (matches
# "shipping") and "token" (an API-*token-name* field is not itself a secret).
_SECRET_RE = re.compile(
    r"password|passwd|passphrase|passcode|secret|otp|totp|2fa|mfa|cvv|cvc|ssn",
    re.I)


def display_value(value, selector, name=""):
    """Mask secrets so they never leak into a shareable caption. Masks when the
    target *or its humanized name* looks like a secret field — so a field located
    semantically (get_by_label("Passcode"), aria-label) is covered, not just
    `input[name=password]`. Conservative by design (see `_SECRET_RE`).

    Residual gap (documented): a secret field located by a placeholder that carries
    no keyword (e.g. "at least 8 characters") is undetectable from the trace alone —
    locate such fields by `type=password` so this catches them."""
    if value and (_SECRET_RE.search(selector or "") or _SECRET_RE.search(name or "")):
        return "••••••••"
    return value or ""


def humanize_error(err, target=""):
    """Turn a Playwright failure into one plain-English sentence.

    A red step is only useful if the viewer can tell WHY — and the audience for a
    demo can't read a Playwright call log. Deterministic (no AI): these messages
    have stable shapes. Returns "" when there's nothing useful to say.
    """
    if not err:
        return ""
    if isinstance(err, str):
        msg, name = err, ""
    else:
        msg, name = (err.get("message") or ""), (err.get("name") or "")
    if not msg:
        return ""
    first = msg.strip().splitlines()[0]
    # humanize_selector already quotes accessible names ('the "Save" button'), so
    # don't double-quote it
    what = (target if '"' in (target or "") else f"“{target}”") if target else "that element"
    # strict mode: the locator matched several elements
    m = re.search(r"strict mode violation:.*?resolved to (\d+) elements", msg, re.S)
    if m:
        return (f"{what} matched {m.group(1)} elements on the page, so it's ambiguous — "
                "name it more specifically.")
    # the trace often stores only "Timeout 4000ms exceeded." (no call log), so key
    # off the error name too rather than requiring the "waiting for" detail
    if name == "TimeoutError" or first.startswith("Timeout "):
        secs = re.search(r"Timeout (\d+)ms", first)
        wait = f" within {int(secs.group(1)) / 1000:g}s" if secs else ""
        if re.search(r"waiting for .*to be visible", msg):
            return f"{what} never became visible{wait} — it may be hidden or behind a click."
        return (f"Couldn't find {what} on the page{wait} — the name may have changed, "
                "or it only appears after another step.")
    if "net::ERR_" in msg or "NS_ERROR" in msg:
        code = re.search(r"(net::ERR_[A-Z_]+)", msg)
        return (f"The page didn't load ({code.group(1) if code else 'network error'}) — "
                "check the URL is reachable.")
    if re.search(r"expect.*to(Be|Have)", first) or "Expected" in msg:
        return f"The check on {what} didn't hold — the page didn't end up as expected."
    return first[:160]


def humanize(step):
    """Return (caption, kind) where kind in {nav, action, check}."""
    m, p = step["method"], step["params"]
    if m == "goto":
        return f"Open {short_url(p.get('url'))}", "nav"
    if m == "submit":
        tgt = humanize_selector(p.get("selector"))
        val = display_value(p.get("value"), p.get("selector"), tgt)
        return f'Submit "{val}" in {tgt}', "action"
    if m == "fill":
        tgt = humanize_selector(p.get("selector"))
        val = display_value(p.get("value"), p.get("selector"), tgt)
        return f'Type "{val}" into {tgt}', "action"
    if m in ("type", "pressSequentially"):   # keystroke-typed text (press_sequentially)
        tgt = humanize_selector(p.get("selector"))
        val = display_value(p.get("text"), p.get("selector"), tgt)
        return f'Type "{val}" into {tgt}', "action"
    if m in ("press", "keyboardPress"):
        # page.keyboard.press lands as "keyboardPress" — same idea as a locator
        # press, and a common way to scroll ("End") or submit ("Enter")
        key = p.get("key", "a key")
        friendly = {"End": "Jump to the bottom of the page",
                    "Home": "Jump to the top of the page",
                    "Escape": "Dismiss with Escape", "Enter": "Press Enter"}
        return friendly.get(key, f"Press {key}"), "action"
    if m in ("mouseWheel", "wheel"):
        return "Scroll the page", "action"
    if m == "click":
        return f"Click {humanize_selector(p.get('selector'))}", "action"
    if m == "check":
        return f"Check {humanize_selector(p.get('selector'))}", "action"
    if m == "uncheck":
        return f"Uncheck {humanize_selector(p.get('selector'))}", "action"
    if m == "selectOption":
        return f"Select an option in {humanize_selector(p.get('selector'))}", "action"
    if m == "setInputFiles":
        return f"Upload a file to {humanize_selector(p.get('selector'))}", "action"
    if m == "expect":
        exp = p.get("expression") or ""
        if exp in ("to.have.title", "to.have.url"):     # page-level, no element target
            return f"Confirm the page {predicate(exp, p)}", "check"
        tgt = humanize_selector(p.get("selector"))
        return f"Confirm {tgt} {predicate(exp, p)}", "check"
    if m in ("waitForSelector", "waitFor"):
        return f"Wait for {humanize_selector(p.get('selector'))}", "action"
    if m == "waitForURL":
        hint = json.dumps(p.get("url") or p.get("glob") or "")
        if re.search(r"persona-reaction|reaction|results", hint, re.I):
            return "Wait for the results", "action"
        if re.search(r"login|sign[-_]?in", hint, re.I):
            return "Wait for sign-in to finish", "action"
        return "Wait for the page to load", "action"
    if m == "waitForFunction":
        expr = str(p.get("expression") or p.get("function") or "")
        if re.search(r"google|reply|message|innerText|textContent|response", expr, re.I):
            return "Wait for the response", "action"
        return "Wait for the page to update", "action"
    return m, "action"


# ----------------------------------------------------------------------------
# 2b. Optional AI narration (Phase 3) — opt-in, BYO-key, never the default.
#     The deterministic captions above stay the source of truth; this only adds
#     a friendlier `narration` line on top when --ai is set and a key is present.
# ----------------------------------------------------------------------------

# Default per the Anthropic model guidance. Cheaper options (e.g. claude-haiku-4-5)
# work too and cut the per-flow cost — override with --ai-model or `ai_model:` in
# specreel.yml. One batched request rewrites a whole flow (~$0.01 or less/flow).
DEFAULT_AI_MODEL = "claude-opus-4-8"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

NARRATION_SYSTEM = (
    "You rewrite literal UI-test step captions into short, friendly product-demo "
    "narration — the kind a sales engineer would say while clicking through the app.\n"
    "Rules:\n"
    "- One line per input step, in the SAME order. Return exactly as many lines as given.\n"
    "- Present tense, plain English, <= 12 words, no trailing period.\n"
    "- Describe ONLY what the step does; never invent actions, data, or outcomes.\n"
    "- For checks (kind=check), phrase as a confirmation (e.g. \"the dashboard loads\").\n"
    "- For steps that open a raw URL (localhost, an IP, a bare host/path), refer to "
    "the product by its `product` name (or just \"the app\" if none is given) — never "
    "read out a localhost URL or port.\n"
    "- No surrounding quotes, no step numbers, no markdown."
)

NARRATION_SCHEMA = {
    "type": "object",
    "properties": {"narrations": {"type": "array", "items": {"type": "string"}}},
    "required": ["narrations"],
    "additionalProperties": False,
}


def resolve_api_key(explicit=None):
    return explicit or os.environ.get("ANTHROPIC_API_KEY") or ""


def build_narration_request(rendered, title, model, product=""):
    """Assemble the Messages API request body for a one-shot batched rewrite."""
    steps = [{"i": i + 1, "kind": r["kind"], "caption": r["caption"]}
             for i, r in enumerate(rendered)]
    payload = {"title": title, "steps": steps}
    if product:
        payload["product"] = product
    user = json.dumps(payload, ensure_ascii=False)
    return {
        "model": model,
        "max_tokens": 1024,
        # system as a cache_control block — prompt caching kicks in when a gallery
        # rewrites many flows behind the same system prefix (no-op if below the
        # model's min cacheable size; harmless to mark).
        "system": [{"type": "text", "text": NARRATION_SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        # structured outputs guarantee valid JSON back — no brittle parsing.
        "output_config": {"format": {"type": "json_schema", "schema": NARRATION_SCHEMA}},
        # NB: no temperature/top_p/budget_tokens — removed on current models (would 400).
        # thinking is omitted (off by default) — this is a simple, schema-bounded rewrite.
    }


def _anthropic_messages(body, api_key, timeout=60, retries=1):
    """One POST to the Messages API, with a single retry on transient failures
    (read timeout, connection reset, 429/5xx). A generation that dies on a blip
    surfaces to the user as 'scenario failed to resolve' with no way to tell it
    was just network weather — one retry removes most of that noise."""
    import socket
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        ANTHROPIC_URL, data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < retries:
                last = e
                import time as _t
                _t.sleep(2 * (attempt + 1))
                continue
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError) as e:
            if attempt < retries:
                last = e
                continue
            raise
    raise last


def parse_narrations(response):
    """Pull the narration list out of a Messages API response (structured output
    guarantees the first text block is valid JSON matching NARRATION_SCHEMA)."""
    text = next((b.get("text", "") for b in response.get("content", [])
                 if b.get("type") == "text"), "")
    obj = json.loads(text) if text else {}
    return obj.get("narrations") or []


def apply_narration(rendered, narrations):
    """Attach narration to each step IFF the count matches (else keep literal).
    Returns True when applied. Pure — unit-testable without the network."""
    if len(narrations) != len(rendered) or not rendered:
        return False
    for r, n in zip(rendered, narrations):
        if isinstance(n, str) and n.strip():
            r["narration"] = n.strip()
    return True


def narrate(rendered, title, api_key, model=DEFAULT_AI_MODEL, timeout=60, product=""):
    """Rewrite captions into narration in place. Best-effort: any failure leaves
    the deterministic captions untouched and returns False (graceful degrade)."""
    try:
        body = build_narration_request(rendered, title, model, product=product)
        resp = _anthropic_messages(body, api_key, timeout=timeout)
        ok = apply_narration(rendered, parse_narrations(resp))
        if not ok:
            sys.stderr.write("specreel: AI narration count mismatch — kept literal captions\n")
        return ok
    except Exception as e:
        sys.stderr.write(f"specreel: AI narration failed ({type(e).__name__}: {e}) "
                         f"— kept literal captions\n")
        return False


# ---- Studio voiceover (opt-in, BYO-key): pre-rendered neural TTS audio --------
# The browser Web Speech voice is the free default; this upgrades to studio-grade
# narration by synthesizing an audio clip per step and embedding it in the player.
# Uses OpenAI's TTS API (one POST per step, returns mp3 bytes) — BYO OPENAI_API_KEY.
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"   # also: tts-1 (cheap), tts-1-hd (higher fidelity)
DEFAULT_TTS_VOICE = "nova"              # alloy/echo/fable/onyx/nova/shimmer/sage/coral…
# gpt-4o-mini-tts accepts free-text delivery notes; tts-1/tts-1-hd reject the field
DEFAULT_TTS_INSTRUCTIONS = ("Calm, friendly product-demo narrator. Moderate pace, "
                            "natural emphasis; read UI names as plain words.")
_TTS_RETRY_SLEEP = 0.8


def resolve_tts_key(explicit=None):
    return explicit or os.environ.get("OPENAI_API_KEY") or ""


def tts_cache_dir():
    """Where synthesized clips are cached across builds. CI re-renders demos every
    build; a step whose words didn't change shouldn't re-bill. Override with
    SPECREEL_TTS_CACHE=<dir>; disable with SPECREEL_TTS_CACHE=off."""
    env = os.environ.get("SPECREEL_TTS_CACHE", "")
    if env.lower() in ("0", "off", "none", "no"):
        return None
    return env or os.path.join(os.path.expanduser("~"), ".cache", "specreel", "tts")


def speakable(step):
    """The text a step's narration should SAY: failure wording included (a listener
    can't see the red caption bar), masked secrets (•••) and url schemes translated
    for the ear."""
    t = (step.get("narration") or step.get("caption") or "").strip()
    if not t:
        return ""
    if step.get("failed"):
        why = (step.get("why") or "").strip()
        t = "This step failed: " + t + (". " + why if why else "")
    t = re.sub(r"•+", "the hidden value", t)
    t = re.sub(r"https?://", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _openai_tts(text, key, voice, model, fmt="mp3", timeout=30, instructions=""):
    """One TTS call → audio bytes. Isolated for mockability."""
    import urllib.request
    payload = {"model": model, "voice": voice, "input": text, "response_format": fmt}
    if instructions:
        payload["instructions"] = instructions
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(OPENAI_TTS_URL, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def synthesize_voiceover(rendered, key, voice=DEFAULT_TTS_VOICE, model=DEFAULT_TTS_MODEL,
                         timeout=30, verbose=False, instructions=None, workers=4):
    """Attach a base64 audio clip to each step (step['audio']) by synthesizing its
    narration/caption. Clips are disk-cached by (model, voice, instructions, text)
    and synthesis fans out over a few threads with one retry per clip. Best-effort
    and per-step graceful — a failed clip just falls back to the browser voice for
    that step. Returns the number of clips attached."""
    if not key:
        return 0
    if instructions is None:
        instructions = DEFAULT_TTS_INSTRUCTIONS if model.startswith("gpt-") else ""
    jobs = [(r, speakable(r)) for r in rendered]
    jobs = [(r, t) for r, t in jobs if t]
    if not jobs:
        return 0
    cache = tts_cache_dir()

    def clip(text):
        path = None
        if cache:
            h = hashlib.sha1("|".join((model, voice, instructions, text))
                             .encode("utf-8")).hexdigest()
            path = os.path.join(cache, h + ".mp3")
            try:
                with open(path, "rb") as f:
                    return f.read(), True
            except OSError:
                pass
        for attempt in (1, 2):          # one retry — a blip shouldn't mute a step
            try:
                audio = _openai_tts(text, key, voice, model, timeout=timeout,
                                    instructions=instructions)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(_TTS_RETRY_SLEEP)
        if path:
            try:
                os.makedirs(cache, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(audio)
            except OSError:
                pass
        return audio, False

    from concurrent.futures import ThreadPoolExecutor
    made = hits = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        futs = [(r, ex.submit(clip, t)) for r, t in jobs]
        for r, fut in futs:
            try:
                audio, hit = fut.result()
            except Exception as e:
                sys.stderr.write(f"specreel: voiceover clip failed ({type(e).__name__}: {e}) "
                                 f"— that step uses the browser voice\n")
                continue
            r["audio"] = "data:audio/mpeg;base64," + base64.b64encode(audio).decode()
            made += 1
            hits += 1 if hit else 0
    if verbose and made:
        print(f"  voiceover: {made} clips via {model}/{voice}"
              + (f" ({hits} cached)" if hits else ""))
    return made


# ----------------------------------------------------------------------------
# 3. Emit HTML (self-contained, base64 frames) and optional MP4
# ----------------------------------------------------------------------------

# Theme palettes — one superset of CSS vars per theme, injected as the :root block
# of every template. Dark is the default; light is a clean paper variant.
THEME_VARS = {
    "dark": ("--bg:#0c0d0f;--panel:#131517;--panel-2:#16191c;--line:#23272b;"
             "--line-soft:#1b1e21;--text:#e8eaed;--muted:#8b9298;--faint:#5b6166;"
             "--green:#5ff19b;--green-dim:#1d3b2a;--amber:#ffc46b;--red:#ff7a7a;"
             "--blue:#7fb6ff;--ink:#06140c;"),
    "light": ("--bg:#f6f5f1;--panel:#ffffff;--panel-2:#f1f0ea;--line:#e3e1d9;"
              "--line-soft:#edece5;--text:#1c1d1f;--muted:#5d6166;--faint:#9aa0a6;"
              "--green:#1f9d57;--green-dim:#dcf2e4;--amber:#a8701a;--red:#cc4444;"
              "--blue:#2f6fdc;--ink:#ffffff;"),
}
FONT_VARS = ("--mono:'JetBrains Mono',ui-monospace,monospace;"
             "--serif:'Instrument Serif',Georgia,serif;")


def theme_block(theme):
    return ":root{" + THEME_VARS.get(theme, THEME_VARS["dark"]) + FONT_VARS + "}"


def b64_img(path):
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def render_steps(steps, trace_dir, viewport=None, final_url=""):
    out = []
    cur_url = ""
    vw = (viewport or {}).get("width") or 0
    vh = (viewport or {}).get("height") or 0
    b64_cache = {}

    def load(sha):
        if sha not in b64_cache:
            fp = os.path.join(trace_dir, "resources", sha)
            b64_cache[sha] = b64_img(fp) if os.path.exists(fp) else None
        return b64_cache[sha]

    for i, s in enumerate(steps):
        cap, kind = humanize(s)
        # the snapshot URL is authoritative (post-navigation); fall back to a
        # goto's target, else carry the previous step's URL forward
        if s.get("url"):
            cur_url = s["url"]
        elif s.get("method") == "goto" and s.get("params", {}).get("url"):
            cur_url = s["params"]["url"]
        imgs = [b for b in (load(sha) for sha in (s.get("clip") or [])) if b]
        img = imgs[-1] if imgs else (load(s["frame"]) if s.get("frame") else None)
        dts = (s.get("clip_dts") or [])[:len(imgs)] if imgs else []
        # click position as % of the viewport — the screencast frame matches the
        # viewport aspect, so % maps onto the displayed image regardless of scale
        point = s.get("point") or {}
        px = round(point["x"] / vw * 100, 2) if point and vw else None
        py = round(point["y"] / vh * 100, 2) if point and vh else None
        why = (humanize_error(s.get("error"),
                              humanize_selector(s.get("params", {}).get("selector")))
               if s.get("error") else "")
        dur = max(1.2, min(4.0, ((s["end"] or 0) - (s["start"] or 0)) / 1000.0 + 0.8))
        # the last step pins to the trace's final frame (the settled end state),
        # so show the URL the flow actually ended on — a click that navigates
        # would otherwise label its destination with the page it left
        shown_url = final_url if (i == len(steps) - 1 and final_url) else cur_url
        out.append({"i": i + 1, "caption": cap, "kind": kind,
                    "img": img, "imgs": imgs, "dts": dts,
                    "px": px, "py": py,
                    "url": short_url(shown_url) if shown_url else "",
                    "dur": round(dur, 2),
                    "failed": bool(s.get("error")), "why": why})
    return out


def step_payload(rendered, motion=True):
    """The per-step data the players consume — narration as the headline, the
    literal caption as a sub-line. Shared by the single demo and the bundle.
    motion=False (the bundle, an email-sized artifact) keeps one frame per step;
    motion=True adds each step's clip frames + timings for real playback."""
    out = []
    for r in rendered:
        d = {"caption": r.get("narration") or r["caption"],
             "lit": r["caption"] if r.get("narration") else "",
             "kind": r["kind"], "img": r["img"] or "",
             "dur": r["dur"], "failed": r["failed"],
             "audio": r.get("audio") or "",
             "url": r.get("url") or "",
             "why": r.get("why") or ""}   # plain-English reason a step failed
        if r.get("px") is not None:
            d["px"], d["py"] = r["px"], r["py"]
        if motion and len(r.get("imgs") or []) > 1:
            d["imgs"], d["dts"] = r["imgs"], r["dts"]
        out.append(d)
    return out


def og_meta(title, desc, image=""):
    """Open Graph + Twitter card tags so a shared gallery link unfurls with a real
    title/description in Slack, iMessage, Twitter, etc. (the 'send a demo' moment)."""
    t, d = html.escape(title or "Specreel demos"), html.escape(desc or "")
    tags = [f'<meta property="og:title" content="{t}">',
            f'<meta property="og:description" content="{d}">',
            '<meta property="og:type" content="website">',
            f'<meta name="description" content="{d}">']
    if image:
        tags += [f'<meta property="og:image" content="{html.escape(image)}">',
                 '<meta name="twitter:card" content="summary_large_image">']
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    return "\n".join(tags)


def _script_json(obj):
    """JSON safe to embed inside a <script> element. json.dumps doesn't escape
    '/', so a trace-derived string containing '</script>' would terminate the
    script tag at HTML-parse time and inject markup. '<\\/' is an equivalent
    escape inside JSON strings (and '<' only occurs inside strings in JSON)."""
    return json.dumps(obj).replace("</", "<\\/")


def build_html(rendered, title, out_dir, theme="dark", analytics=""):
    steps_json = _script_json(step_payload(rendered))
    n_actions = sum(1 for r in rendered if r["kind"] != "check")
    n_checks = sum(1 for r in rendered if r["kind"] == "check")
    tpl = HTML_TEMPLATE.replace("__THEMEVARS__", theme_block(theme)) \
        .replace("__ANALYTICS__", analytics) \
        .replace("__OG__", og_meta(title, "A demo flow generated from a test — always current.")) \
        .replace("__TITLE__", html.escape(title)) \
        .replace("__NACT__", str(n_actions)).replace("__NCHK__", str(n_checks)) \
        .replace("__DATE__", time.strftime("%b %d, %Y")) \
        .replace("__TTSJS__", TTS_JS) \
        .replace("__STEPS__", steps_json)
    path = os.path.join(out_dir, "demo.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(tpl)
    return path


def build_index(entries, out_dir, ctx=None, gallery_title="", theme="dark", analytics=""):
    """Gallery page linking every demo. Each card carries a 'demo' + 'test' pill
    from the one source — the product thesis (same artifact) made visible — plus
    a real end-state thumbnail, duration, and freshness tied to the build."""
    ctx = ctx or {}
    build = ctx.get("build", "")
    cards = []
    for e in entries:
        healed = e.get("healed") and not e["failed"]
        cls = "card fail" if e["failed"] else ("card heal" if healed else "card")
        if e["failed"]:
            status = '<span class="pill test fail">✕ test failed</span>'
        elif healed:
            status = '<span class="pill test heal">⟳ updated</span>'
        else:
            status = '<span class="pill test">✓ test</span>'
        if e["failed"]:
            fresh = '<span class="stale">● re-rendered</span>'
        elif healed:
            fresh = f'<span class="stale">● updated this build{" · " + html.escape(build) if build else ""}</span>'
        else:
            fresh = f'<span class="fresh">● fresh{" · " + html.escape(build) if build else ""}</span>'
        thumb = (f'<div class="thumb" style="background-image:url(\'{e["thumb"]}\')"></div>'
                 if e.get("thumb") else '<div class="thumb empty"></div>')
        src = '<span class="src">public</span>' if e.get("public") else ""
        dstatus = "fail" if e["failed"] else ("heal" if healed else "fresh")
        cards.append(
            f'<a class="{cls}" data-status="{dstatus}" '
            f'data-title="{html.escape(e["title"]).lower()}" '
            f'href="{html.escape(e["slug"])}/demo.html">'
            f'{thumb}'
            f'<div class="cbody">'
            f'<div class="ctop"><span class="ct">{html.escape(e["title"])}{src}</span>'
            f'<span class="badges"><span class="pill demo">▶ demo</span>{status}</span></div>'
            f'<div class="cmeta">{fresh}<span class="dur">▶ {fmt_duration(e.get("duration", 0))}</span></div>'
            f'<div class="cm">{e["n_steps"]} steps · {e["n_actions"]} actions · '
            f'{e["n_checks"]} checks</div>'
            f'</div></a>'
        )
    n = len(entries)
    n_fail = sum(1 for e in entries if e["failed"])
    n_fresh = n - n_fail
    total_dur = fmt_duration(sum(e.get("duration", 0) for e in entries))

    repo = html.escape(ctx.get("repo") or "")
    branch = html.escape(ctx.get("branch") or "")
    bits = []
    if repo:
        bits.append(f'<span>{repo}</span>')
    if branch:
        bits.append(f'<span class="branch">⌥ {branch}</span>')
    if build:
        state = "failing" if n_fail else "passing"
        cls_b = "build fail" if n_fail else "build"
        bits.append(f'<span class="{cls_b}"><span class="pulse"></span>'
                    f'build {html.escape(build)} {state}</span>')
    repobar = "".join(bits) or '<span class="branch">local run</span>'

    kicker = (f'<div class="kicker">{html.escape(gallery_title)}</div>'
              if gallery_title else "")
    og_desc = (f"{n} flow{'s' if n != 1 else ''}"
               + (f" · {n_fail} failing" if n_fail else " · all fresh")
               + " — demos generated from the tests, always current.")
    tpl = (INDEX_TEMPLATE
           .replace("__THEMEVARS__", theme_block(theme))
           .replace("__ANALYTICS__", analytics)
           .replace("__OG__", og_meta(gallery_title or "Specreel demos", og_desc))
           .replace("__CARDS__", "\n".join(cards))
           .replace("__REPOBAR__", repobar)
           .replace("__KICKER__", kicker)
           .replace("__NFLOWS__", str(n))
           .replace("__NFRESH__", str(n_fresh))
           .replace("__NFAIL__", str(n_fail))
           .replace("__FAILCLASS__", "amber" if n_fail else "green")
           .replace("__TOTALDUR__", total_dur))
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(tpl)
    return path


def _load_font(bold, size):
    """Find a usable sans-serif font across macOS/Linux/Windows, else fall back
    to Pillow's bundled default so MP4 export never hard-crashes on font setup."""
    from PIL import ImageFont
    candidates = [
        # Linux (DejaVu)
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial{}.ttf".format(" Bold" if bold else ""),
        "/Library/Fonts/Arial{}.ttf".format(" Bold" if bold else ""),
        # Windows
        "C:\\Windows\\Fonts\\{}".format("arialbd.ttf" if bold else "arial.ttf"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _card(W, H, kick, headline, sub, accent=(95, 241, 155)):
    """A title/outro card frame — what turns the export from a screen recording
    into something that looks produced."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (W, H), (12, 13, 15))
    d = ImageDraw.Draw(im)
    f_k = _load_font(bold=True, size=22)
    f_h = _load_font(bold=True, size=54)
    f_s = _load_font(bold=False, size=26)
    y = H // 2 - 90
    d.text((80, y), kick, font=f_k, fill=accent)
    d.text((80, y + 44), headline[:60], font=f_h, fill=(232, 234, 237))
    d.text((80, y + 122), sub, font=f_s, fill=(139, 146, 152))
    d.rectangle([80, y + 178, 200, y + 182], fill=accent)
    return im


def _caption_frames(rendered, frames_dir, W=1280, motion=True):
    """Composite every playable frame (each step's clip, or its single still)
    with the caption bar. Returns (items, spans): items = [(path, seconds)] in
    playback order; spans = [(step, first, last)] mapping each step that produced
    frames to its slice of items (so audio can be laid onto the same timeline)."""
    from PIL import Image, ImageDraw
    f_cap = _load_font(bold=True, size=30)
    f_lbl = _load_font(bold=False, size=20)
    out = []
    spans = []
    idx = 0
    for r in rendered:
        imgs = (r.get("imgs") or []) if motion else []
        if not imgs:
            imgs = [r["img"]] if r.get("img") else []
        if not imgs:
            continue
        first = idx
        dts = (r.get("dts") or []) if motion else []
        # hold each clip frame for its recorded gap; the final frame of a step
        # holds for the remainder of the step's display duration (the beat that
        # lets a viewer actually read the caption)
        for j, b64 in enumerate(imgs):
            raw = base64.b64decode(b64.split(",", 1)[1])
            tmp = os.path.join(frames_dir, f"raw_{idx}.jpg")
            with open(tmp, "wb") as fh:
                fh.write(raw)
            im = Image.open(tmp).convert("RGB")
            if im.width != W:
                im = im.resize((W, int(im.height * W / im.width)))
            bar_h = 96
            canvas = Image.new("RGB", (im.width, im.height + bar_h), (12, 13, 15))
            canvas.paste(im, (0, 0))
            d = ImageDraw.Draw(canvas)
            accent = (95, 241, 155) if r["kind"] != "check" else (127, 182, 255)
            if r["failed"]:
                accent = (255, 122, 122)
            tag = "CHECK" if r["kind"] == "check" else ("NAV" if r["kind"] == "nav" else "STEP")
            d.text((28, im.height + 20), f"{tag} {r['i']}", font=f_lbl, fill=accent)
            d.text((28, im.height + 48), r.get("narration") or r["caption"],
                   font=f_cap, fill=(232, 234, 237))
            d.rectangle([0, im.height, 6, im.height + bar_h], fill=accent)
            outp = os.path.join(frames_dir, f"f_{idx:04d}.png")
            canvas.save(outp)
            last = (j == len(imgs) - 1)
            if last:
                held = sum(dts[1:len(imgs)]) / 1000.0 if len(dts) > 1 else 0
                dur = max(0.9, r["dur"] - held)
            else:
                dur = max(0.08, (dts[j + 1] if j + 1 < len(dts) else 120) / 1000.0)
            out.append((outp, round(dur, 3)))
            idx += 1
        spans.append((r, first, idx - 1))
    return out, spans


# MP4 timeline bookends (title card in, outcome card out) — the audio track is
# planned against the same constants so narration stays step-aligned.
_MP4_LEAD, _MP4_TAIL = 2.0, 2.6


def plan_voiceover(step_durs, clip_durs, lead=_MP4_LEAD, tail=_MP4_TAIL, breath=0.35):
    """Fit narration clips onto the MP4 timeline. step_durs[k] = a step's video
    seconds; clip_durs[k] = its narration clip's seconds (None = no clip). A clip
    that outruns its step extends the step's last-frame hold rather than being cut
    mid-sentence. Returns (extend, segments): extend[k] = extra hold seconds for
    step k; segments = [('silence', dur) | ('clip', k, dur)] covering
    lead + every step + tail exactly. Pure and unit-tested."""
    extend, segments = [], [("silence", round(lead, 3))]
    for k, (sd, cd) in enumerate(zip(step_durs, clip_durs)):
        ext = round(max(0.0, cd + breath - sd), 3) if cd is not None else 0.0
        extend.append(ext)
        dur = round(sd + ext, 3)
        segments.append(("clip", k, dur) if cd is not None else ("silence", dur))
    segments.append(("silence", round(tail, 3)))
    return extend, segments


def _probe_duration(path):
    """Seconds of audio in a file, via ffprobe (ships alongside ffmpeg). None if
    ffprobe is missing or the file is unreadable."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        return float(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError):
        return None


def _voiceover_track(spans, items, frames_dir):
    """Decode each step's narration clip, measure it, and lay the clips onto the
    MP4 timeline. May lengthen a step's last-frame hold in `items` in place (a
    clip must not be cut mid-sentence). Returns (clip_paths, audio_filtergraph)
    for ffmpeg, or None when there's no usable audio (no clips, no ffprobe,
    undecodable data) — the MP4 then ships silent exactly as before."""
    step_durs, clip_durs, paths = [], [], []
    for n, (r, a, b) in enumerate(spans):
        step_durs.append(round(sum(items[j][1] for j in range(a, b + 1)), 3))
        data = r.get("audio") or ""
        if data.startswith("data:audio"):
            p = os.path.join(frames_dir, f"vo_{n}.mp3")
            try:
                with open(p, "wb") as fh:
                    fh.write(base64.b64decode(data.split(",", 1)[1]))
            except (ValueError, OSError):
                return None
            d = _probe_duration(p)
            if d is None:
                return None
            paths.append(p)
            clip_durs.append(round(d, 3))
        else:
            paths.append(None)
            clip_durs.append(None)
    if not any(paths):
        return None
    extend, segments = plan_voiceover(step_durs, clip_durs)
    for n, ext in enumerate(extend):
        if ext:
            last = spans[n][2]
            p, dur = items[last]
            items[last] = (p, round(dur + ext, 3))
    # one filtergraph: each clip padded/trimmed to exactly its step's screen time,
    # silence elsewhere, all concatenated — no mixing, so alignment is exact
    inputs, chains, labels = [], [], []
    for k, seg in enumerate(segments):
        if seg[0] == "clip":
            inputs.append(paths[seg[1]])
            chains.append(f"[{len(inputs)}:a]aresample=24000,"
                          "aformat=sample_fmts=fltp:channel_layouts=mono,"
                          f"apad=whole_dur={seg[2]},atrim=0:{seg[2]}[a{k}]")
        else:
            chains.append(f"anullsrc=r=24000:cl=mono:d={seg[1]},"
                          f"aformat=sample_fmts=fltp:channel_layouts=mono[a{k}]")
        labels.append(f"[a{k}]")
    chains.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[voix]")
    return inputs, ";".join(chains)


def _write_concat(items, path):
    with open(path, "w", encoding="utf-8") as fh:
        for p, dur in items:
            fh.write(f"file '{os.path.abspath(p)}'\nduration {dur}\n")
        if items:
            fh.write(f"file '{os.path.abspath(items[-1][0])}'\n")   # hold last


def build_mp4(rendered, title, out_dir, trace_dir, failed=False):
    """Motion MP4: every captured frame, bookended by a title and outcome card.
    Steps carrying studio voiceover clips get them muxed in as a real audio track
    (a narrated MP4 is the shareable artifact); without clips the file is silent."""
    from PIL import Image
    frames_dir = os.path.join(out_dir, "_frames")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        items, spans = _caption_frames(rendered, frames_dir)
        if not items:
            return None
        vo = None
        if any((r.get("audio") or "").startswith("data:audio") for r in rendered):
            vo = _voiceover_track(spans, items, frames_dir)
            if vo is None:
                sys.stderr.write("specreel: voiceover clips present but ffprobe/"
                                 "decoding unavailable — writing a silent MP4\n")
        W, H = Image.open(items[0][0]).size
        n_act = sum(1 for r in rendered if r["kind"] != "check")
        n_chk = sum(1 for r in rendered if r["kind"] == "check")
        intro = os.path.join(frames_dir, "a_intro.png")
        _card(W, H, "DEMO · GENERATED FROM A REAL TEST", title,
              f"{n_act} actions · {n_chk} checks").save(intro)
        outro = os.path.join(frames_dir, "z_outro.png")
        accent = (255, 122, 122) if failed else (95, 241, 155)
        _card(W, H, "SPECREEL",
              "This flow is failing" if failed else "Flow verified",
              ("A step failed on the last run" if failed
               else "Generated from a passing test") + " · " + time.strftime("%b %d, %Y"),
              accent=accent).save(outro)
        items = [(intro, _MP4_LEAD)] + items + [(outro, _MP4_TAIL)]
        listfile = os.path.join(frames_dir, "list.txt")
        _write_concat(items, listfile)
        mp4 = os.path.join(out_dir, "demo.mp4")
        if vo:
            clip_paths, afilter = vo
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile]
            for p in clip_paths:
                cmd += ["-i", p]
            cmd += ["-filter_complex",
                    "[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30[v];" + afilter,
                    "-map", "[v]", "-map", "[voix]",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "medium",
                    "-c:a", "aac", "-b:a", "128k", mp4]
        else:
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
                   "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "medium", mp4]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write(r.stderr[-800:])
            return None
        return mp4
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


def build_gif(rendered, title, out_dir, trace_dir, failed=False, width=900):
    """Looping GIF — the artifact devs actually paste into a README or a PR.
    Palette-generated for decent color at a sane size; no title cards (a GIF
    should start on content), and capped so it stays embeddable."""
    frames_dir = os.path.join(out_dir, "_gif")
    os.makedirs(frames_dir, exist_ok=True)
    try:
        items, _ = _caption_frames(rendered, frames_dir, W=width)
        if not items:
            return None
        listfile = os.path.join(frames_dir, "list.txt")
        _write_concat(items, listfile)
        gif = os.path.join(out_dir, "demo.gif")
        pal = os.path.join(frames_dir, "pal.png")
        r1 = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                             "-vf", "fps=10,palettegen=stats_mode=diff", pal],
                            capture_output=True, text=True)
        if r1.returncode != 0:
            sys.stderr.write(r1.stderr[-500:])
            return None
        r2 = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                             "-i", pal, "-lavfi",
                             "fps=10[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
                             "-loop", "0", gif], capture_output=True, text=True)
        if r2.returncode != 0:
            sys.stderr.write(r2.stderr[-500:])
            return None
        return gif
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


# ----------------------------------------------------------------------------
# 4. Drivers: one trace -> one demo, or a directory of traces -> a gallery
# ----------------------------------------------------------------------------

def fmt_duration(secs):
    secs = int(round(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def generate_demo(trace_path, out_dir, title=None, want_mp4=False, verbose=True,
                  setup_urls=None, ai=False, api_key=None, ai_model=DEFAULT_AI_MODEL,
                  product="", collect=False, theme="dark", analytics="",
                  voice=None, tts_model=DEFAULT_TTS_MODEL, tts_key=None,
                  tts_instructions=None, quality=DEFAULT_QUALITY, want_gif=False):
    """Render a single trace.zip into out_dir/demo.html (+ optional demo.mp4).

    Returns a stats dict: title, html, mp4, n_actions, n_checks, n_steps, failed,
    duration (seconds), thumb (base64 end-state frame for gallery cards).
    """
    title = title or os.path.splitext(os.path.basename(trace_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="specreel_")
    try:
        with zipfile.ZipFile(trace_path) as z:
            z.extractall(tmp)
        events = load_events(tmp)
        steps, frames = build_steps(events)
        steps = coalesce_steps(steps)
        # Setup trim (login / auth redirect) can wipe every step when the run
        # dies on the sign-in page — remember that so we don't report a green
        # empty demo as "ok" / "passing".
        n_before_trim = len(steps)
        had_error_before_trim = any(s.get("error") for s in steps)
        steps = trim_setup_steps(steps, setup_urls)
        attach_frames(steps, frames)
        attach_clip_frames(steps, frames,
                           QUALITY_FRAMES.get(quality, QUALITY_FRAMES[DEFAULT_QUALITY]))
        coverage = capture_coverage(steps, frames)
        rendered = render_steps(steps, tmp, viewport=extract_viewport(events),
                                final_url=final_snapshot_url(events))

        # Phase 3 (opt-in): rewrite captions into narration. Best-effort — the
        # deterministic captions remain the source of truth on any failure.
        if ai and api_key and narrate(rendered, title, api_key, model=ai_model,
                                       product=product) and verbose:
            print(f"  AI: narrated via {ai_model}")

        # Studio voiceover (opt-in, BYO-key): pre-render neural TTS per step.
        if voice:
            synthesize_voiceover(rendered, resolve_tts_key(tts_key), voice=voice,
                                 model=tts_model, verbose=verbose,
                                 instructions=tts_instructions)

        html_path = build_html(rendered, title, out_dir, theme=theme, analytics=analytics)
        n_act = sum(1 for r in rendered if r["kind"] != "check")
        n_chk = sum(1 for r in rendered if r["kind"] == "check")
        failed = any(r["failed"] for r in rendered)
        # A blank demo is never a pass: either the trace had nothing, or setup
        # trim removed a failed login / auth-wall run (hosted false-success).
        if not rendered:
            failed = True
            if verbose and (n_before_trim or had_error_before_trim):
                print("  ⚠ empty after setup trim — marking failed "
                      f"(had {n_before_trim} step(s) before trim)")
        # demo playback length (what the viewer experiences on ▶), not test runtime
        duration = sum(r["dur"] for r in rendered)
        thumb = next((r["img"] for r in reversed(rendered) if r.get("img")), "")
        if verbose:
            print(f"  {len(rendered)} steps  ({n_act} actions, {n_chk} checks)  "
                  f"<- {len(frames)} frames")
            print(f"  HTML: {html_path}")
            for r in rendered:
                mark = "x" if r["failed"] else ("?" if r["kind"] == "check" else ">")
                print(f"    {mark} {r['caption']}")
        mp4_path = None
        if want_mp4:
            mp4_path = build_mp4(rendered, title, out_dir, tmp, failed=failed)
            if mp4_path and verbose:
                print(f"  MP4:  {mp4_path}")
        if want_gif:
            gif_path = build_gif(rendered, title, out_dir, tmp, failed=failed)
            if gif_path and verbose:
                print(f"  GIF:  {gif_path}")
        # stable signature of the flow (literal captions, not AI narration) — lets
        # the gallery detect "this flow changed since the last build".
        sig = hashlib.sha1("\n".join(r["caption"] for r in rendered).encode("utf-8")).hexdigest()[:12]
        stats = {"title": title, "html": html_path, "mp4": mp4_path,
                 "n_actions": n_act, "n_checks": n_chk, "n_steps": len(rendered),
                 "failed": failed, "duration": duration, "thumb": thumb, "sig": sig,
                 "capture": coverage}
        if coverage["issues"] and verbose:
            for msg in coverage["issues"]:
                print(f"  ⚠ capture: {msg}")
        if collect:               # for the single-file bundle: one frame per step
            stats["steps"] = step_payload(rendered, motion=False)  # (email-sized)
        return stats
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------
# Config (specreel.yml) — a tiny stdlib-only YAML subset, no PyYAML dependency
# ----------------------------------------------------------------------------

def _parse_scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_parse_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
    return s


def _parse_yaml(text):
    """Parse the small YAML subset specreel.yml uses: nested mappings (2-space
    indent), simple scalar lists (`- item`), and quoted/bare/bool/int scalars.
    Not a general YAML parser — just enough for the documented config schema."""
    lines = []
    for raw in text.splitlines():
        body = raw.split(" #", 1)[0].rstrip() if " #" in raw else raw.rstrip()
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        indent = len(body) - len(body.lstrip(" "))
        lines.append((indent, body.strip()))
    pos = 0

    def block(min_indent):
        nonlocal pos
        result = None
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < min_indent:
                break
            if content.startswith("- "):
                result = result if result is not None else []
                pos += 1
                result.append(_parse_scalar(content[2:]))
            else:
                result = result if result is not None else {}
                m = re.match(r"^([^:]+):\s*(.*)$", content)
                if not m:
                    pos += 1
                    continue
                key, val = m.group(1).strip(), m.group(2)
                pos += 1
                if val == "":
                    if pos < len(lines) and lines[pos][0] > indent:
                        result[key] = block(indent + 1)
                    else:
                        result[key] = None
                else:
                    result[key] = _parse_scalar(val)
        return result if result is not None else {}

    return block(0)


def load_config(path):
    """Load a specreel.yml from an explicit path, else return {}."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        cfg = _parse_yaml(f.read())
    return cfg if isinstance(cfg, dict) else {}


def find_config(explicit, root):
    """Resolve config path: explicit > ./specreel.yml > <root>/specreel.yml."""
    for cand in (explicit, os.path.join(os.getcwd(), "specreel.yml"),
                 os.path.join(root, "specreel.yml")):
        if cand and os.path.exists(cand):
            return cand
    return None


def trim_setup_steps(steps, setup_urls):
    """Drop setup that shouldn't appear in the demo: a `goto` matching a setup
    pattern AND the actions performed on that page.

    Dropping only the goto isn't enough — a sign-in leaves "Type demo@acme.test
    into the Email field" and "Click Log in" behind, which both bores the viewer
    and puts the test account's address in a shareable artifact. Everything from
    a matching goto up to the next navigation is setup, so it all goes.
    """
    if not setup_urls:
        return steps
    out, skipping = [], False
    for s in steps:
        if s["method"] == "goto":
            url = s["params"].get("url") or ""
            skipping = any(pat in url for pat in setup_urls)
            if skipping:
                continue          # the setup navigation itself
        elif skipping:
            continue              # an action ON the setup page (a credential fill)
        out.append(s)
    return out


def gather_build_context():
    """Best-effort repo/branch/build for the gallery header. Reads CI env first
    (GitHub Actions), then falls back to local git, then to nothing. Every field
    is optional — the header degrades gracefully when run outside a repo/CI."""
    def git(*a):
        try:
            r = subprocess.run(["git", *a], capture_output=True, text=True, timeout=3)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        url = git("config", "--get", "remote.origin.url")
        if url:
            repo = re.sub(r"\.git$", "", url.split("/")[-2] + "/" + url.split("/")[-1]) \
                if "/" in url else url
    branch = os.environ.get("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD")
    run = os.environ.get("GITHUB_RUN_NUMBER", "")
    build = f"#{run}" if run else git("rev-parse", "--short", "HEAD")
    return {"repo": repo, "branch": branch, "build": build}


def find_traces(root):
    """All trace.zip files under a directory (Playwright drops them in
    test-results/<test-name>/trace.zip), sorted for stable output."""
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn == "trace.zip" or (fn.endswith(".zip") and "trace" in fn):
                hits.append(os.path.join(dirpath, fn))
    return sorted(hits)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "demo"


def title_from_trace_path(path, root):
    """Playwright names the result dir after the test; turn that into a title."""
    rel = os.path.relpath(os.path.dirname(path), root)
    name = rel if rel not in (".", "") else os.path.splitext(os.path.basename(path))[0]
    name = name.replace(os.sep, " / ")
    return humanize_name(name).strip() or "demo"


def build_manifest(entries, out_dir, ctx, title, showcase=False):
    """Machine-readable index of the gallery — for a future hosted index page,
    a status check, or wiring demos into docs. Sits next to index.html."""
    data = {
        "title": title or "",
        "repo": ctx.get("repo", ""), "branch": ctx.get("branch", ""),
        "build": ctx.get("build", ""),
        # true when this build also carries showcase/ — the curated,
        # customer-facing render (public & passing flows only)
        "showcase": bool(showcase),
        "flows": [{
            "slug": e["slug"], "title": e["title"], "public": e.get("public", False),
            "steps": e["n_steps"], "actions": e["n_actions"], "checks": e["n_checks"],
            "duration": round(e.get("duration", 0), 2), "failed": e["failed"],
            "healed": e.get("healed", False), "sig": e.get("sig", ""),
            # capture coverage: false = the flow's first/last step wasn't recorded
            # (a truncated demo). CI can gate on this; the cloud can badge it.
            "capture_ok": not e.get("capture", {}).get("issues"),
            "demo": f"{e['slug']}/demo.html",
        } for e in entries],
    }
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def build_bundle(entries, out_dir, ctx=None, gallery_title="", theme="dark", analytics=""):
    """One self-contained HTML: the whole gallery + every flow's player inlined
    into a single file (frames are already base64). Host it anywhere, attach it
    to an email, or open it locally — the portable 'send a demo' artifact."""
    ctx = ctx or {}
    flows = [{
        "slug": e["slug"], "title": e["title"], "public": bool(e.get("public")),
        "n_steps": e["n_steps"], "n_actions": e["n_actions"], "n_checks": e["n_checks"],
        "duration": fmt_duration(e.get("duration", 0)), "failed": e["failed"],
        "healed": bool(e.get("healed")) and not e["failed"],
        "thumb": e.get("thumb", ""), "steps": e.get("steps", []),
    } for e in entries]
    n, n_fail = len(flows), sum(1 for f in flows if f["failed"])
    data = {
        "title": gallery_title or "Specreel flows",
        "repo": ctx.get("repo", ""), "branch": ctx.get("branch", ""),
        "build": ctx.get("build", ""),
        "stats": {"flows": n, "fresh": n - n_fail, "failing": n_fail},
        "flows": flows,
    }
    n = len(entries)
    n_fail = sum(1 for e in entries if e["failed"])
    og_desc = (f"{n} flow{'s' if n != 1 else ''}"
               + (f" · {n_fail} failing" if n_fail else " · all fresh")
               + " — demos generated from the tests, always current.")
    tpl = (BUNDLE_TEMPLATE.replace("__THEMEVARS__", theme_block(theme))
           .replace("__ANALYTICS__", analytics)
           .replace("__OG__", og_meta(gallery_title or "Specreel demos", og_desc))
           .replace("__TTSJS__", TTS_JS)
           .replace("__DATA__", _script_json(data)))
    path = os.path.join(out_dir, "gallery.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(tpl)
    return path


def _inline_asset(path):
    """Read a small local image into a data: URI (showcase logo embedding)."""
    mimes = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    mime = mimes.get(os.path.splitext(path)[1].lower())
    if not mime or not os.path.isfile(path):
        return ""
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


def _accent_css(color):
    """CSS overriding the accent color for the showcase. A hex color also
    re-tints the translucent glows; any other CSS color swaps the accent only."""
    color = (color or "").strip()
    if not color:
        return ""
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", color)
    if not m:
        return ":root{--green:" + color + "}"
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c + c for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    dim, glow, off = (f"rgba({r},{g},{b},.09)", f"rgba({r},{g},{b},.5)",
                      f"rgba({r},{g},{b},0)")
    return (":root{--green:" + color + ";--green-dim:" + dim + "}"
            "@keyframes pulse{0%{box-shadow:0 0 0 0 " + glow + "}"
            "70%{box-shadow:0 0 0 7px " + off + "}"
            "100%{box-shadow:0 0 0 0 " + off + "}}")


def build_showcase(entries, out_dir, ctx=None, cfg=None, theme="dark",
                   analytics="", cfg_dir="", bundle=False):
    """The curated, customer-facing render of the same build: only flows marked
    `public: true` that are passing, written to out_dir/showcase/.

    A separate directory, not a filtered view — the players embed their frames,
    so a failing or internal flow hidden with CSS in a shared asset would still
    ship its bytes to anyone who views source. Curation has to happen here, at
    generation time. Provenance (build + verify date) stays visible: that the
    demos are re-verified every build is the trust mark; the failures are the
    internal gallery's business."""
    cfg, ctx = cfg or {}, ctx or {}
    sc_dir = os.path.join(out_dir, "showcase")
    # always start clean — a flow that was public last build but is failing or
    # private now must not linger from an earlier render
    shutil.rmtree(sc_dir, ignore_errors=True)
    public = [e for e in entries if e.get("public")]
    include = [e for e in public if not e["failed"]]
    for e in public:
        if e["failed"]:
            print(f"  showcase: [{e['slug']}] failing — left out of this build")
    if not public:
        sys.stderr.write("  showcase: no flows marked `public: true` in "
                         "specreel.yml — nothing to include\n")
        return None
    if not include:
        sys.stderr.write("  showcase: every public flow is failing — no curated "
                         "gallery this build\n")
        return None
    os.makedirs(sc_dir)
    for e in include:
        shutil.copytree(os.path.join(out_dir, e["slug"]),
                        os.path.join(sc_dir, e["slug"]))

    product = cfg.get("product_name") or ""
    headline = cfg.get("showcase_title") or (
        f"{product} in action" if product else "See it in action")
    tagline = cfg.get("showcase_tagline") or (
        "Every demo below was generated from a real, passing product flow — "
        "regenerated and re-verified on each release.")
    logo_html = ""
    if cfg.get("showcase_logo"):
        lp = cfg["showcase_logo"]
        lp = lp if os.path.isabs(lp) else os.path.join(cfg_dir or ".", lp)
        data = _inline_asset(lp)
        if data:
            logo_html = f'<img class="plogo" src="{data}" alt="">'
        else:
            sys.stderr.write(f"  showcase: logo not found or unsupported type: "
                             f"{cfg['showcase_logo']}\n")
    brand = (f'{logo_html}<span class="pname">{html.escape(product)}</span>'
             if (logo_html or product)
             else '<span class="logo">specreel<span class="dot">.</span></span>')
    custom_css = ""
    if cfg.get("showcase_css"):
        cp = cfg["showcase_css"]
        cp = cp if os.path.isabs(cp) else os.path.join(cfg_dir or ".", cp)
        try:
            custom_css = open(cp, encoding="utf-8").read()
        except OSError:
            sys.stderr.write(f"  showcase: css file not found: {cfg['showcase_css']}\n")

    date = time.strftime("%b %d, %Y")
    build = ctx.get("build", "")
    proof = ('<span class="build"><span class="pulse"></span>verified'
             + (f" · build {html.escape(build)}" if build else "")
             + f" · {date}</span>")
    cards = []
    for e in include:
        thumb = (f'<div class="thumb" style="background-image:url(\'{e["thumb"]}\')"></div>'
                 if e.get("thumb") else '<div class="thumb empty"></div>')
        cards.append(
            f'<a class="card" data-title="{html.escape(e["title"]).lower()}" '
            f'href="{html.escape(e["slug"])}/demo.html">'
            f'{thumb}<div class="cbody"><div class="ct">{html.escape(e["title"])}</div>'
            f'<div class="cmeta"><span class="dur">▶ {fmt_duration(e.get("duration", 0))}</span>'
            f'<span class="ok">✓ verified</span></div></div></a>')
    n = len(include)
    og_desc = (f"{n} product demo{'s' if n != 1 else ''} — generated from real "
               f"flows, verified {date}.")
    tpl = (SHOWCASE_TEMPLATE
           .replace("__THEMEVARS__", theme_block(theme))
           .replace("__ACCENT__", _accent_css(cfg.get("showcase_accent") or ""))
           .replace("__CUSTOMCSS__", custom_css)
           .replace("__ANALYTICS__", analytics)
           .replace("__OG__", og_meta(headline, og_desc))
           .replace("__BRAND__", brand)
           .replace("__PROOF__", proof)
           .replace("__KICKER__", html.escape(product or "Product demos"))
           .replace("__HEADLINE__", html.escape(headline))
           .replace("__TAGLINE__", html.escape(tagline))
           .replace("__CARDS__", "\n".join(cards))
           .replace("__COUNT__", f"{n} demo{'s' if n != 1 else ''}")
           .replace("__DATE__", date))
    path = os.path.join(sc_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(tpl)
    # curated manifest: what the showcase contains and when it was verified —
    # deliberately no health fields (this file is public alongside the pages)
    man = {"title": headline, "build": build, "date": date,
           "flows": [{"slug": e["slug"], "title": e["title"],
                      "duration": round(e.get("duration", 0), 2),
                      "demo": f"{e['slug']}/demo.html"} for e in include]}
    with open(os.path.join(sc_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2)
    if bundle:
        build_bundle(include, sc_dir, ctx=ctx, gallery_title=headline,
                     theme=theme, analytics=analytics)
    return path


def build_notify_payload(entries, ctx, gallery_title, url=""):
    """A Slack-compatible message summarizing a gallery build (BYO incoming webhook)."""
    n = len(entries)
    n_fail = sum(1 for e in entries if e["failed"])
    n_heal = sum(1 for e in entries if e.get("healed") and not e["failed"])
    emoji = "🔴" if n_fail else ("🟡" if n_heal else "🟢")
    bits = [f"*{gallery_title or 'Specreel'}* — {n} flows"]
    bits.append(f"{n - n_fail} fresh" if not n_heal else f"{n - n_fail - n_heal} fresh · {n_heal} updated")
    if n_fail:
        bits.append(f"{n_fail} failing")
    if ctx.get("build"):
        bits.append(f"build {ctx['build']}")
    text = f"{emoji} " + " · ".join(bits)
    if url:
        text += f"\n<{url}|View demos>"
    return {"text": text}


def notify_slack(webhook, payload, timeout=15):
    """POST a JSON payload to a Slack incoming-webhook URL. Best-effort."""
    import urllib.request
    try:
        req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        sys.stderr.write(f"specreel: notify failed ({type(e).__name__}: {e})\n")
        return False


def generate_gallery(root, out_dir, want_mp4=False, config_path=None,
                     ai=False, api_key=None, ai_model=None, bundle=False, theme=None,
                     notify=None, public_url="", voice=None, tts_model=None, tts_key=None,
                     tts_instructions=None, strict=False, quality=None, want_gif=False,
                     showcase=False):
    """Batch mode: render every trace under `root` and write an index.html.

    Honors an optional specreel.yml: per-flow title/public/hidden, a gallery
    title, setup_urls (leading auth/redirect steps to drop), and AI narration
    (ai / ai_model). CLI flags take precedence over config.
    """
    traces = find_traces(root)
    if not traces:
        sys.stderr.write(f"No trace.zip found under {root}\n")
        return 1
    cfg_path = find_config(config_path, root)
    cfg = load_config(cfg_path)
    # showcase_logo / showcase_css paths resolve relative to the config file
    cfg_dir = os.path.dirname(os.path.abspath(cfg_path)) if cfg_path else os.getcwd()
    flows_cfg = cfg.get("flows") or {}
    setup_urls = cfg.get("setup_urls") or []
    gallery_title = cfg.get("title") or ""
    # SPECREEL_PRODUCT lets a host (e.g. Specreel Cloud) name the product for
    # narration without writing a config file into the user's traces
    product = cfg.get("product_name") or os.environ.get("SPECREEL_PRODUCT", "")
    theme = (theme or cfg.get("theme") or "dark").lower()
    if theme not in THEME_VARS:
        theme = "dark"
    analytics = cfg.get("analytics") or ""    # raw HTML snippet injected into <head>
    bundle = bundle or bool(cfg.get("bundle"))
    showcase = showcase or bool(cfg.get("showcase"))
    quality = (quality or cfg.get("quality") or DEFAULT_QUALITY).lower()
    if quality not in QUALITY_FRAMES:
        quality = DEFAULT_QUALITY
    ai = ai or bool(cfg.get("ai"))
    ai_model = ai_model or cfg.get("ai_model") or DEFAULT_AI_MODEL
    api_key = resolve_api_key(api_key)
    if ai and not api_key:
        sys.stderr.write("specreel: --ai set but ANTHROPIC_API_KEY is empty — "
                         "rendering literal captions (no narration)\n")
        ai = False
    # studio voiceover (opt-in, BYO OPENAI_API_KEY)
    voice = voice or cfg.get("voice")
    tts_model = tts_model or cfg.get("tts_model") or DEFAULT_TTS_MODEL
    tts_instructions = tts_instructions or cfg.get("tts_instructions")
    tts_key = resolve_tts_key(tts_key)
    if voice and not tts_key:
        sys.stderr.write("specreel: voice set but OPENAI_API_KEY is empty — "
                         "using the browser voice instead\n")
        voice = None
    if cfg:
        print(f"  config: specreel.yml ({len(flows_cfg)} flow overrides"
              f"{', ' + str(len(setup_urls)) + ' setup url(s)' if setup_urls else ''}"
              f"{', AI narration on' if ai else ''})")

    os.makedirs(out_dir, exist_ok=True)
    # previous build's per-flow signatures, to detect "changed since last build"
    prev_sigs = {}
    prev_path = os.path.join(out_dir, "manifest.json")
    if os.path.exists(prev_path):
        try:
            for f in json.load(open(prev_path, encoding="utf-8")).get("flows", []):
                if f.get("sig"):
                    prev_sigs[f["slug"]] = f["sig"]
        except Exception:
            pass
    entries = []
    used = {"showcase"}      # reserved: the curated render lives at out/showcase/
    for tp in traces:
        title = title_from_trace_path(tp, root)
        slug = slugify(title)
        n, base = 1, slug
        while slug in used:                       # de-dupe slugs
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        fc = flows_cfg.get(slug) or {}
        if fc.get("hidden"):
            print(f"[{slug}] hidden (config)")
            continue
        title = fc.get("title") or title
        print(f"[{slug}] {title}")
        try:
            stats = generate_demo(tp, os.path.join(out_dir, slug), title=title,
                                  want_mp4=want_mp4, verbose=False, setup_urls=setup_urls,
                                  ai=ai, api_key=api_key, ai_model=ai_model, product=product,
                                  collect=bundle, theme=theme, analytics=analytics,
                                  voice=voice, tts_model=tts_model, tts_key=tts_key,
                                  tts_instructions=tts_instructions,
                                  quality=quality, want_gif=want_gif)
        except (zipfile.BadZipFile, OSError, KeyError, json.JSONDecodeError) as e:
            # one corrupt/vanished trace must not abort the other N-1 demos
            sys.stderr.write(f"[{slug}] SKIPPED — unreadable trace "
                             f"({type(e).__name__}: {e})\n")
            continue
        stats["slug"] = slug
        stats["public"] = bool(fc.get("public"))
        # amber "updated" state: steps changed vs last build (still green), or a
        # CI healer flagged this flow via `healed: true`.
        changed = (slug in prev_sigs and prev_sigs[slug] != stats["sig"]
                   and not stats["failed"])
        stats["healed"] = bool(fc.get("healed")) or changed
        entries.append(stats)
        flag = ("FAIL" if stats["failed"] else ("updated" if stats["healed"] else "ok"))
        print(f"    {stats['n_steps']} steps · {stats['n_actions']} actions · "
              f"{stats['n_checks']} checks · {fmt_duration(stats['duration'])} · {flag}")
        # always-on capture check: warn (or, under --strict, fail) when a flow's
        # first/last step wasn't captured — the recurring "missing step" bug.
        for msg in stats.get("capture", {}).get("issues", []):
            sys.stderr.write(f"    ⚠ [{slug}] {msg}\n")
    ctx = gather_build_context()
    index = build_index(entries, out_dir, ctx=ctx, gallery_title=gallery_title,
                        theme=theme, analytics=analytics)
    sc_path = build_showcase(entries, out_dir, ctx=ctx, cfg=cfg, theme=theme,
                             analytics=analytics, cfg_dir=cfg_dir,
                             bundle=bundle) if showcase else None
    manifest = build_manifest(entries, out_dir, ctx, gallery_title,
                              showcase=sc_path is not None)
    print(f"\n  {len(entries)} demos -> {index}\n  manifest -> {manifest}")
    if sc_path:
        print(f"  showcase -> {sc_path}  (curated: public & passing flows only)")
    if bundle:
        bpath = build_bundle(entries, out_dir, ctx=ctx, gallery_title=gallery_title,
                             theme=theme, analytics=analytics)
        kb = os.path.getsize(bpath) // 1024
        print(f"  bundle   -> {bpath}  ({kb} KB, single self-contained file)")

    webhook = notify or cfg.get("notify_webhook") or os.environ.get("SPECREEL_SLACK_WEBHOOK", "")
    if webhook:
        url = public_url or cfg.get("public_url") or ""
        if notify_slack(webhook, build_notify_payload(entries, ctx, gallery_title, url)):
            print("  notified -> Slack")
    # --strict (CI): a truncated capture is a build failure, like a red test —
    # so a demo missing its first/last step can't ship silently.
    truncated = [e["slug"] for e in entries if e.get("capture", {}).get("issues")]
    if strict and truncated:
        sys.stderr.write(f"\n  specreel --strict: {len(truncated)} flow(s) with a "
                         f"capture gap — {', '.join(truncated)}\n")
        return 1
    return 0


# ----------------------------------------------------------------------------
# 5. Publish: deploy a generated gallery to a real URL (no backend required)
# ----------------------------------------------------------------------------

def repo_root(start="."):
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def pages_url_from_remote(remote):
    """github.com remote -> the https://<owner>.github.io/<repo>/ Pages URL."""
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$", remote or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return f"https://{owner}.github.io/{repo}/"


def embed_snippet(base_url):
    base = base_url.rstrip("/")
    return (f'<iframe src="{base}/" width="100%" height="640" '
            f'style="border:1px solid #23272b;border-radius:12px" '
            f'title="Specreel demos" loading="lazy"></iframe>\n'
            f'<!-- link one flow directly: {base}/gallery.html#<flow-slug> -->')


def _zip_dir(path):
    """Zip a directory's contents into bytes (paths relative to `path`)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, path))
    return buf.getvalue()


def _post_multipart(url, token, fields, file_field, filename, file_bytes, timeout=120):
    """Minimal stdlib multipart/form-data POST (keeps the CLI dependency-free)."""
    import urllib.request, urllib.error
    boundary = "----specreel" + os.urandom(8).hex()
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: application/zip\r\n\r\n" % (boundary, file_field, filename)).encode("utf-8"))
    parts.append(file_bytes)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "X-Api-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {e.code}"}


def publish_cloud(site, cloud_url, token, project=None):
    """Upload a gallery to Specreel Cloud and return its hosted URL."""
    cloud_url = (cloud_url or os.environ.get("SPECREEL_CLOUD_URL", "")).rstrip("/")
    token = token or os.environ.get("SPECREEL_CLOUD_TOKEN", "")
    if not cloud_url or not token:
        sys.stderr.write("publish: set --cloud-url and --token (or SPECREEL_CLOUD_URL / "
                         "SPECREEL_CLOUD_TOKEN).\n")
        return 1
    project = project or slugify(os.path.basename(os.path.abspath(site))) or "demos"
    title, n_flows, n_failing = project, 0, 0
    flow_status = []        # compact per-flow status, drives cloud monitoring (feature A)
    man = os.path.join(site, "manifest.json")
    if os.path.exists(man):
        try:
            d = json.load(open(man, encoding="utf-8"))
            flows = d.get("flows", [])
            title = d.get("title") or title
            n_flows = len(flows)
            n_failing = sum(1 for f in flows if f.get("failed"))
            flow_status = [{"slug": f.get("slug", ""), "title": f.get("title", ""),
                            "failed": bool(f.get("failed")), "healed": bool(f.get("healed"))}
                           for f in flows if f.get("slug")]
        except Exception:
            pass
    print(f"  uploading {site} -> {cloud_url} (project '{project}') ...")
    resp = _post_multipart(cloud_url + "/api/v1/publish", token,
                           {"project": project, "title": title,
                            "n_flows": n_flows, "n_failing": n_failing,
                            "flows": json.dumps(flow_status)},
                           "gallery", "site.zip", _zip_dir(site))
    if resp.get("ok"):
        print(f"  published -> {resp['url']}")
        print("\n  embed:\n" + embed_snippet(resp["url"]))
        return 0
    sys.stderr.write(f"publish: cloud rejected the upload ({resp.get('error', 'unknown error')})\n")
    return 1


def publish(site, target, message="specreel: publish gallery", cloud_url=None,
            token=None, project=None):
    """Deploy an already-generated gallery `site/` dir to a target.
    Targets: 'dir:<path>' | 'ghpages' | 'cloud' (Specreel Cloud)."""
    if not os.path.isdir(site) or not os.path.exists(os.path.join(site, "index.html")):
        sys.stderr.write(f"publish: '{site}' is not a generated gallery "
                         f"(run `specreel <traces> -o {site}` first)\n")
        return 1

    if target.startswith("dir:") or target == "dir":
        dest = target[4:] if target.startswith("dir:") else ""
        if not dest:
            sys.stderr.write("publish: use --to dir:<path>\n")
            return 1
        dest = os.path.abspath(dest)
        shutil.copytree(site, dest, dirs_exist_ok=True)
        url = "file:///" + dest.lstrip("/")
        print(f"  published -> {dest}")
        print(f"  open: {url}/index.html")
        print("\n  embed:\n" + embed_snippet(url))
        return 0

    if target == "cloud":
        return publish_cloud(site, cloud_url, token, project)

    if target == "ghpages":
        root = repo_root(site) or repo_root(".")
        if not root:
            sys.stderr.write("publish: not a git repo — `git init` and add a GitHub remote first\n")
            return 1
        try:
            remote = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                                    capture_output=True, text=True).stdout.strip()
        except Exception:
            remote = ""
        if not remote:
            sys.stderr.write(
                "publish: no 'origin' remote. Create a GitHub repo and run:\n"
                "  git remote add origin git@github.com:<you>/<repo>.git\n"
                "then re-run `specreel publish %s --to ghpages`.\n" % site)
            return 1
        # build a clean single-commit gh-pages from the site dir and force-push it,
        # without touching the working tree or carrying source history.
        ghdir = tempfile.mkdtemp(prefix="specreel_ghpages_")
        try:
            for name in os.listdir(site):
                s = os.path.join(site, name)
                d = os.path.join(ghdir, name)
                shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy2(s, d)
            open(os.path.join(ghdir, ".nojekyll"), "w").close()  # serve _underscore dirs
            for cmd in (["init", "-q"], ["checkout", "-q", "-b", "gh-pages"],
                        ["add", "-A"], ["-c", "user.email=specreel@local",
                                        "-c", "user.name=specreel", "commit", "-q", "-m", message]):
                subprocess.run(["git", "-C", ghdir, *cmd], check=True)
            push = subprocess.run(["git", "-C", ghdir, "push", "-q", "--force", remote,
                                   "gh-pages"], capture_output=True, text=True)
            if push.returncode != 0:
                sys.stderr.write(push.stderr[-800:] + "\npublish: push failed.\n")
                return 1
        finally:
            shutil.rmtree(ghdir, ignore_errors=True)
        url = pages_url_from_remote(remote)
        print(f"  pushed gh-pages -> {remote}")
        if url:
            print(f"  URL (enable once: Settings → Pages → Branch: gh-pages /root):\n  {url}")
            print("\n  embed:\n" + embed_snippet(url))
        return 0

    sys.stderr.write(f"publish: unknown target '{target}' (use dir:<path> or ghpages)\n")
    return 1


def publish_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py publish",
                                 description="deploy a generated gallery to a URL")
    ap.add_argument("site", help="the gallery directory (e.g. site/)")
    ap.add_argument("--to", default="ghpages",
                    help="target: dir:<path> (copy) | ghpages | cloud")
    ap.add_argument("--message", default="specreel: publish gallery")
    ap.add_argument("--cloud-url", default=None, help="Specreel Cloud base URL (or SPECREEL_CLOUD_URL)")
    ap.add_argument("--token", default=None, help="Cloud API token (or SPECREEL_CLOUD_TOKEN)")
    ap.add_argument("--project", default=None, help="cloud project slug (default: the dir name)")
    args = ap.parse_args(argv)
    return publish(args.site, args.to, message=args.message,
                   cloud_url=args.cloud_url, token=args.token, project=args.project)


# ----------------------------------------------------------------------------
# 6. Init: scaffold a specreel.yml from a directory of traces (activation)
# ----------------------------------------------------------------------------

def init_config(root, out_path):
    """Discover traces and write a starter specreel.yml — slugs match what the
    gallery will produce, so the user just edits titles and flips flags."""
    traces = find_traces(root)
    if not traces:
        sys.stderr.write(f"init: no trace.zip found under {root}\n")
        return 1
    if os.path.exists(out_path):
        sys.stderr.write(f"init: {out_path} already exists — not overwriting\n")
        return 1
    used, flows = set(), []
    for tp in traces:
        title = title_from_trace_path(tp, root)
        slug, base, n = slugify(title), slugify(title), 1
        while slug in used:
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        flows.append((slug, title))
    lines = [
        "# specreel.yml — generated by `specreel init`. Edit freely.",
        "title: My App — Product Flows",
        "# product_name: My App   # --ai narration says this instead of raw URLs",
        "# theme: dark            # dark | light",
        "# ai: false              # true (+ ANTHROPIC_API_KEY) to narrate captions",
        "# bundle: false          # true to also emit a single-file gallery.html",
        "",
        "# setup_urls:            # leading nav steps to drop from every demo",
        "#   - /login",
        "",
        "flows:",
    ]
    for slug, title in flows:
        lines += [f"  {slug}:", f"    title: {title}", "    public: true"]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  wrote {out_path}  ({len(flows)} flows discovered)")
    print(f"  next: edit titles/flags, then  specreel.py {root} -o site --bundle")
    return 0


def manifest_diff(old, new):
    """Compare two gallery manifests by flow slug. Returns a dict of lists:
    regressed (was passing, now failing), recovered, added, removed, still_failing.
    Each entry is the relevant flow dict (new for all but removed)."""
    old_by = {f.get("slug"): f for f in (old or {}).get("flows", []) if f.get("slug")}
    new_by = {f.get("slug"): f for f in (new or {}).get("flows", []) if f.get("slug")}
    out = {"regressed": [], "recovered": [], "added": [], "removed": [], "still_failing": []}
    for slug, f in new_by.items():
        was = old_by.get(slug)
        if was is None:
            out["added"].append(f)
        elif f.get("failed") and not was.get("failed"):
            out["regressed"].append(f)
        elif not f.get("failed") and was.get("failed"):
            out["recovered"].append(f)
        elif f.get("failed") and was.get("failed"):
            out["still_failing"].append(f)
    for slug, f in old_by.items():
        if slug not in new_by:
            out["removed"].append(f)
    return out


def _diff_markdown(diff, base=""):
    """The 'Changes vs previous build' block for a PR comment, or '' if no changes."""
    def names(rows):
        return ", ".join((f"[{f['title']}]({base}/{f['demo']})" if base and f.get("demo")
                          else f.get("title", f.get("slug", "?"))) for f in rows)
    parts = []
    if diff["regressed"]:
        parts.append(f"- ⚠️ **Regressed:** {names(diff['regressed'])}")
    if diff["recovered"]:
        parts.append(f"- ✅ **Recovered:** {names(diff['recovered'])}")
    if diff["added"]:
        parts.append(f"- ➕ **Added:** {names(diff['added'])}")
    if diff["removed"]:
        parts.append(f"- ➖ **Removed:** {names(diff['removed'])}")
    if not parts:
        return ""
    return "\n".join(["", "**Changes vs previous build**", *parts])


def markdown_summary(man, url="", diff=None):
    """A PR-comment-ready markdown summary of a gallery, from its manifest dict.
    With `diff` (from manifest_diff), prepends a 'Changes vs previous build' block.
    Ends with an HTML marker so a workflow can upsert a single sticky comment."""
    flows = man.get("flows", [])
    n = len(flows)
    nf = sum(1 for f in flows if f["failed"])
    nh = sum(1 for f in flows if f.get("healed") and not f["failed"])
    badge = (f"🔴 {nf} failing" if nf else (f"🟡 {nh} updated" if nh else "🟢 all fresh"))
    base = url.rstrip("/")
    lines = [f"### 🎬 Specreel — {man.get('title') or 'demos'}",
             f"{n} flows · {badge}" + (f" · build {man['build']}" if man.get("build") else "")]
    if url:
        lines.append(f"\n[▶ View the demo gallery]({base}/)")
    if diff:
        block = _diff_markdown(diff, base)
        if block:
            lines.append(block)
    lines += ["", "| flow | test | steps |", "|---|---|---|"]
    for f in flows:
        st = "❌ failed" if f["failed"] else ("🟡 updated" if f.get("healed") else "✅ pass")
        name = f"[{f['title']}]({base}/{f['demo']})" if url else f["title"]
        lines.append(f"| {name} | {st} | {f['steps']} |")
    lines += ["", "<!-- specreel-summary -->"]
    return "\n".join(lines)


def summary_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py summary",
                                 description="print a markdown summary of a gallery")
    ap.add_argument("site", help="the gallery directory (with manifest.json)")
    ap.add_argument("--url", default="", help="public base URL for links")
    ap.add_argument("--since", default=None,
                    help="a previous manifest.json to diff against (adds a Changes block)")
    args = ap.parse_args(argv)
    mpath = os.path.join(args.site, "manifest.json")
    if not os.path.exists(mpath):
        sys.stderr.write(f"summary: no manifest.json in {args.site}\n")
        return 1
    man = json.load(open(mpath, encoding="utf-8"))
    diff = None
    if args.since and os.path.exists(args.since):
        diff = manifest_diff(json.load(open(args.since, encoding="utf-8")), man)
    print(markdown_summary(man, url=args.url, diff=diff))
    return 0


NOTES_SYSTEM = (
    "You write concise, user-facing release notes from a product's end-to-end flow "
    "inventory and what changed since the last build. Output GitHub-flavored markdown: "
    "a one-line summary, then a few short bullets. Be specific and factual — use only "
    "the flows and changes provided; never invent features. Frame recovered flows as "
    "fixes, regressed flows as known issues, and added flows as new capabilities.")


def build_notes_request(man, diff, model, product=""):
    """Messages API body to draft release notes from a manifest (+ optional diff)."""
    flows = [{"title": f.get("title"), "passing": not f.get("failed")}
             for f in man.get("flows", [])]
    payload = {"title": man.get("title") or product or "the product", "flows": flows}
    if diff:
        changes = {k: [f.get("title") for f in v] for k, v in diff.items() if v}
        if changes:
            payload["changes_since_last_build"] = changes
    if product:
        payload["product"] = product
    return {
        "model": model, "max_tokens": 700,
        "system": [{"type": "text", "text": NOTES_SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    }


def release_notes(man, diff, api_key, model=DEFAULT_AI_MODEL, timeout=60, product=""):
    """Draft markdown release notes from a manifest + optional diff. Best-effort:
    returns the markdown string, or '' on any failure (graceful degrade)."""
    try:
        body = build_notes_request(man, diff, model, product=product)
        resp = _anthropic_messages(body, api_key, timeout=timeout)
        return next((b.get("text", "") for b in resp.get("content", [])
                     if b.get("type") == "text"), "").strip()
    except Exception as e:
        sys.stderr.write(f"notes: generation failed ({type(e).__name__}: {e})\n")
        return ""


def notes_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py notes",
                                 description="draft release notes from a gallery (AI, BYO-key)")
    ap.add_argument("site", help="the gallery directory (with manifest.json)")
    ap.add_argument("--since", default=None, help="a previous manifest.json to diff against")
    ap.add_argument("--ai-model", default=DEFAULT_AI_MODEL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--product", default="", help="product name for the notes")
    ap.add_argument("-o", "--out", default=None, help="write to a file instead of stdout")
    args = ap.parse_args(argv)
    mpath = os.path.join(args.site, "manifest.json")
    if not os.path.exists(mpath):
        sys.stderr.write(f"notes: no manifest.json in {args.site}\n")
        return 1
    key = resolve_api_key(args.api_key)
    if not key:
        sys.stderr.write("notes: set ANTHROPIC_API_KEY (or --api-key) — notes need AI\n")
        return 1
    man = json.load(open(mpath, encoding="utf-8"))
    diff = None
    if args.since and os.path.exists(args.since):
        diff = manifest_diff(json.load(open(args.since, encoding="utf-8")), man)
    md = release_notes(man, diff, key, model=args.ai_model, product=args.product)
    if not md:
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"  notes -> {args.out}")
    else:
        print(md)
    return 0


# ----------------------------------------------------------------------------
# 7. Recommend: crawl a running app, suggest demo-worthy flows, scaffold a
#    runnable Playwright script. Activation for users who have no tests yet.
# ----------------------------------------------------------------------------

class _PageParser(HTMLParser):
    """Minimal HTML scan: title, headings, forms (+ fields), buttons, links."""
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self.headings = []
        self._htag = None
        self._hbuf = ""
        self.links = []
        self._href = None
        self._abuf = ""
        self._alabel = ""        # aria-label/title fallback for text-less links
        self.buttons = []
        self._btn = False
        self._bbuf = ""
        self._blabel = ""
        self.forms = []
        self._form = None
        self.inputs = []
        self._svg = 0            # inside <svg>: its <title>/text is icon metadata,
                                 # not page content (Stripe's page title came out
                                 # as "…Stripe logoStripe logoGuidesCard_32")

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "svg":
            self._svg += 1
            return
        if self._svg:
            return
        if tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3"):
            self._htag, self._hbuf = tag, ""
        elif tag == "a" and d.get("href"):
            self._href, self._abuf = d["href"], ""
            # an overlay/icon link often has NO text and carries its accessible
            # name in aria-label (or title) — that IS what get_by_role(name=…)
            # matches, so capture it or the link looks nameless to us
            self._alabel = d.get("aria-label") or d.get("title") or ""
        elif tag == "button":
            self._btn, self._bbuf = True, ""
            self._blabel = d.get("aria-label") or d.get("title") or ""
        elif tag == "form":
            self._form = {"action": d.get("action", ""),
                          "method": (d.get("method") or "get").lower(), "fields": []}
        elif tag in ("input", "textarea"):
            f = {"name": d.get("name", ""), "placeholder": d.get("placeholder", ""),
                 "type": ("textarea" if tag == "textarea" else (d.get("type") or "text").lower()),
                 "id": d.get("id", "")}
            if f["type"] not in ("hidden", "submit", "button", "checkbox", "radio", "file"):
                self.inputs.append(f)
            if self._form is not None:
                self._form["fields"].append(f)

    def handle_endtag(self, tag):
        if tag == "svg":
            self._svg = max(0, self._svg - 1)
            return
        if self._svg:
            return
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3") and self._htag == tag:
            t = " ".join(self._hbuf.split())
            if t:
                self.headings.append(t)
            self._htag = None
        elif tag == "a" and self._href is not None:
            txt = " ".join(self._abuf.split()) or " ".join(
                (getattr(self, "_alabel", "") or "").split())
            self.links.append({"text": txt, "href": self._href})
            self._href, self._alabel = None, ""
        elif tag == "button" and self._btn:
            t = " ".join(self._bbuf.split()) or " ".join(
                (getattr(self, "_blabel", "") or "").split())
            if t:
                self.buttons.append(t)
            self._btn, self._blabel = False, ""
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._svg:
            return
        if self._in_title:
            self.title += data
        if self._htag:
            self._hbuf += data
        if self._href is not None:
            self._abuf += data
        if self._btn:
            self._bbuf += data


def extract_page(html_text, url):
    p = _PageParser()
    try:
        p.feed(html_text)
    except Exception:
        pass
    return {"url": url, "title": " ".join(p.title.split()) or url,
            "headings": p.headings[:6],
            "forms": [f for f in p.forms if f["fields"]],
            "buttons": p.buttons[:12], "inputs": p.inputs, "links": p.links}


def _fetch_html(url, timeout=8, headers=None):
    import urllib.request
    h = {"User-Agent": "specreel-recommend/1"}
    if headers:
        h.update(headers)              # e.g. Cookie / Authorization for logged-in crawls
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if "html" not in (r.headers.get("content-type") or ""):
            return ""
        return r.read(800000).decode("utf-8", "replace")


_ASSET_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js",
              ".ico", ".pdf", ".zip", ".woff", ".woff2", ".ttf", ".map")


def _candidate_links(url, links, host):
    """Same-origin, non-asset links to follow next (resolved + de-fragmented)."""
    from urllib.parse import urljoin, urlparse
    out = []
    for ln in links:
        href = ln.get("href")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        nxt = urljoin(url, href)
        pu = urlparse(nxt)
        if pu.netloc != host or pu.path.lower().endswith(_ASSET_EXT):
            continue
        out.append(nxt)
    return out


def crawl(base, max_pages=12, fetch=None, headers=None):
    """Breadth-first, same-origin crawl of the raw server HTML. `fetch(url)->html`
    is injectable for tests. `headers` (e.g. a Cookie) enables logged-in crawls.
    For client-rendered SPAs, use crawl_browser()."""
    from urllib.parse import urlparse
    fetch = fetch or (lambda u: _fetch_html(u, headers=headers))
    host = urlparse(base).netloc
    seen, queue, pages = set(), [base], []
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        key = url.split("#")[0].rstrip("/") or url
        if key in seen:
            continue
        seen.add(key)
        try:
            body = fetch(url)
        except Exception:
            body = ""
        if not body:
            continue
        pg = extract_page(body, url)
        pages.append(pg)
        for nxt in _candidate_links(url, pg["links"], host):
            if (nxt.split("#")[0].rstrip("/") or nxt) not in seen:
                queue.append(nxt)
    return pages


# Browser-rendered DOM extractor — same shape extract_page produces, but read from
# the live DOM after JS runs, so single-page apps map richly.
_BROWSER_EXTRACT = r"""() => {
  const t = el => (el.textContent || '').trim().replace(/\s+/g, ' ');
  // visible === can Playwright act on it? A field that exists but is hidden
  // (a search box inside a closed modal) makes .fill() time out unless the flow
  // opens it first — so record it, and what probably opens it.
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const opener = i => {
    // guess by the FIELD's semantics, not by container: a hidden search box is
    // almost always revealed by a control whose accessible name says "search"
    const kw = ((i.placeholder||'')+' '+(i.name||'')+' '+(i.getAttribute('type')||'')).toLowerCase();
    const want = /search/.test(kw) ? /search/i
               : /subscribe|sign.?up/.test(kw) ? /subscribe|sign.?up/i
               : /search|menu|open|toggle|show/i;
    const cands = [...document.querySelectorAll('button,[role=button],a[aria-label]')]
      .map(b => b.getAttribute('aria-label') || (b.textContent || '').trim())
      .filter(x => x && x.length < 40 && want.test(x));
    return cands[0] || '';
  };
  const fieldOf = i => ({name: i.name || '', placeholder: i.placeholder || '', id: i.id || '',
    visible: vis(i), opened_by: vis(i) ? '' : opener(i),
    type: i.tagName === 'TEXTAREA' ? 'textarea' : ((i.getAttribute('type') || 'text').toLowerCase())});
  const skip = new Set(['hidden','submit','button','checkbox','radio','file','image','reset']);
  const inputs = [...document.querySelectorAll('input,textarea')].map(fieldOf).filter(f => !skip.has(f.type));
  const forms = [...document.querySelectorAll('form')].map(f => ({
    fields: [...f.querySelectorAll('input,textarea')].map(fieldOf)})).filter(f => f.fields.length);
  return {
    title: (document.title || '').trim(),
    headings: [...document.querySelectorAll('h1,h2,h3')].map(t).filter(Boolean).slice(0, 6),
    forms: forms,
    // Prefer action buttons over chrome: drop loading spinners and keep enough
    // that page-local CTAs ("From library", "Run scorecard") survive after nav.
    buttons: [...document.querySelectorAll('button,[role=button]')].map(el => {
        const a = (el.getAttribute('aria-label') || '').trim();
        let tx = t(el);
        // SPA buttons often concatenate idle+loading labels into one string
        // ("Sign In Signing in...") — keep the idle name Playwright can match.
        tx = tx.replace(/^(Sign In)\s*Signing in\.\.\.$/i, '$1')
               .replace(/^(Log In)\s*Logging in\.\.\.$/i, '$1');
        return (a && a.length < 60 && !/signing in/i.test(a) ? a : tx);
      }).filter(x => x && x.length > 1 && x.length < 80 && !/^loading/i.test(x))
      .filter((x, i, arr) => arr.findIndex(y => y.toLowerCase() === x.toLowerCase()) === i)
      .slice(0, 40),
    inputs: inputs,
    links: [...document.querySelectorAll('a[href]')].map(a => ({text: t(a), href: a.getAttribute('href')})),
    // role counts stop NL flows inventing checkbox/option clicks on pages that
    // have none (Kumkuat audiences use "Add all" buttons, not checkboxes).
    roles: {
      checkbox: document.querySelectorAll('input[type=checkbox],[role=checkbox]').length,
      option: document.querySelectorAll('[role=option]').length,
      listitem: document.querySelectorAll('[role=listitem]').length,
    },
  };
}"""


def _goto_settled(page, url, timeout=20000, wait_ms=1200):
    """Navigate somewhere that may re-navigate itself.

    SPAs routinely replace the URL on mount (auth checks, locale/router
    redirects). If that lands while goto() is still pending, Playwright aborts
    with "interrupted by another navigation" — a race, not a broken site, and it
    made kumkuat.ai unscannable from Cloud Run while working locally. Treat it
    as "the app took over" and wait for the page it settled on.

    Waits for domcontentloaded rather than load: a slow third-party asset (fonts,
    analytics) shouldn't fail a crawl, and wait_ms covers rendering anyway.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as e:
        msg = str(e)
        if "interrupted by another navigation" in msg or "navigation to" in msg.lower():
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:
                pass                       # it may already be settled
        else:
            # one retry: transient DNS/TLS blips shouldn't sink a whole crawl
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    page.wait_for_timeout(wait_ms)         # let the SPA render


class CrawlFailed(Exception):
    """Every page failed to load — carries the first underlying reason so the
    user sees what actually went wrong, not a generic empty result."""


def browser_login(page, login_url, user, password, wait_ms=1500):
    """Sign in with a real browser before crawling, so discovery sees the
    AUTHENTICATED app instead of its marketing shell. Best-effort: returns True
    if it believes it logged in (the URL moved off the login page, or the
    password field is gone). Never raises."""
    try:
        _goto_settled(page, login_url, timeout=25000, wait_ms=600)
        pw = page.locator("input[type=password]").first
        pw.wait_for(state="visible", timeout=8000)
        # the identifier field: the fillable text/email input before the password
        ident = page.locator(
            "input[type=email], input[type=text], input[name*=user i], "
            "input[name*=email i], input[id*=user i], input[id*=email i]").first
        try:
            ident.fill(user, timeout=5000)
        except Exception:
            pass
        pw.fill(password, timeout=5000)
        try:
            page.get_by_role("button", name=re.compile(
                r"log ?in|sign ?in|continue|submit", re.I)).first.click(timeout=5000)
        except Exception:
            pw.press("Enter")
        page.wait_for_load_state("load", timeout=20000)
        page.wait_for_timeout(wait_ms)
        moved = page.url.rstrip("/") != login_url.rstrip("/")
        gone = page.locator("input[type=password]").count() == 0
        return bool(moved or gone)
    except Exception as e:
        sys.stderr.write(f"  login: could not sign in ({type(e).__name__}) — "
                         "crawling as an anonymous visitor\n")
        return False


def crawl_browser(base, max_pages=12, wait_ms=1200, headers=None, login=None):
    """Render each page with Playwright before extracting, so client-rendered
    apps expose their real DOM. Needs Playwright installed; returns the same page
    dicts as crawl() so recommend_flows()/scaffold work unchanged. `headers` (e.g.
    a Cookie) are sent with every request for logged-in crawls."""
    from urllib.parse import urlparse
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.stderr.write("recommend --browser needs Playwright:\n"
                         "  pip install playwright && playwright install chromium\n")
        return []
    host = urlparse(base).netloc
    seen, queue, pages = set(), [base], []
    errors = []
    with sync_playwright() as p:
        # container-safe flags: the Chrome sandbox needs privileges a hardened
        # container doesn't grant, and /dev/shm is tiny in most runtimes
        browser = p.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        if headers:
            ctx.set_extra_http_headers(headers)
        page = ctx.new_page()
        try:
            if login and login.get("url") and login.get("password"):
                if browser_login(page, login["url"], login.get("user", ""),
                                 login["password"], wait_ms=wait_ms):
                    print("  login: signed in — crawling the authenticated app",
                          file=sys.stderr)
            while queue and len(pages) < max_pages:
                url = queue.pop(0)
                key = url.split("#")[0].rstrip("/") or url
                if key in seen:
                    continue
                seen.add(key)
                try:
                    _goto_settled(page, url, wait_ms=wait_ms)
                    data = page.evaluate(_BROWSER_EXTRACT)
                except Exception as e:
                    # remember WHY. Swallowing this made a total failure look
                    # identical to "the site has no pages", and the user got
                    # "is the app running?" for a site that was plainly running.
                    if not errors:
                        errors.append(f"{url}: {type(e).__name__}: "
                                      f"{str(e).splitlines()[0][:160]}")
                    continue
                data["url"] = url
                pages.append(data)
                for nxt in _candidate_links(url, data["links"], host):
                    if (nxt.split("#")[0].rstrip("/") or nxt) not in seen:
                        queue.append(nxt)
        finally:
            browser.close()
    if not pages and errors:
        # surface the first real reason instead of an empty list
        sys.stderr.write(f"  crawl: {errors[0]}\n")
        raise CrawlFailed(errors[0])
    return pages


_FILLABLE = {"text", "email", "password", "search", "tel", "url", "number", "textarea"}


def _fillable(f):
    return f["type"] in _FILLABLE and (f["name"] or f["placeholder"] or f["id"])


_USER_RE = re.compile(r"user|email|login|account|e-?mail", re.I)
_PASS_RE = re.compile(r"pass|pwd", re.I)


def find_login_fields(pg):
    """Locate the username + password inputs on a rendered login page.

    Returns {"user": field, "password": field, "submit": "Button name"} with any
    part possibly missing. Password is identified by type=password (the reliable
    signal); the username is the nearest text/email field that looks like an
    identifier — or, failing that, any other fillable field on the form.
    """
    fields = list(pg.get("inputs") or [])
    for fm in pg.get("forms") or []:
        for f in fm.get("fields") or []:
            if f not in fields:
                fields.append(f)
    pw = next((f for f in fields if f.get("type") == "password"), None)
    if not pw:
        pw = next((f for f in fields
                   if _PASS_RE.search(f.get("name", "") + f.get("id", "")
                                      + f.get("placeholder", ""))), None)
    ident = next((f for f in fields
                  if f is not pw and f.get("type") in ("text", "email", "search")
                  and _USER_RE.search(f.get("name", "") + f.get("id", "")
                                      + f.get("placeholder", ""))), None)
    if not ident:
        ident = next((f for f in fields if f is not pw and _fillable(f)), None)
    submit = next((b for b in (pg.get("buttons") or [])
                   if re.search(r"log ?in|sign ?in|continue|submit", b, re.I)), "")
    return {"user": ident, "password": pw, "submit": submit}


def login_settle(lang="py", ind=""):
    """Wait until a SPA sign-in actually leaves the login page.

    `wait_for_load_state("load")` alone is not enough — many apps keep you on
    /login until a XHR finishes, then client-route to the app. Hosted runs were
    racing that redirect: flow `goto(BASE)` hit the marketing shell and every
    authenticated click timed out."""
    a = "await " if lang == "py" else "await "
    if lang == "py":
        # r-string inside generated source: leave the login/sign-in path
        pred = r'lambda u: not re.search(r"/(login|sign[-_]?in)(/|$|\?)", u, re.I)'
        return "\n".join([
            f"{ind}try:",
            f"{ind}    {a}page.wait_for_url({pred}, timeout=20000)",
            f"{ind}except Exception:",
            f"{ind}    pass",
            f"{ind}{a}page.wait_for_timeout(800)",
        ])
    return "\n".join([
        f"{ind}await page.waitForURL(u => !/\\/(login|sign[-_]?in)(\\/|$|\\?)/i.test(u), "
        f"{{ timeout: 20000 }}).catch(() => {{}});",
        f"{ind}await page.waitForTimeout(800);",
    ])


def ensure_login_settle(steps, lang="py"):
    """Append a post-sign-in settle to stored/custom login steps when missing."""
    text = (steps or "").rstrip()
    if not text:
        return text
    if "wait_for_url" in text or "waitForURL" in text or "wait_for_function" in text:
        return text
    return text + "\n" + login_settle(lang=lang)


def login_prelude(login_url, pg, lang="py", ind=""):
    """Generated login steps to run BEFORE a flow: open the login page, fill the
    credentials from {{SPECREEL_USER}}/{{SPECREEL_PASSWORD}}, submit, settle.

    Credentials are placeholders substituted at run time — they are never written
    into a scaffold, a config file, or a repo. The demo trims these steps (the
    login URL goes into setup_urls), so a viewer sees the app, not the sign-in.
    """
    got = find_login_fields(pg or {})
    a = "await " if lang == "py" else "await "
    q = json.dumps
    out = [f"{ind}{a}page.goto({q(login_url)})"]
    if got["user"]:
        out.append(f"{ind}{a}{_locator_expr(got['user'], lang)}"
                   f'.fill("{{{{SPECREEL_USER}}}}")')
    if got["password"]:
        out.append(f"{ind}{a}{_locator_expr(got['password'], lang)}"
                   f'.fill("{{{{SPECREEL_PASSWORD}}}}")')
    if not (got["user"] and got["password"]):
        mark = "#" if lang == "py" else "//"
        out.append(f"{ind}{mark} TODO: login fields not auto-detected — set them here")
    if got["submit"]:
        nm = q(got["submit"])
        out.append(f'{ind}{a}page.get_by_role("button", name={nm}).first.click()'
                   if lang == "py" else
                   f'{ind}{a}page.getByRole("button", {{ name: {nm} }}).first().click()')
    elif got["password"]:
        out.append(f'{ind}{a}{_locator_expr(got["password"], lang)}.press("Enter")')
    # Prefer URL-leave settle over load — SPA auth often finishes after "load".
    settle = login_settle(lang=lang, ind=ind)
    out.extend(settle.splitlines())
    return "\n".join(out)


_LOGIN_URL_RE = re.compile(r"/(login|signin|sign-in|auth|account/login|users/sign_in)\b", re.I)


def detect_login_wall(pages):
    """Does this site require signing in to see anything worth demoing?

    Returns {"needed": bool, "login_url": str, "reason": str}. Called after a
    crawl so onboarding can ask for credentials instead of handing back a
    gallery of the marketing shell (or nothing at all).

    Signals, strongest first: pages that landed on a login URL, a password field
    on the page we were given, and a page whose only real control is a sign-in.
    """
    if not pages:
        return {"needed": False, "login_url": "", "reason": ""}
    login_pages = [p for p in pages if _LOGIN_URL_RE.search(p.get("url", ""))]
    with_pw = [p for p in pages
               if any(f.get("type") == "password"
                      for f in (p.get("inputs") or []))
               or any(f.get("type") == "password"
                      for fm in (p.get("forms") or []) for f in fm.get("fields") or [])]
    first = pages[0]
    landed_on_login = bool(_LOGIN_URL_RE.search(first.get("url", "")))
    url = ""
    if with_pw:
        url = with_pw[0].get("url", "")
    elif login_pages:
        url = login_pages[0].get("url", "")
    else:
        # no password field crawled, but a prominent "Log in" link is a hint of
        # a gated app — only report it when there's little else to demo
        for p in pages:
            for l in p.get("links") or []:
                if re.fullmatch(r"\s*(log ?in|sign ?in)\s*", l.get("text", ""), re.I):
                    url = l.get("href", "")
                    break
            if url:
                break
    if landed_on_login and with_pw:
        return {"needed": True, "login_url": first.get("url", ""),
                "reason": "that URL is a sign-in page"}
    if with_pw and len(pages) <= 2:
        return {"needed": True, "login_url": url,
                "reason": "the pages we could reach are behind a sign-in"}
    if with_pw:
        return {"needed": False, "login_url": url,
                "reason": "a sign-in page was found — adding credentials would let "
                          "Specreel demo the logged-in app too"}
    return {"needed": False, "login_url": url, "reason": ""}


def page_labels(pg, limit=40):
    """The clickable things that ACTUALLY exist on a page — button text first,
    then links.

    Carried onto every flow so the plain-English resolver can only reference
    controls that are really there. Buttons come first: a 14-slot budget filled
    by sidebar nav alone used to hide page CTAs ("From library", "Run scorecard"),
    and the model invented near-miss names that timed out at run time.
    """
    out, seen = [], set()
    ordered = (list(pg.get("buttons", []))
               + [l.get("text", "") for l in pg.get("links", [])])
    for t in ordered:
        t = " ".join((t or "").split())[:60]
        # responsive nav renders the label twice (desktop+mobile spans) and the
        # texts concatenate: "Sign inSign in" -> "Sign in"
        h = len(t) // 2
        if h and t[:h] == t[h:]:
            t = t[:h]
        if t and len(t) > 1 and t.lower() not in seen and not t.lower().startswith("loading"):
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return out


def recommend_flows(pages, limit=8):
    """Turn a crawled site map into ranked candidate flows (forms > search > nav)."""
    forms, searches, navs = [], [], []
    for pg in pages:
        head = pg["headings"][0] if pg["headings"] else ""
        short = pg["title"].split("—")[0].split("|")[0].strip() or pg["title"]
        labels = page_labels(pg)
        roles = pg.get("roles") or {}
        meta = {"labels": labels, "roles": roles,
                "buttons": list(pg.get("buttons") or [])[:24]}
        for fm in pg["forms"]:
            fields = [x for x in fm["fields"] if _fillable(x)]
            if fields:
                # "Fill the …" (not "Fill & submit"): the scaffold deliberately leaves
                # the submit click as a TODO — the title shouldn't claim otherwise.
                forms.append({"type": "form", "score": 3, "url": pg["url"],
                              "title": f"Fill the {short} form", "heading": head,
                              "page_title": short, "fields": fields[:6], **meta})
        s = next((x for x in pg["inputs"]
                  if x["type"] == "search" or "search" in (x["name"] + x["placeholder"]).lower()), None)
        if s:
            searches.append({"type": "search", "score": 2, "url": pg["url"],
                             "title": f"Search on {short}", "heading": head,
                             "page_title": short, "fields": [s], **meta})
        elif not pg["forms"] and head:
            navs.append({"type": "nav", "score": 1, "url": pg["url"],
                         "title": f"Open {short}", "heading": head,
                         "page_title": short, "fields": [], **meta})
    def sig(fl):
        return "|".join(f'{f.get("name", "")}:{f.get("placeholder", "")}'
                        for f in fl["fields"])

    # a search box lives inside a <form>, so the same field would surface twice —
    # once as "Fill the X form" and once as "Search on X" (it did, on 4 of 6 real
    # sites tested). The search variant is the better demo; drop the form twin.
    search_sigs = {sig(s) for s in searches}
    forms = [f for f in forms if sig(f) not in search_sigs]

    seen, out = set(), []
    for fl in forms + searches + navs:
        if fl["type"] in ("form", "search"):
            # the same widget (a footer newsletter form, a header search box) shows
            # up on every crawled page — dedupe by its field signature, not by URL,
            # so a site-wide widget yields ONE flow instead of one per page.
            key = fl["type"] + "|" + sig(fl)
        else:
            key = fl["url"] + fl["type"]
        if key in seen:
            continue
        seen.add(key)
        out.append(fl)
    return out[:limit]


def _sample_value(f):
    n = (f["name"] + " " + f["placeholder"] + " " + f["id"]).lower()
    t = f["type"]
    if t == "email" or "email" in n:
        return "demo@example.com"
    if t == "password" or "password" in n:
        return "demo-pass-123"
    if t == "search" or "search" in n:
        return "test"
    if t == "tel" or "phone" in n:
        return "+15555550100"
    if t == "number":
        return "1"
    if t == "url":
        return "https://example.com"
    if "name" in n:
        return "Demo User"
    return "Sample text"


def _locator_expr(f, lang):
    """Playwright locator for a field, in the given language."""
    if f["placeholder"]:
        ph = json.dumps(f["placeholder"])   # safely quoted for both languages
        return (f'page.get_by_placeholder({ph})' if lang == "py"
                else f'page.getByPlaceholder({ph})')
    sel = f'#{f["id"]}' if f["id"] else (f'[name="{f["name"]}"]' if f["name"] else "input")
    return f'page.locator({json.dumps(sel)})'


def slugify_flow(title, used):
    base = slugify(title)
    slug, n = base, 1
    while slug in used:
        n += 1
        slug = f"{base}-{n}"
    used.add(slug)
    return slug


def scaffold_script(flows, base, lang="py", login_steps=""):
    """Emit a runnable Playwright script that traces each flow into
    test-results/<slug>/trace.zip — exactly what `specreel <dir>` consumes."""
    used = set()
    for fl in flows:
        fl["slug"] = slugify_flow(fl["title"], used)

    def _settle(ind):
        """Trailing settle: give the page a beat so the trace's last frames show
        the RESULT, not the click mid-flight.

        Prefer `load` + a short pause over `networkidle`. Apps with open
        websockets / analytics (chat, live scorecards) never go network-idle, so
        a 4s networkidle wait times out, leaves a failed step in the trace
        (demo shows FAIL even though the script caught it), and still doesn't
        wait for the real outcome."""
        if lang == "py":
            return [f'{ind}await page.wait_for_load_state("load")',
                    f"{ind}await page.wait_for_timeout(1200)"]
        return [f"{ind}await page.waitForLoadState('load');",
                f"{ind}await page.waitForTimeout(1200);"]

    def steps(fl, ind):
        pre = ""
        if login_steps:
            # every flow starts from a fresh browser context, so the sign-in has
            # to run per flow — not once at the top of the file
            pre = "\n".join((ind + ln) if ln.strip() else ln
                            for ln in str(login_steps).strip("\n").splitlines()) + "\n"
        if fl.get("code"):
            # an NL-authored flow: use its body verbatim (re-indented) + a settle
            lines = str(fl["code"]).strip("\n").splitlines() or ["pass"]
            body = [(ind + ln) if ln.strip() else ln for ln in lines]
            return pre + "\n".join(body + _settle(ind))
        out = []
        url = fl["url"]
        out.append(f'{ind}await page.goto({json.dumps(url)})')
        # Landing assert: prefer the page <title> — stable across content updates.
        # (Asserting the first heading breaks when e.g. the newest blog post changes.)
        pt = (fl.get("page_title") or "").strip()
        hd = (fl.get("heading") or "").strip()
        if pt:
            pat = json.dumps(re.escape(pt))     # safely quoted for both languages
            if lang == "py":
                out.append(f'{ind}await expect(page).to_have_title(re.compile({pat}, re.I))')
            else:
                out.append(f'{ind}await expect(page).toHaveTitle(new RegExp({pat}, "i"))')
        elif hd and len(hd) <= 45:              # short headings only — long ones are content
            hdq = json.dumps(hd)
            out.append(f'{ind}# TODO: confirm a stable element on the page')
            if lang == "py":
                out.append(f'{ind}await expect(page.get_by_text({hdq}).first).to_be_visible()')
            else:
                out.append(f'{ind}await expect(page.getByText({hdq}).first()).toBeVisible()')
        # a beat after each field: fill() is instant but the screencast paints at
        # ~10fps — without pacing, a step's frame predates its own typing
        beat = (f"{ind}await page.wait_for_timeout(350)" if lang == "py"
                else f"{ind}await page.waitForTimeout(350);")
        for f in fl.get("fields", []):
            loc = _locator_expr(f, lang)
            val = _sample_value(f)
            if fl["type"] == "search":
                out.append(f'{ind}await {loc}.fill({json.dumps(val)})')
                out.append(beat)
                out.append(f'{ind}await {loc}.press("Enter")')
            else:
                out.append(f'{ind}await {loc}.fill({json.dumps(val)})')
                out.append(beat)
        if fl["type"] == "nav":
            # scroll so the demo actually shows the page, not just its header
            out.append(f"{ind}await page.mouse.wheel(0, 600)")
        else:
            out.append(f'{ind}# TODO: click submit / assert the result you care about')
        out += _settle(ind)
        return pre + "\n".join(out)

    if lang == "py":
        body = ["\"\"\"Auto-scaffolded by `specreel recommend`. Edit the TODOs, then:",
                "    python this_file.py && specreel test-results -o site --bundle",
                "Each flow writes test-results/<slug>/trace.zip for specreel to render.\"\"\"",
                "import asyncio, os, re",
                "from playwright.async_api import async_playwright, expect", "",
                f'BASE = os.environ.get("BASE_URL", "{base}")', "", "FLOWS = []", "",
                "def flow(fn): FLOWS.append((fn.__name__, fn)); return fn", ""]
        for fl in flows:
            body.append(f'@flow  # {fl["title"]}')
            body.append(f'async def {fl["slug"].replace("-", "_")}(page):')
            body.append(steps(fl, "    "))
            body.append("")
        body += [
            "async def main():",
            "    async with async_playwright() as p:",
            "        browser = await p.chromium.launch()",
            "        for name, fn in FLOWS:",
            "            ctx = await browser.new_context(viewport={'width':1280,'height':800})",
            "            await ctx.tracing.start(screenshots=True, snapshots=True, sources=True)",
            "            page = await ctx.new_page()",
            "            page.set_default_timeout(10000)   # fail fast, not 30s/step",
            "            try:",
            "                await fn(page)",
            "                print('ok  ', name)",
            "            except Exception as e:",
            "                print('FAIL', name, '-', str(e).splitlines()[0])",
            "            d = os.path.join('test-results', name.replace('_','-'))",
            "            os.makedirs(d, exist_ok=True)",
            "            await ctx.tracing.stop(path=os.path.join(d, 'trace.zip'))",
            "            await ctx.close()",
            "        await browser.close()", "",
            "asyncio.run(main())", ""]
        return "\n".join(body)

    # JS / TS
    body = ["// Auto-scaffolded by `specreel recommend`. Edit the TODOs, then:",
            "//   node this_file.mjs && specreel test-results -o site --bundle",
            "import { chromium, expect } from '@playwright/test';",
            "import fs from 'node:fs';", "",
            f"const BASE = process.env.BASE_URL || '{base}';", "", "const FLOWS = ["]
    for fl in flows:
        body.append(f"  // {fl['title']}")
        body.append(f"  ['{fl['slug']}', async (page) => {{")
        body.append(steps(fl, "    "))
        body.append("  }],")
    body += ["];", "",
             "for (const [name, fn] of FLOWS) {",
             "  const browser = await chromium.launch();",
             "  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });",
             "  await ctx.tracing.start({ screenshots: true, snapshots: true, sources: true });",
             "  const page = await ctx.newPage();",
             "  page.setDefaultTimeout(10000);  // fail fast, not 30s/step",
             "  try { await fn(page); console.log('ok  ', name); }",
             "  catch (e) { console.log('FAIL', name, '-', String(e).split('\\n')[0]); }",
             "  fs.mkdirSync(`test-results/${name}`, { recursive: true });",
             "  await ctx.tracing.stop({ path: `test-results/${name}/trace.zip` });",
             "  await browser.close();",
             "}", ""]
    return "\n".join(body)


def ai_curate(flows, api_key, model=DEFAULT_AI_MODEL, product="", instruction="", allow_drop=False):
    """Ask the model to re-rank and rename the candidate flows. With `instruction`,
    follow a plain-English refinement ("focus on checkout, drop marketing pages");
    `allow_drop` lets it omit flows (otherwise dropped flows are appended back).
    Best-effort — falls back to the input order on any failure. Never invents flows."""
    schema = {"type": "object", "properties": {"order": {"type": "array", "items": {
        "type": "object", "properties": {
            "index": {"type": "integer"}, "title": {"type": "string"}},
        "required": ["index", "title"], "additionalProperties": False}}},
        "required": ["order"], "additionalProperties": False}
    listing = [{"index": i, "type": f["type"], "title": f["title"],
                "url": f.get("url", ""), "heading": f.get("heading", "")}
               for i, f in enumerate(flows)]
    sysmsg = ("You curate a list of candidate product-demo flows discovered by crawling "
              "an app. Reorder them most-demo-worthy first and give each a crisp, "
              f"customer-facing title{' for ' + product if product else ''}. Use ONLY the "
              "given flows (by index) — never invent new ones. "
              + ("Follow the user's instruction; you MAY omit flows it asks to drop. "
                 if instruction else "Return every index exactly once."))
    user = {"flows": listing}
    if instruction:
        user["instruction"] = instruction
    body = {"model": model, "max_tokens": 1024,
            "system": [{"type": "text", "text": sysmsg, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    try:
        resp = _anthropic_messages(body, api_key)
        text = next((b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"), "")
        order = json.loads(text).get("order", [])
        out, seen = [], set()
        for it in order:
            i = it.get("index")
            if isinstance(i, int) and 0 <= i < len(flows) and i not in seen:
                seen.add(i)
                fl = dict(flows[i])
                if it.get("title"):
                    fl["title"] = it["title"]
                out.append(fl)
        if not allow_drop:
            for i, fl in enumerate(flows):    # append any the model dropped
                if i not in seen:
                    out.append(fl)
        return out if out else flows
    except Exception as e:
        sys.stderr.write(f"specreel: AI curation failed ({type(e).__name__}: {e})\n")
        return flows


NL_FLOW_SYSTEM = (
    "You write a single Playwright flow body from a plain-English description, for a "
    "demo/test of a web app. You receive the app's discovered pages and form fields as "
    "context. Output ONLY the statements that go INSIDE an async flow function whose one "
    "argument is `page` — navigations, fills, clicks, and one assertion. Rules:\n"
    "- Navigate with `page.goto(BASE + \"/path\")` (BASE is a predefined variable).\n"
    "- Prefer get_by_role / get_by_label / get_by_placeholder, grounded in the given "
    "fields/buttons; fall back to a CSS selector only if needed.\n"
    "- NEVER invent the text of a link or button. Each page lists its real "
    "`clickable` text — use one of those strings verbatim. If the thing the user "
    "described isn't in that list (e.g. they say 'read more' but the page only has "
    "post titles), pick the closest real entry, or target the element structurally "
    "(e.g. the first article's link: page.locator(\"article a\").first) and leave a "
    "TODO. A name that isn't on the page makes the flow fail on the first click.\n"
    "- NEVER invent URL paths. Navigate only to paths that appear in known_pages "
    "(or BASE itself). If the user describes a feature whose page isn't listed, "
    "goto the closest real URL and click a real `clickable` entry from there — "
    "do not guess `/audience-scorecards`-style paths that aren't in the crawl.\n"
    "- Prefer `page.goto(BASE + \"/known/path\")` when known_pages already has the "
    "destination (e.g. Audience Scorecards → /simulations). Don't open BASE and "
    "then click a sidebar label to reach a page whose URL you already know — "
    "direct goto is faster and survives collapsed nav / slow SPA mounts.\n"
    "- Do NOT emit a sign-in sequence (goto /login, fill email/password, Sign In). "
    "The runner already signs in before your body runs. Start at the feature URL.\n"
    "- Always chain `.first` (or `.nth(i)`) before `.click()` / `.fill()` on "
    "get_by_role / get_by_placeholder — duplicate accessible names are common and "
    "strict mode will fail the run.\n"
    "- Respect each page's `roles` counts: if roles.checkbox is 0, do NOT call "
    "get_by_role(\"checkbox\") — use a real button from `clickable` (e.g. \"Add all\"). "
    "If roles.option is 0, do not click role=option; open a search/picker from "
    "clickable/placeholders and pick the first result with a structural locator.\n"
    "- Button `name=` strings must match `clickable` verbatim (case-sensitive "
    "preferred). \"Select from library\" is wrong if the page only has \"From library\".\n"
    "- If known_pages is empty, output ONLY `await page.goto(BASE)` plus a "
    "`# TODO: no crawled pages — sign in / re-scan before regenerating` comment "
    "and a title assertion. Inventing the rest is worse than a stub.\n"
    "- Prefer `.first` on a locator that could match several elements — an "
    "ambiguous locator is a strict-mode failure, not a passing demo.\n"
    "- A field with \"visible\": false EXISTS but is hidden (typically a search "
    "box inside a closed modal). You MUST click the control that reveals it "
    "first — use its \"opened_by\" name, e.g. page.get_by_role(\"button\", name=\"Search\").first.click() — then fill it. "
    "Filling a hidden field just times out.\n"
    "- For content that only appears AFTER an action (search results, a dropdown) "
    "you cannot know its markup — do NOT invent class names like \".search-results "
    "a\". Prefer a role-based locator scoped to the container, e.g. "
    "page.locator(\"[role=dialog], .modal\").get_by_role(\"link\").first, and leave "
    "a TODO so the author can tighten it.\n"
    "- To scroll, use `page.mouse.wheel(0, N)` (repeat for further) or "
    "`page.keyboard.press(\"End\")` — NEVER page.evaluate(window.scrollTo…): it "
    "throws while a navigation is in flight, and it renders no visible step in "
    "the demo. Scrolling via the mouse shows up as a real captioned step.\n"
    "- After a click that navigates or kicks off work (submit, run, send), wait "
    "for the OUTCOME — a URL change, a new message, a results heading — not "
    "`networkidle` (SPAs with websockets never go idle and will just time out) "
    "and not a blind short sleep. Example: "
    "`await page.wait_for_url(re.compile(r\\\"results\\\"), timeout=60000)` or "
    "`await page.get_by_text(re.compile(r\\\"score|reply\\\", re.I)).first.wait_for(timeout=60000)`. "
    "Then pause ~1s so the demo captures the finished frame.\n"
    "- After a click that navigates, let the destination settle before acting on "
    "it (e.g. `await page.wait_for_load_state(\"load\")`).\n"
    "- Add exactly one assertion (expect(...)) for what should be true at the end. "
    "Assert something STABLE (the page title, a permanent UI element) — never "
    "content that changes over time, like the newest post's headline.\n"
    "- Leave a `# TODO` (py) or `// TODO` (js) where you have to guess.\n"
    "- If `variables` are given, use the literal placeholder `{{NAME}}` for those "
    "values (e.g. .fill(\"{{EMAIL}}\")) — they're substituted at run time.\n"
    "- No function signature, no imports, no markdown fences — just the body lines, "
    "in the requested language, using `await`.")

NL_FLOW_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "code": {"type": "string"}},
    "required": ["title", "code"], "additionalProperties": False}


def normalize_flow_code(code, lang):
    """Make a generated flow body safe to run, or return "" if it can't be.

    A model asked for Python occasionally emits a `//` comment (JS habit), which
    is a SyntaxError — the whole flow then dies at import time with a message
    that has nothing to do with the user's app. Normalize the obvious slips and
    then actually compile Python before we hand it back."""
    if not code:
        return ""
    lines = code.strip().splitlines()
    if lang == "py":
        fixed = []
        for ln in lines:
            stripped = ln.lstrip()
            if stripped.startswith("//"):        # JS comment in Python
                ln = ln[:len(ln) - len(stripped)] + "#" + stripped[2:]
            # JS regex literal in a matcher: to_have_title(/Dylan Roy/i)
            # -> to_have_title(re.compile("Dylan Roy", re.I))   (scaffold imports re)
            ln = re.sub(r'\(/(.+?)/([a-z]*)\)',
                        lambda m: '(re.compile("%s"%s))' % (
                            m.group(1).replace('\\', '\\\\').replace('"', '\\"'),
                            ", re.I" if "i" in m.group(2) else ""),
                        ln)
            fixed.append(ln)
        lines = fixed
        text = "\n".join(lines)
        text = strip_embedded_login(text)
        text = ensure_locator_first(text)
        body = "\n".join("    " + ln for ln in text.splitlines()) or "    pass"
        try:
            compile("async def _f(page, BASE, expect):\n" + body, "<flow>", "exec")
        except SyntaxError as e:
            sys.stderr.write(f"nl_flow: generated python didn't compile ({e.msg} "
                             f"at line {e.lineno}) — discarding\n")
            return ""
        return text
    else:
        lines = [("//" + ln.lstrip()[1:] if ln.lstrip().startswith("#") else ln)
                 for ln in lines]
        text = ensure_locator_first("\n".join(lines))
        return text


def strip_embedded_login(code):
    """Drop a duplicated sign-in block the model sometimes prepends.

    Hosted runs already inject login_prelude; a second goto(/login) + fill with
    {{EMAIL}} (or a concatenated 'Sign In Signing in...' button) races setup trim
    and leaves a blank failing demo."""
    lines = code.splitlines()
    saw_login = any(re.search(r'goto\([^)]*login', ln, re.I) for ln in lines)
    if not saw_login:
        return code
    for i, ln in enumerate(lines):
        # first real app navigation after the login detour
        if re.search(r'goto\(\s*BASE\s*(\+\s*[\'"]/(?!login)|\)\s*$)', ln):
            return "\n".join(lines[i:]).lstrip("\n")
        if re.search(r'goto\(\s*BASE\s*\+\s*[\'"]/', ln) and not re.search(r'login', ln, re.I):
            return "\n".join(lines[i:]).lstrip("\n")
    return code


def ensure_locator_first(code):
    """Playwright strict mode: get_by_role('button', name='X').click() fails when
    two matches exist (Kumkuat's 'Pick an Audience'). Prefer .first unless the
    chain already picks nth/first/last."""
    def fix(m):
        s = m.group(0)
        if re.search(r"\.(first|nth|last)\s*\(", s):
            return s
        return re.sub(r"\.(click|fill|check|press)\(\s*$", r".first.\1(", s)

    return re.sub(
        r"(?:page\.)?(?:get_by_role|get_by_placeholder|get_by_label|get_by_text|"
        r"getByRole|getByPlaceholder|getByLabel|getByText)\("
        r"[^\n]*?\.(?:click|fill|check|press)\(\s*",
        fix, code)


# get_by_role("button", name="From library")  /  getByRole('button', { name: '…' })
_ROLE_NAME_RE = re.compile(
    r"""get_by_role\(\s*['"](\w+)['"]\s*,\s*name\s*=\s*['"]([^'"]+)['"]"""
    r"""|getByRole\(\s*['"](\w+)['"]\s*,\s*\{\s*name:\s*['"]([^'"]+)['"]""",
    re.I)
_ROLE_BARE_RE = re.compile(
    r"""get_by_role\(\s*['"](checkbox|option|switch)['"]\s*\)"""
    r"""|getByRole\(\s*['"](checkbox|option|switch)['"]\s*\)""",
    re.I)
_STRICT_CLICK_RE = re.compile(
    r"""(get_by_role|get_by_placeholder|get_by_label|get_by_text)\([^;\n]+?\)\s*\.(click|fill|check|press)\("""
)


def lint_flow_against_context(code, context_flows):
    """Flag invented locators before we save a flow that will fail on first run.

    Returns a list of short issue strings (empty = looks grounded). Conservative:
    unknown button names and checkbox/option use when the crawl saw none."""
    if not code:
        return ["empty code"]
    known = set()
    role_totals = {"checkbox": 0, "option": 0, "listitem": 0}
    for f in context_flows or []:
        for t in (f.get("labels") or []) + (f.get("buttons") or []) + (f.get("clickable") or []):
            if t:
                known.add(str(t).casefold())
        for field in f.get("fields") or []:
            ph = field.get("placeholder") or ""
            if ph:
                known.add(ph.casefold())
            ob = field.get("opened_by") or ""
            if ob:
                known.add(ob.casefold())
        roles = f.get("roles") or {}
        for k in role_totals:
            try:
                role_totals[k] += int(roles.get(k) or 0)
            except (TypeError, ValueError):
                pass
    issues = []
    for m in _ROLE_NAME_RE.finditer(code):
        role = (m.group(1) or m.group(3) or "").lower()
        name = m.group(2) or m.group(4) or ""
        if not name:
            continue
        if re.search(r"(?i)sign\s*in\s*signing", name):
            issues.append(f'button name={name!r} looks like a loading-state concat — use "Sign In"')
            continue
        key = name.casefold()
        if known and key not in known:
            # Playwright name matching is substring-ish toward the accessible
            # name, so "library" can match "From library". The reverse is how
            # the model invents failures: "Select from library" when only
            # "From library" exists — reject that direction.
            if not any(key == k or key in k for k in known):
                issues.append(f'{role} name={name!r} is not in clickable/buttons — '
                              f'use a verbatim label from the crawl')
    for m in _ROLE_BARE_RE.finditer(code):
        role = (m.group(1) or m.group(2) or "").lower()
        if role_totals.get(role, 0) == 0 and (context_flows or []):
            issues.append(f'get_by_role("{role}") used but crawled pages have '
                          f'0 {role}s — pick a real button/placeholder instead')
    # bare get_by_role(...).click() without .first/.nth → strict-mode landmine
    for m in _STRICT_CLICK_RE.finditer(code):
        chunk = m.group(0)
        if not re.search(r"\.(first|nth|last)\s*\(", chunk):
            issues.append("get_by_*().click/fill without .first/.nth — "
                          "add .first to avoid strict-mode double matches")
            break
    if re.search(r'goto\([^)]*login', code, re.I) and re.search(r'goto\(\s*BASE', code):
        issues.append("flow embeds its own /login — hosted runs already sign in; "
                      "start at the feature URL instead")
    return issues


def _nl_context_payload(context_flows):
    return [{"title": f.get("title"), "url": f.get("url"), "type": f.get("type"),
             "fields": [{"name": x.get("name"), "placeholder": x.get("placeholder"),
                         "type": x.get("type"),
                         "visible": x.get("visible", True),
                         "opened_by": x.get("opened_by", "")}
                        for x in f.get("fields", [])],
             "clickable": f.get("labels") or f.get("clickable") or [],
             "buttons": f.get("buttons") or [],
             "roles": f.get("roles") or {}}
            for f in (context_flows or [])]


def nl_flow(prompt, context_flows, base, lang, api_key, model=DEFAULT_AI_MODEL, timeout=150,
            var_names=None):
    """Turn a plain-English description into a runnable flow, grounded in the crawled
    pages. `var_names` are project variables the model should reference as {{NAME}}.
    Returns a flow dict with a verbatim `code` body (for scaffold_script), or None on
    failure. BYO-key; never raises.

    After the first draft, lint against crawled clickables/roles and give the model
    one repair pass — catches invented 'Select from library' / checkbox clicks before
    they ship as a broken scenario."""
    ctx = _nl_context_payload(context_flows)
    payload = {"description": prompt, "language": ("python" if lang == "py" else "javascript"),
               "base_url": base, "known_pages": ctx}
    if var_names:
        payload["variables"] = list(var_names)

    def _ask(user_payload):
        body = {"model": model, "max_tokens": 800,
                "system": [{"type": "text", "text": NL_FLOW_SYSTEM,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user",
                              "content": json.dumps(user_payload, ensure_ascii=False)}],
                "output_config": {"format": {"type": "json_schema", "schema": NL_FLOW_SCHEMA}}}
        resp = _anthropic_messages(body, api_key, timeout=timeout)
        text = next((b.get("text", "") for b in resp.get("content", [])
                     if b.get("type") == "text"), "")
        obj = json.loads(text) if text else {}
        code = normalize_flow_code((obj.get("code") or "").strip(), lang)
        return obj, code

    try:
        obj, code = _ask(payload)
        if not code:
            return None
        issues = lint_flow_against_context(code, context_flows)
        if issues:
            repair = dict(payload)
            repair["lint_errors"] = issues
            repair["draft_code"] = code
            repair["fix_instruction"] = (
                "The draft fails grounding checks. Rewrite the code so every "
                "get_by_role name appears verbatim in known_pages clickable/buttons, "
                "and do not use checkbox/option when roles counts are 0. Prefer "
                "existing CTAs like 'Add all' / 'From library' / 'Pick an Audience'.")
            obj2, code2 = _ask(repair)
            if code2:
                issues2 = lint_flow_against_context(code2, context_flows)
                if len(issues2) <= len(issues):
                    obj, code, issues = obj2, code2, issues2
            if issues:
                sys.stderr.write("nl_flow: still ungrounded after repair — "
                                 + "; ".join(issues[:3]) + "\n")
        return {"title": obj.get("title") or prompt[:48], "type": "custom",
                "url": base, "heading": "", "fields": [], "code": code, "nl": True}
    except Exception as e:
        sys.stderr.write(f"nl_flow: failed ({type(e).__name__}: {e})\n")
        return None


def assemble_scaffold(spec, api_key=None):
    """Build a scaffold from a wizard spec: {base_url, lang, items} where each item
    is a discovered flow dict, or {nl_prompt, title?} for a plain-English flow.
    NL items are resolved via nl_flow (grounded in the discovered items)."""
    base = spec.get("base_url", "")
    lang = "js" if spec.get("lang") == "js" else "py"
    items = spec.get("items", [])
    discovered = [it for it in items if not it.get("nl_prompt")]
    resolved = []
    for it in items:
        if it.get("nl_prompt"):
            fl = nl_flow(it["nl_prompt"], discovered, base, lang, api_key) if api_key else None
            if not fl:                       # graceful stub so the slug/flow still appears
                tip = "# TODO" if lang == "py" else "// TODO"
                fl = {"title": it.get("title") or it["nl_prompt"][:48], "type": "custom",
                      "url": base, "heading": "", "fields": [],
                      "code": f'{tip}: {it["nl_prompt"]}\nawait page.goto(BASE)'}
            resolved.append(fl)
        else:
            resolved.append(it)
    return scaffold_script(resolved, base, lang=lang,
                           login_steps=spec.get("login_steps", ""))


def scaffold_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py scaffold",
                                 description="assemble a Playwright scaffold from a wizard spec JSON")
    ap.add_argument("--specs", required=True, help="JSON file: {base_url, lang, items[]}")
    ap.add_argument("--api-key", default=None, help="Anthropic key for any nl_prompt items")
    ap.add_argument("-o", "--out", default=None, help="output file (default stdout)")
    args = ap.parse_args(argv)
    spec = json.load(open(args.specs, encoding="utf-8"))
    out = assemble_scaffold(spec, resolve_api_key(args.api_key) or None)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        sys.stderr.write(f"  scaffold -> {args.out}\n")
    else:
        print(out)
    return 0


def curate_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py curate",
                                 description="reorder/rename/drop discovered flows by a plain-English instruction")
    ap.add_argument("--flows", required=True, help="JSON file: a list of flow dicts")
    ap.add_argument("--instruction", default="", help="plain-English refinement")
    ap.add_argument("--product", default="")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args(argv)
    key = resolve_api_key(args.api_key)
    flows = json.load(open(args.flows, encoding="utf-8"))
    if not key:
        sys.stderr.write("curate: set ANTHROPIC_API_KEY (or --api-key)\n")
        print(json.dumps(flows))
        return 1
    out = ai_curate(flows, key, product=args.product, instruction=args.instruction,
                    allow_drop=bool(args.instruction))
    print(json.dumps(out))
    return 0


EXPLAIN_SYSTEM = (
    "You explain why an automated browser-test run failed, for a developer. Given the "
    "failing flow titles and the run log, write 2–4 short sentences: the most likely "
    "cause and what to check next. Be concrete and grounded in the log — never invent "
    "details. Plain text, no markdown headings.")


def explain_failure(failing, log, api_key, model=DEFAULT_AI_MODEL, timeout=60):
    """Draft a short failure analysis from failing flow titles + a log tail. Returns
    the text, or '' on any failure (graceful)."""
    try:
        payload = {"failing_flows": failing or [], "log_tail": (log or "")[-3000:]}
        body = {"model": model, "max_tokens": 400,
                "system": [{"type": "text", "text": EXPLAIN_SYSTEM,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
        resp = _anthropic_messages(body, api_key, timeout=timeout)
        return next((b.get("text", "") for b in resp.get("content", [])
                     if b.get("type") == "text"), "").strip()
    except Exception as e:
        sys.stderr.write(f"explain: failed ({type(e).__name__}: {e})\n")
        return ""


def explain_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py explain",
                                 description="AI explanation of a failed run (BYO-key)")
    ap.add_argument("--input", required=True, help="JSON file: {failing:[titles], log:'...'}")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args(argv)
    key = resolve_api_key(args.api_key)
    if not key:
        sys.stderr.write("explain: set ANTHROPIC_API_KEY (or --api-key)\n")
        return 1
    d = json.load(open(args.input, encoding="utf-8"))
    out = explain_failure(d.get("failing", []), d.get("log", ""), key)
    if not out:
        return 1
    print(out)
    return 0


def nlflow_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py nlflow",
                                 description="resolve one plain-English scenario into a runnable flow dict (JSON)")
    ap.add_argument("--desc", required=True, help="the plain-English scenario")
    ap.add_argument("--url", default="", help="base URL")
    ap.add_argument("--lang", choices=["py", "js"], default="py")
    ap.add_argument("--context", default=None, help="JSON file: context flow dicts to ground selectors")
    ap.add_argument("--vars", default="", help="comma-separated variable names (used as {{NAME}})")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args(argv)
    key = resolve_api_key(args.api_key)
    if not key:
        print(json.dumps({"error": "no ANTHROPIC_API_KEY"}))
        return 1
    ctx = []
    if args.context and os.path.exists(args.context):
        try:
            ctx = json.load(open(args.context))
        except Exception:
            ctx = []
    var_names = [v.strip() for v in args.vars.split(",") if v.strip()]
    fl = nl_flow(args.desc, ctx, args.url, args.lang, key, var_names=var_names)
    if not fl:
        print(json.dumps({"error": "generation failed"}))
        return 1
    print(json.dumps(fl))
    return 0


def recommend_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py recommend",
                                 description="crawl a running app and suggest + scaffold demo flows")
    ap.add_argument("url", help="base URL of the running app, e.g. http://localhost:3000")
    ap.add_argument("--max", type=int, default=12, help="max pages to crawl")
    ap.add_argument("--lang", choices=["py", "js"], default=None, help="scaffold language")
    ap.add_argument("-o", "--out", default=None, help="scaffold file (default specreel_flows.<lang>)")
    ap.add_argument("--browser", action="store_true",
                    help="render pages with Playwright first (for client-rendered SPAs)")
    ap.add_argument("--wait", type=int, default=1200,
                    help="ms to wait for SPA render in --browser mode")
    ap.add_argument("--ai", action="store_true", help="curate/rename with AI (BYO key)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--cookie", default=None,
                    help="Cookie header for a logged-in crawl, e.g. 'session=abc; csrf=xyz'")
    ap.add_argument("--login-url", default=None,
                    help="sign in at this URL before crawling (implies --browser). "
                         "Credentials come from SPECREEL_LOGIN_USER / "
                         "SPECREEL_LOGIN_PASSWORD — never passed on the command line, "
                         "so they stay out of your shell history and process list. "
                         "Use a DEDICATED TEST ACCOUNT: whatever it can see may end "
                         "up in a shared demo.")
    ap.add_argument("--header", action="append", default=[], metavar="K:V",
                    help="extra request header (repeatable), e.g. --header 'Authorization: Bearer …'")
    ap.add_argument("--json", action="store_true",
                    help="emit suggestions + scaffold as JSON on stdout (for tooling); "
                         "progress/errors go to stderr and no file is written")
    args = ap.parse_args(argv)

    headers = {}
    if args.cookie:
        headers["Cookie"] = args.cookie
    for h in args.header:
        k, _, v = h.partition(":")
        if k.strip() and v.strip():
            headers[k.strip()] = v.strip()
    headers = headers or None

    # In --json mode stdout is reserved for the JSON payload; humanize to stderr.
    say = (lambda *a: sys.stderr.write(" ".join(str(x) for x in a) + "\n")) if args.json else print

    def fail(msg):
        if args.json:
            print(json.dumps({"error": msg, "url": args.url, "flows": []}))
        sys.stderr.write(f"recommend: {msg}\n")
        return 1

    lang = args.lang or ("js" if os.path.exists("package.json") else "py")
    login = None
    if args.login_url:
        pw = os.environ.get("SPECREEL_LOGIN_PASSWORD", "")
        if not pw:
            return fail("--login-url needs SPECREEL_LOGIN_PASSWORD in the environment "
                        "(and usually SPECREEL_LOGIN_USER)")
        login = {"url": args.login_url, "user": os.environ.get("SPECREEL_LOGIN_USER", ""),
                 "password": pw}
        args.browser = True          # signing in requires a real browser
    mode = "browser-rendered" if args.browser else "server HTML"
    say(f"  crawling {args.url} (max {args.max} pages, {mode}"
        f"{', authenticated' if login else ''}) ...")
    try:
        pages = (crawl_browser(args.url, max_pages=args.max, wait_ms=args.wait,
                               headers=headers, login=login)
                 if args.browser else crawl(args.url, max_pages=args.max, headers=headers))
    except CrawlFailed as e:
        return fail(f"couldn't load that URL — {e}")
    if not pages:
        return fail("no pages fetched — is the app running?" +
                    ("" if args.browser else " (a JS-rendered SPA? try --browser)"))
    flows = recommend_flows(pages)
    if not flows:
        return fail("no candidate flows found" +
                    ("" if args.browser else " — client-rendered? re-run with --browser"))
    if args.ai:
        key = resolve_api_key(args.api_key)
        if key:
            flows = ai_curate(flows, key)
        else:
            sys.stderr.write("recommend: --ai set but no ANTHROPIC_API_KEY — using crawl order\n")

    # if we found an auth form but weren't given a session, nudge toward --cookie
    if not headers and any(re.search(r"log ?in|sign ?in|sign ?up|register",
                                     f"{fl.get('title', '')} {fl.get('url', '')}", re.I)
                           for fl in flows):
        say('  tip: found a login/signup form — for logged-in pages re-run with '
            '--cookie "session=…" (copy it from your browser devtools).')

    scaffold = scaffold_script(flows, args.url, lang=lang)   # also assigns fl["slug"]
    if args.json:
        # full flow dicts (incl. fields/heading) so tooling can re-assemble subsets
        print(json.dumps({
            "url": args.url, "lang": lang, "pages": len(pages),
            "flows": [{"type": fl["type"], "title": fl["title"], "url": fl["url"],
                       "slug": fl.get("slug") or slugify(fl["title"]) or fl["type"],
                       "heading": fl.get("heading", ""),
                       "page_title": fl.get("page_title", ""),   # keeps the stable
                       # to_have_title assert alive through the cloud wizard path
                       "fields": fl.get("fields", []),
                       # real link/button text — the NL resolver must only name
                       # controls that exist, or it invents them and the flow fails
                       "labels": fl.get("labels", []),
                       "n_fields": len(fl.get("fields", []))} for fl in flows],
            "scaffold": scaffold,
            # does this app need credentials to show anything worth demoing?
            "login": detect_login_wall(pages),
        }))
        return 0

    say(f"  found {len(pages)} pages → {len(flows)} suggested flows:\n")
    for i, fl in enumerate(flows, 1):
        nf = len(fl.get("fields", []))
        extra = f" · {nf} field(s)" if nf else ""
        say(f"    {i}. [{fl['type']}] {fl['title']}  ({fl['url']}{extra})")
    out = args.out or f"specreel_flows.{lang}"
    with open(out, "w") as f:
        f.write(scaffold)
    say(f"\n  scaffolded -> {out}")
    runner = "python " + out if lang == "py" else "node " + out
    say(f"  edit the TODOs, then:  {runner} && specreel test-results -o site --bundle")
    return 0


def diagnose(path):
    """Preflight a traces dir/zip before rendering. Returns (results, ok) where
    results is a list of (level, message) with level in {ok, warn, fail}, and ok is
    False if anything is fatal (no traces / a corrupt zip). Pure + testable."""
    results = []
    ok = True
    if path.endswith(".zip") and os.path.isfile(path):
        traces = [path]
    elif os.path.isdir(path):
        traces = find_traces(path)
    elif os.path.exists(path):
        return [("fail", f"{path} is not a trace.zip or a directory")], False
    else:
        return [("fail", f"path not found: {path}")], False

    if not traces:
        return [("fail", f"no trace.zip found under {path} — run your Playwright tests "
                 "with tracing on (JS: use:{{ trace:'on' }} · py: --tracing on)")], False

    total_frames = 0
    for tp in traces[:50]:
        rel = os.path.relpath(tp, path) if os.path.isdir(path) else os.path.basename(tp)
        tmp = tempfile.mkdtemp(prefix="specreel_doc_")
        try:
            with zipfile.ZipFile(tp) as z:
                z.extractall(tmp)
            events = load_events(tmp)
            if not events:
                results.append(("fail", f"{rel}: not a Playwright trace (no .trace inside)"))
                ok = False
                continue
            steps, frames = build_steps(events)
            steps = coalesce_steps(steps)
            n_steps = len(steps)
            total_frames += len(frames)
            if not frames:
                results.append(("warn", f"{rel}: {n_steps} steps but 0 screencast frames — "
                                "enable screenshots in tracing or the demo has no visuals"))
            elif n_steps == 0:
                results.append(("warn", f"{rel}: no demo-worthy steps (only plumbing?)"))
            else:
                # same check the render uses: is each end of the flow captured?
                cov = capture_coverage(steps, frames)
                if cov["issues"]:
                    for msg in cov["issues"]:
                        results.append(("warn", f"{rel}: {n_steps} steps · {len(frames)} "
                                        f"frames — {msg}"))
                else:
                    results.append(("ok", f"{rel}: {n_steps} steps · {len(frames)} frames"))
        except zipfile.BadZipFile:
            results.append(("fail", f"{rel}: not a valid zip file"))
            ok = False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    cfg = find_config(None, path if os.path.isdir(path) else os.path.dirname(path) or ".")
    if cfg:
        try:
            load_config(cfg)
            results.append(("ok", f"config: {os.path.basename(cfg)} parses"))
        except Exception as e:
            results.append(("warn", f"config: {os.path.basename(cfg)} failed to parse ({e})"))
    return results, ok


def doctor_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py doctor",
                                 description="preflight a traces dir/zip before rendering")
    ap.add_argument("path", nargs="?", default="test-results",
                    help="a trace.zip or a directory of them (default test-results)")
    ap.add_argument("--mp4", action="store_true", help="also check MP4 export readiness")
    args = ap.parse_args(argv)
    sym = {"ok": "✓", "warn": "⚠", "fail": "✕"}
    results, ok = diagnose(args.path)
    print(f"  specreel doctor — {args.path}\n")
    for level, msg in results:
        print(f"   {sym[level]} {msg}")
    if args.mp4:
        try:
            import PIL  # noqa: F401
            pil = True
        except Exception:
            pil = False
        ff = bool(shutil.which("ffmpeg"))
        print(f"   {sym['ok'] if pil else sym['warn']} MP4: Pillow "
              f"{'present' if pil else 'missing (pip install Pillow)'}")
        print(f"   {sym['ok'] if ff else sym['warn']} MP4: ffmpeg "
              f"{'present' if ff else 'missing (install ffmpeg)'}")
    n_warn = sum(1 for lvl, _ in results if lvl == "warn")
    n_fail = sum(1 for lvl, _ in results if lvl == "fail")
    print(f"\n  {'ready to render' if ok and not n_warn else ('ready (with warnings)' if ok else 'issues found')}"
          f" — {n_fail} error(s), {n_warn} warning(s)")
    return 0 if ok else 1


def init_main(argv):
    ap = argparse.ArgumentParser(prog="specreel.py init",
                                 description="scaffold a specreel.yml from a traces dir")
    ap.add_argument("traces", help="directory containing trace.zip files")
    ap.add_argument("-o", "--out", default="specreel.yml")
    args = ap.parse_args(argv)
    return init_config(args.traces, args.out)


def loginsteps_main(argv):
    """Emit generated sign-in steps for a login page (placeholders, no secrets).
    The cloud calls this when a user saves a login and leaves the steps blank."""
    ap = argparse.ArgumentParser(prog="specreel.py loginsteps",
                                 description="generate Playwright sign-in steps for a login page")
    ap.add_argument("--url", required=True, help="the login page URL")
    ap.add_argument("--lang", default="py", choices=["py", "js"])
    ap.add_argument("--wait", type=int, default=1500, help="render wait (ms)")
    args = ap.parse_args(argv)
    pages = crawl_browser(args.url, max_pages=1, wait_ms=args.wait)
    if not pages:
        sys.stderr.write("loginsteps: could not load the login page\n")
        return 1
    got = find_login_fields(pages[0])
    if not got["password"]:
        if got["user"]:
            sys.stderr.write(
                "loginsteps: this page asks for an identifier but no password — it "
                "looks like a magic-link / passwordless sign-in, which a username "
                "and password can't automate. Use a session cookie instead "
                "(recommend --cookie), or point at a password login page.\n")
        else:
            sys.stderr.write("loginsteps: no sign-in fields found on that page — "
                             "is that the login URL?\n")
        return 1
    print(login_prelude(args.url, pages[0], lang=args.lang))
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "loginsteps":
        sys.exit(loginsteps_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "publish":
        sys.exit(publish_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        sys.exit(init_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        sys.exit(summary_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "notes":
        sys.exit(notes_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        sys.exit(doctor_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "recommend":
        sys.exit(recommend_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "scaffold":
        sys.exit(scaffold_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "nlflow":
        sys.exit(nlflow_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "explain":
        sys.exit(explain_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "curate":
        sys.exit(curate_main(sys.argv[2:]))
    ap = argparse.ArgumentParser(description="Playwright trace.zip -> watchable demo")
    ap.add_argument("trace", help="a trace.zip, or a directory to scan (batch mode)")
    ap.add_argument("-o", "--out", default="specreel-out")
    ap.add_argument("--title", default=None, help="title (single-trace mode only)")
    ap.add_argument("--mp4", action="store_true")
    ap.add_argument("--config", default=None,
                    help="path to specreel.yml (batch mode; auto-discovered otherwise)")
    ap.add_argument("--ai", action="store_true",
                    help="opt-in AI narration (needs ANTHROPIC_API_KEY; BYO-key, metered)")
    ap.add_argument("--ai-model", default=DEFAULT_AI_MODEL,
                    help=f"model for --ai narration (default {DEFAULT_AI_MODEL})")
    ap.add_argument("--api-key", default=None,
                    help="Anthropic API key (else read from ANTHROPIC_API_KEY)")
    ap.add_argument("--bundle", action="store_true",
                    help="also emit a single self-contained gallery.html (batch mode)")
    ap.add_argument("--theme", default=None, choices=["dark", "light"],
                    help="color theme (default dark; or set theme: in specreel.yml)")
    ap.add_argument("--notify", default=None,
                    help="Slack incoming-webhook URL to post a build summary to (BYO)")
    ap.add_argument("--url", default="",
                    help="public gallery URL to include in the notification/links")
    ap.add_argument("--voice", nargs="?", const=DEFAULT_TTS_VOICE, default=None,
                    metavar="NAME",
                    help="studio voiceover: pre-render neural TTS narration per step "
                         f"(BYO OPENAI_API_KEY; default voice '{DEFAULT_TTS_VOICE}')")
    ap.add_argument("--tts-model", default=DEFAULT_TTS_MODEL,
                    help=f"TTS model for --voice (default {DEFAULT_TTS_MODEL}; "
                         "tts-1 is cheaper, tts-1-hd higher fidelity)")
    ap.add_argument("--tts-key", default=None,
                    help="OpenAI API key for --voice (else read from OPENAI_API_KEY)")
    ap.add_argument("--tts-instructions", default=None, metavar="TEXT",
                    help="delivery notes for the voiceover narrator (gpt- TTS models "
                         "only, e.g. 'upbeat, brisk'); or tts_instructions: in "
                         "specreel.yml. Clips cache in ~/.cache/specreel/tts — "
                         "override with SPECREEL_TTS_CACHE=<dir>, disable with =off")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any flow's first/last step wasn't captured "
                         "(a truncated demo — for CI, like a failing test)")
    ap.add_argument("--quality", default=None, choices=list(QUALITY_FRAMES),
                    help="motion quality: how many screencast frames each step embeds "
                         "(high=real motion & biggest files [default], medium=lighter, "
                         "low=one still per step & smallest). Or quality: in specreel.yml")
    ap.add_argument("--gif", action="store_true",
                    help="also export demo.gif (needs ffmpeg) — for READMEs and PRs")
    ap.add_argument("--showcase", action="store_true",
                    help="also emit showcase/ — a curated, customer-facing gallery "
                         "containing only flows marked public: true that are passing "
                         "(or showcase: true in specreel.yml; batch mode)")
    args = ap.parse_args()

    if os.path.isdir(args.trace):
        sys.exit(generate_gallery(args.trace, args.out, want_mp4=args.mp4,
                                  config_path=args.config, ai=args.ai,
                                  api_key=args.api_key, ai_model=args.ai_model,
                                  bundle=args.bundle, theme=args.theme,
                                  notify=args.notify, public_url=args.url,
                                  voice=args.voice, tts_model=args.tts_model,
                                  tts_key=args.tts_key,
                                  tts_instructions=args.tts_instructions,
                                  strict=args.strict,
                                  quality=args.quality, want_gif=args.gif,
                                  showcase=args.showcase))

    api_key = resolve_api_key(args.api_key)
    if args.ai and not api_key:
        sys.stderr.write("specreel: --ai set but ANTHROPIC_API_KEY is empty — "
                         "rendering literal captions (no narration)\n")
    voice, tts_key = args.voice, resolve_tts_key(args.tts_key)
    if voice and not tts_key:
        sys.stderr.write("specreel: --voice set but OPENAI_API_KEY is empty — "
                         "using the browser voice instead\n")
        voice = None
    stats = generate_demo(args.trace, args.out, title=args.title, want_mp4=args.mp4,
                          ai=args.ai, api_key=api_key, ai_model=args.ai_model,
                          theme=args.theme or "dark", voice=voice,
                          tts_model=args.tts_model, tts_key=tts_key,
                          tts_instructions=args.tts_instructions,
                          quality=args.quality or DEFAULT_QUALITY, want_gif=args.gif)
    if args.strict and stats.get("capture", {}).get("issues"):
        sys.exit(1)


# Shared player narration engine, injected as __TTSJS__ into HTML_TEMPLATE and
# BUNDLE_TEMPLATE (they had drifted apart as two copies). Expects the host script
# to define STEPS, cur, timer and the #tts / #voice controls; provides tts state,
# _sched (autoplay pacing that waits for narration), playStep and speak.
TTS_JS = r"""let tts=false,_ttsAuto=true,_voice=null;
function _clipMs(s){return (s&&s.imgs&&s.imgs.length>1)?s.dts.reduce((a,b)=>a+b,0):0;}
function _hasVO(){try{return STEPS.some(s=>s&&s.audio);}catch(e){return false;}}
/* prefer a natural/neural browser voice over the robotic system default */
function _vscore(v){var n=(v.name||'').toLowerCase();if(!/^en(-|_|\b|$)/i.test(v.lang||''))return -1;var s=0;
if(/natural|neural/.test(n))s+=10;if(/siri/.test(n))s+=9;if(/google/.test(n))s+=6;
if(/premium|enhanced/.test(n))s+=5;if(/(samantha|aria|jenny|guy|ava|allison|emma|nova|zira|libby|sonia)/.test(n))s+=3;
if(/en-us/i.test(v.lang||''))s+=2;if(v.localService===false)s+=1;
if(/zarvox|albert|bells|cellos|trinoids|whisper|bad news|good news|boing|bahh|bubbles|wobble|deranged|hysterical|fred|junior|ralph|kathy|organ|e-?speak|compact|novelty/.test(n))s-=20;
return s;}
function _vsorted(){try{var vs=(speechSynthesis.getVoices()||[]).filter(function(v){return _vscore(v)>-1;});vs.sort(function(a,b){return _vscore(b)-_vscore(a);});return vs;}catch(e){return [];}}
function _fillVoices(){try{var sel=document.getElementById('voice'),vs=_vsorted();
if(!sel)return;
/* with studio narration the browser-voice picker is noise — hide it */
sel.style.display=(_hasVO()||!vs.length)?'none':'';
if(!vs.length)return;
var want=null;try{want=localStorage.getItem('specreel-voice');}catch(e){}
if(want){for(var i=0;i<vs.length;i++)if(vs[i].name===want){_voice=vs[i];break;}}
if(!_voice)_voice=vs[0];
sel.innerHTML='';vs.forEach(function(v){var o=document.createElement('option');o.value=v.name;o.textContent=v.name.replace(/\s*\(.*\)$/,'').slice(0,26);if(_voice&&v.name===_voice.name)o.selected=true;sel.appendChild(o);});
sel.onchange=function(){for(var i=0;i<vs.length;i++){if(vs[i].name===sel.value){_voice=vs[i];break;}}try{localStorage.setItem('specreel-voice',sel.value);}catch(e){}if(tts)playStep(cur);};}catch(e){}}
var _au=(window.Audio?new Audio():null),_next=null,_gen=0;
function _fire(g){if(g!=null&&g!==_gen)return;clearTimeout(timer);var f=_next;_next=null;if(f)f();}
if(_au)_au.onended=function(){var g=_gen;setTimeout(function(){_fire(g);},280);};
/* What the voice SAYS: the caption plus failure wording a listener can't see,
   with masked secrets (•••) and url schemes translated for the ear. */
function _spk(s){var t=(s&&s.caption)||'';
if(s&&s.failed)t='This step failed: '+t+(s.why?('. '+s.why):'');
return t.replace(/•+/g,'the hidden value').replace(/https?:\/\//g,'').replace(/\s+/g,' ');}
/* Autoplay without TTS uses step.dur (~1.4s). With TTS on, that cuts mid-sentence —
   wait for studio audio / utterance.onend, with a text-length fallback so a stuck
   speechSynthesis can't freeze the demo. */
function _speakMs(s){return Math.max(((s&&s.dur)||1.4)*1000,_clipMs(s||{})+700,900+_spk(s).length*58);}
function _sched(fn){var s=STEPS[cur];clearTimeout(timer);
var hold=Math.max(((s&&s.dur)||1.4)*1000,_clipMs(s||{})+700);
/* Last step: dwell a beat longer so the climax isn't covered by the outro. */
if(cur>=STEPS.length-1)hold+=1400;
if(tts){_next=fn;var g=_gen;timer=setTimeout(function(){if(_next===fn&&g===_gen){_next=null;fn();}},Math.min(Math.max(_speakMs(s),hold)+5000,30000));}
else{_next=null;timer=setTimeout(fn,hold);}}
function playStep(i){_gen++;try{if(_au)_au.pause();if(window.speechSynthesis)speechSynthesis.cancel();}catch(e){}
_updTTS();if(!tts)return;var s=STEPS[i];if(!s)return;
if(s.audio&&_au){try{_au.src=s.audio;var p=_au.play();if(p&&p.catch)p.catch(function(){speak(_spk(s));});}catch(e){speak(_spk(s));}}else{speak(_spk(s));}}
function speak(t){if(!tts||!window.speechSynthesis)return;try{speechSynthesis.cancel();if(!_voice)_voice=_vsorted()[0]||null;
var g=_gen,u=new SpeechSynthesisUtterance(t);if(_voice){u.voice=_voice;u.lang=_voice.lang;}u.rate=0.97;u.pitch=1.0;
u.onend=function(){setTimeout(function(){_fire(g);},280);};u.onerror=function(){_fire(g);};
/* Chrome can drop an utterance queued synchronously after cancel() — breathe first */
setTimeout(function(){if(g!==_gen||!tts)return;try{speechSynthesis.speak(u);}catch(e){_fire(g);}},60);}catch(e){_fire(_gen);}}
/* Paid studio narration announces itself on the toggle; the free voice is On/Off */
function _updTTS(){var b=document.getElementById('tts');if(!b)return;
b.textContent=tts?(_hasVO()?'🔊 Voiceover':'🔊 On'):'🔊 Off';
b.title=_hasVO()?'Studio voiceover narration':'Read steps aloud';}
/* A demo built with studio voiceover defaults to sound ON (it still only starts on
   the viewer's Play click); a stored viewer preference always wins. */
function _initTTS(){var st=null;try{st=localStorage.getItem('specreel-tts');}catch(e){}
if(st==='1'||st==='0'){tts=(st==='1');_ttsAuto=false;}
else if(_ttsAuto)tts=_hasVO();
_updTTS();_fillVoices();}
(function(){var b=document.getElementById('tts');if(!b)return;
b.onclick=function(){tts=!tts;_ttsAuto=false;try{localStorage.setItem('specreel-tts',tts?'1':'0');}catch(e){}
_updTTS();if(tts){playStep(cur);}else{try{if(_au)_au.pause();if(window.speechSynthesis)speechSynthesis.cancel();}catch(e){}}};})();
if(window.speechSynthesis)speechSynthesis.onvoiceschanged=function(){_fillVoices();};
_initTTS();"""


HTML_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
__OG__
<title>__TITLE__ — Specreel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
__THEMEVARS__
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;
background-image:radial-gradient(800px 420px at 88% -10%,rgba(95,241,155,.06),transparent 60%);
min-height:100vh;padding:24px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto}
.top{display:flex;align-items:baseline;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:20px}
.brand{font-family:var(--serif);font-size:28px}.brand .d{color:var(--green)}
.title{font-family:var(--serif);font-size:22px;color:var(--muted)}
.meta{color:var(--faint);font-size:12px}
.layout{display:grid;grid-template-columns:1fr 320px;gap:18px}
.browser{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#0a0b0d;position:relative}
.bbar{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
.bdots{display:flex;gap:5px}.bdots i{width:9px;height:9px;border-radius:50%;background:#2a2f34;display:block}
.burl{flex:1;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 10px;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:72%}
.stage{background:#0a0b0d;overflow:hidden;position:relative;cursor:pointer}
.stage.zoomed{cursor:grab}.stage.zoomed.grabbing{cursor:grabbing}
#shot{transition:transform .12s ease;transform-origin:0 0;will-change:transform}
.stage img{display:block;width:100%}
#cursor{position:absolute;width:14px;height:14px;border-radius:50%;background:rgba(95,241,155,.92);border:2px solid rgba(8,9,11,.75);box-shadow:0 1px 6px rgba(0,0,0,.5);transform:translate(-50%,-50%);left:50%;top:50%;transition:left .5s cubic-bezier(.4,0,.2,1),top .5s cubic-bezier(.4,0,.2,1),opacity .3s;pointer-events:none;z-index:5;opacity:0}
#cursor.vis{opacity:1}
#cursor.rip::after{content:'';position:absolute;inset:-4px;border-radius:50%;border:2px solid var(--green);animation:rip .6s ease-out forwards}
@keyframes rip{from{transform:scale(.6);opacity:1}to{transform:scale(2.6);opacity:0}}
.ovl{position:absolute;inset:0;background:rgba(8,9,11,.84);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:10}
.ovl.hidden{display:none}
.ovl .ocard{text-align:center;padding:36px 44px;max-width:80%}
.ovl .okick{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--green)}
.ovl h2{font-family:var(--serif);font-weight:400;font-size:30px;margin:10px 0 6px;color:var(--text)}
.ovl .osub{color:var(--muted);font-size:12.5px;margin-bottom:20px}
.ovl .obtn{font-family:var(--mono);background:var(--green);color:var(--ink);border:none;border-radius:9px;padding:11px 22px;font-weight:700;font-size:14px;cursor:pointer}
.ovl .olink{display:block;margin:12px auto 0;color:var(--faint);font-size:11.5px;cursor:pointer;background:none;border:none;font-family:var(--mono)}
.ovl .ostat{font-size:26px;color:var(--green)}.ovl .ostat.fail{color:var(--red)}
.cap{position:absolute;left:16px;right:16px;bottom:16px;background:rgba(8,9,11,.86);
border:1px solid var(--line);border-left:3px solid var(--green);border-radius:10px;padding:11px 14px;backdrop-filter:blur(6px)}
.cap.check{border-left-color:var(--blue)}.cap.fail{border-left-color:var(--red)}
.cap .k{font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--green)}
.cap.check .k{color:var(--blue)}.cap.fail .k{color:var(--red)}
.cap .c{font-size:15px;margin-top:3px}
.cap .lit{font-size:10.5px;margin-top:3px;color:var(--faint)}
.cap .why{font-size:12px;margin-top:6px;color:var(--red);display:none}
.cap.fail .why{display:block}
.row .why{font-size:11px;color:var(--red);margin-top:4px;line-height:1.3}
.controls{display:flex;align-items:center;gap:12px;margin-top:14px}
.controls button{font-family:var(--mono);background:var(--panel);color:var(--text);
border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
.controls button.play{background:var(--green);color:var(--ink);border:none;font-weight:700}
.bar{flex:1;height:5px;background:var(--line);border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--green),#9bf3c2);transition:width .2s}
.side{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;align-self:start;max-height:78vh;overflow-y:auto}
.side h3{font-size:11px;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);padding:14px 16px;border-bottom:1px solid var(--line)}
.row{display:flex;gap:10px;padding:10px 16px;border-bottom:1px solid #1b1e21;cursor:pointer}
.row:hover{background:#16191c}.row.on{background:#16191c;box-shadow:inset 2px 0 0 var(--green)}
.row .ix{color:var(--faint);font-size:11px;min-width:16px}
.row .t{font-size:12.5px;line-height:1.35}
.row .pill{font-size:9px;letter-spacing:.5px;text-transform:uppercase;border-radius:4px;padding:1px 5px;margin-top:4px;display:inline-block}
.pill.action{color:var(--green);background:rgba(95,241,155,.1)}
.pill.nav{color:#cbb6ff;background:rgba(190,160,255,.1)}
.pill.check{color:var(--blue);background:rgba(127,182,255,.1)}
.pill.fail{color:var(--red);background:rgba(255,122,122,.12)}
@media(max-width:860px){.layout{grid-template-columns:1fr}}
</style>__ANALYTICS__</head><body><div class="wrap">
<div class="top"><div><span class="brand">specreel<span class="d">.</span></span>
<span class="title">&nbsp;__TITLE__</span></div>
<div class="meta">__NACT__ actions · __NCHK__ checks · regenerated from trace</div></div>
<div class="layout">
<div><div class="browser">
<div class="bbar"><span class="bdots"><i></i><i></i><i></i></span><span class="burl" id="burl">&nbsp;</span></div>
<div class="stage"><img id="shot"><div id="cursor"></div><div class="cap" id="cap"><div class="k" id="k"></div><div class="c" id="c"></div><div class="lit" id="lit"></div><div class="why" id="why"></div></div></div>
<div class="ovl" id="intro"><div class="ocard"><div class="okick">Demo · generated from a real test</div><h2>__TITLE__</h2><div class="osub">__NACT__ actions · __NCHK__ checks</div><button class="obtn" id="ibtn">▶ Play demo</button><button class="olink" id="iskip">browse steps instead</button></div></div>
<div class="ovl hidden" id="outro"><div class="ocard"><div class="ostat" id="ostat">✓</div><h2 id="otitle">Flow verified</h2><div class="osub" id="osub">Generated from a passing test · __DATE__</div><button class="obtn" id="obtn">↺ Replay</button></div></div>
</div>
<div class="controls"><button id="prev">‹ Prev</button><button class="play" id="play">▶ Play</button>
<button id="next">Next ›</button><button id="tts" title="Read steps aloud">🔊 Off</button><select id="voice" title="Voice (your browser's built-in voices)" style="background:var(--panel);color:var(--muted);border:1px solid var(--line);border-radius:7px;padding:4px 6px;font-family:inherit;font-size:11px;max-width:140px"></select><div class="bar"><i id="prog"></i></div><span class="meta" id="counter"></span></div></div>
<div class="side"><h3>Steps</h3><div id="list"></div></div>
</div></div>
<script>
const STEPS=__STEPS__;let cur=0,playing=false,timer=null;
/* trace content (typed values, labels, titles) is untrusted — escape before innerHTML */
const esc=t=>String(t==null?'':t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const shot=document.getElementById('shot'),cap=document.getElementById('cap'),
k=document.getElementById('k'),c=document.getElementById('c'),lit=document.getElementById('lit'),why=document.getElementById('why'),
prog=document.getElementById('prog'),
counter=document.getElementById('counter'),list=document.getElementById('list'),
burl=document.getElementById('burl'),cursor=document.getElementById('cursor'),
intro=document.getElementById('intro'),outro=document.getElementById('outro');
let clipT=[];function clearClips(){clipT.forEach(clearTimeout);clipT=[];}
function clipMs(s){return (s.imgs&&s.imgs.length>1)?s.dts.reduce((a,b)=>a+b,0):0;}
function placeCursor(s,anim){if(s.px==null){cursor.classList.remove('vis','rip');return;}
cursor.style.left=s.px+'%';cursor.style.top=s.py+'%';cursor.classList.add('vis');cursor.classList.remove('rip');
if(anim){void cursor.offsetWidth;clipT.push(setTimeout(()=>cursor.classList.add('rip'),Math.max(380,clipMs(s)-150)));}}
STEPS.forEach((s,i)=>{const r=document.createElement('div');r.className='row';r.dataset.i=i;
const kind=s.failed?'fail':s.kind;
r.innerHTML=`<span class="ix">${String(i+1).padStart(2,'0')}</span><div><div class="t">${esc(s.caption)}</div><span class="pill ${kind}">${s.failed?'failed':s.kind}</span>${s.failed&&s.why?`<div class="why">${esc(s.why)}</div>`:''}</div>`;
r.onclick=()=>{stop();show(i)};list.appendChild(r);});
function show(i,anim){cur=i;const s=STEPS[i];clearClips();try{window.__srResetZoom&&window.__srResetZoom();}catch(e){}
if(anim&&s.imgs&&s.imgs.length>1){let t=0;for(let j=0;j<s.imgs.length;j++){t+=j?s.dts[j]:0;
(j2=>clipT.push(setTimeout(()=>{shot.src=s.imgs[j2];},t)))(j);}}
else if(s.img)shot.src=s.img;
if(s.url)burl.textContent=s.url;
placeCursor(s,!!anim);
cap.className='cap '+(s.failed?'fail':s.kind);
k.textContent=(s.failed?'FAILED · ':'')+(s.kind==='check'?'CHECK':(s.kind==='nav'?'NAVIGATE':'ACTION'))+' '+(i+1);
c.textContent=s.caption;lit.textContent=s.lit||'';lit.style.display=s.lit?'block':'none';if(why)why.textContent=s.why||'';playStep(i);
prog.style.width=((i+1)/STEPS.length*100)+'%';
counter.textContent=(i+1)+' / '+STEPS.length;
[...list.children].forEach((r,j)=>r.classList.toggle('on',j===i));
list.children[i].scrollIntoView({block:'nearest'});
try{window.__specreel={step:i+1,total:STEPS.length};}catch(e){}}
function next(anim){if(cur<STEPS.length-1)show(cur+1,anim);else stop();}
function adv(){if(cur>=STEPS.length-1){stop();showOutro();return;}next(true);if(playing)_sched(adv);}
function play(){if(playing){stop();return;}playing=true;document.getElementById('play').textContent='❚❚ Pause';
intro.classList.add('hidden');outro.classList.add('hidden');
show(cur>=STEPS.length-1?0:cur,true);_sched(adv);}
function showOutro(){const bad=STEPS.some(s=>s.failed);
document.getElementById('ostat').textContent=bad?'✕':'✓';
document.getElementById('ostat').className='ostat'+(bad?' fail':'');
document.getElementById('otitle').textContent=bad?'This flow is currently failing':'Flow verified';
document.getElementById('osub').textContent=(bad?'A step failed on the last run':'Generated from a passing test')+' · __DATE__';
outro.classList.remove('hidden');}
function stop(){playing=false;clearTimeout(timer);_next=null;_gen++;if(_au)_au.pause();if(window.speechSynthesis)speechSynthesis.cancel();document.getElementById('play').textContent='▶ Play';}
__TTSJS__
document.getElementById('next').onclick=()=>{stop();next();};
document.getElementById('prev').onclick=()=>{stop();if(cur>0)show(cur-1);};
document.getElementById('play').onclick=play;
document.onkeydown=e=>{if(e.key==='ArrowRight'){stop();next();}if(e.key==='ArrowLeft'){stop();if(cur>0)show(cur-1);}if(e.key===' '){e.preventDefault();play();}};
document.getElementById('ibtn').onclick=play;
document.getElementById('iskip').onclick=()=>intro.classList.add('hidden');
document.getElementById('obtn').onclick=()=>{outro.classList.add('hidden');show(0);play();};
intro.onclick=e=>{if(e.target===intro)intro.classList.add('hidden');};
outro.onclick=e=>{if(e.target===outro)outro.classList.add('hidden');};
if(STEPS.length)show(0);else{c.textContent='No demo-worthy steps in this trace.';intro.classList.add('hidden');}

/* Click = play/pause (what everyone expects of a player). Zoom is a DELIBERATE
   gesture: double-click toward a point, drag to pan, double-click to reset.
   Single-click used to zoom, so the natural "tap the video" instinct looked
   like the demo randomly zooming in on a step. */
(function(){var st=shot.parentElement,sc=1,ox=0,oy=0,drag=0,dbl=0;
function aT(){shot.style.transform='translate('+ox+'px,'+oy+'px) scale('+sc+')';}
function rs(){sc=1;ox=0;oy=0;shot.style.transformOrigin='0 0';aT();st.classList.remove('zoomed');}
window.__srResetZoom=rs;                     /* show() clears zoom on step change */
shot.addEventListener('load',function(){if(sc===1)rs();});  /* keep an intentional zoom while a clip plays */
st.addEventListener('click',function(e){
  if(drag||dbl)return;
  setTimeout(function(){if(!dbl)play();},220);   /* single click -> play/pause */
});
st.addEventListener('dblclick',function(e){
  dbl=1;setTimeout(function(){dbl=0;},420);
  if(sc===1){var r=st.getBoundingClientRect();
    shot.style.transformOrigin=((e.clientX-r.left)/r.width*100)+'% '+((e.clientY-r.top)/r.height*100)+'%';
    sc=2.2;ox=0;oy=0;st.classList.add('zoomed');aT();}
  else{rs();}
});
st.addEventListener('mousedown',function(e){if(sc===1)return;e.preventDefault();drag=0;var px=e.clientX,py=e.clientY,bx=ox,by=oy;st.classList.add('grabbing');
function mm(ev){ox=bx+(ev.clientX-px);oy=by+(ev.clientY-py);if(Math.abs(ev.clientX-px)+Math.abs(ev.clientY-py)>3)drag=1;aT();}
function mu(){document.removeEventListener('mousemove',mm);document.removeEventListener('mouseup',mu);st.classList.remove('grabbing');setTimeout(function(){drag=0;},0);}
document.addEventListener('mousemove',mm);document.addEventListener('mouseup',mu);});
})();
</script></body></html>"""


INDEX_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
__OG__
<title>Specreel — flows</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
__THEMEVARS__
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;
background-image:radial-gradient(900px 500px at 85% -10%,rgba(95,241,155,.06),transparent 60%),
radial-gradient(700px 400px at 10% 110%,rgba(127,182,255,.04),transparent 60%);
min-height:100vh;padding:22px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto}
.top{display:flex;align-items:center;justify-content:space-between;padding-bottom:18px;border-bottom:1px solid var(--line);margin-bottom:22px}
.brand{display:flex;align-items:baseline;gap:11px}
.logo{font-family:var(--serif);font-size:30px;line-height:1;color:var(--text)}
.logo .dot{color:var(--green);-webkit-text-fill-color:var(--green)}
.tag{color:var(--faint);font-size:11.5px;font-style:italic;font-family:var(--serif)}
.repo{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:12px}
.repo .branch{color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:3px 9px;font-size:11.5px}
.repo .build{display:inline-flex;align-items:center;gap:7px;color:var(--green);font-size:11.5px}
.repo .build.fail{color:var(--red)}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2.2s infinite}
.build.fail .pulse{background:var(--red)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(95,241,155,.5)}70%{box-shadow:0 0 0 7px rgba(95,241,155,0)}100%{box-shadow:0 0 0 0 rgba(95,241,155,0)}}
.hero{margin-bottom:20px}
.kicker{color:var(--green);font-size:11px;text-transform:uppercase;letter-spacing:1.6px;margin-bottom:8px}
.hero h1{font-family:var(--serif);font-weight:400;font-size:34px;line-height:1.1}
.hero h1 em{color:var(--green);font-style:italic}
.hero p{color:var(--muted);font-size:12.5px;margin-top:7px;max-width:620px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.stat .n{font-size:21px;font-weight:700;letter-spacing:-.5px}
.stat .n.green{color:var(--green)}.stat .n.amber{color:var(--amber)}
.stat .l{color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:1px;margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{display:block;text-decoration:none;color:var(--text);background:var(--panel);
border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:.15s}
.card:hover{border-color:#3a4147;transform:translateY(-2px)}
.card.fail{border-left:3px solid var(--red)}.card.heal{border-left:3px solid var(--amber)}
.thumb{height:150px;background:#0a0b0d;background-size:cover;background-position:top center;border-bottom:1px solid var(--line-soft)}
.thumb.empty{display:flex;align-items:center;justify-content:center}
.cbody{padding:13px 15px}
.ctop{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.ct{font-size:13.5px;font-weight:500;line-height:1.3}
.src{font-size:9px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);border:1px solid var(--line);border-radius:5px;padding:1px 6px;margin-left:8px;vertical-align:middle}
.badges{display:flex;gap:6px;flex-shrink:0}
.pill{font-size:10px;letter-spacing:.4px;border-radius:999px;padding:3px 8px;border:1px solid transparent;white-space:nowrap}
.pill.demo{color:var(--blue);background:rgba(127,182,255,.08);border-color:rgba(127,182,255,.25)}
.pill.test{color:var(--green);background:var(--green-dim);border-color:rgba(95,241,155,.3)}
.pill.test.fail{color:var(--red);background:rgba(255,122,122,.08);border-color:rgba(255,122,122,.3)}
.pill.test.heal{color:var(--amber);background:rgba(255,196,107,.1);border-color:rgba(255,196,107,.35)}
.cmeta{display:flex;align-items:center;justify-content:space-between;margin-top:10px;font-size:11px}
.cmeta .fresh{color:var(--green)}.cmeta .stale{color:var(--amber)}.cmeta .dur{color:var(--faint)}
.cm{color:var(--faint);font-size:11px;margin-top:7px}
.foot{display:flex;align-items:center;justify-content:space-between;margin-top:22px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:11px}
.toolbar{display:flex;gap:9px;align-items:center;margin:20px 0 2px;flex-wrap:wrap}
.toolbar input{flex:1;min-width:200px;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:9px 13px;color:var(--text);font-family:inherit;font-size:13px}
.toolbar input:focus{outline:none;border-color:var(--green)}
.fbtn{font-size:12px;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:8px;padding:8px 12px;cursor:pointer;font-family:inherit}
.fbtn:hover{color:var(--text)}.fbtn.on{color:var(--ink);background:var(--green);border-color:var(--green)}
.card.hide{display:none}
.nomatch{display:none;color:var(--faint);font-size:13px;padding:26px 2px}
@media(max-width:880px){.stats{grid-template-columns:repeat(2,1fr)}.hero h1{font-size:27px}}
</style>__ANALYTICS__</head><body><div class="wrap">
<div class="top">
<div class="brand"><span class="logo">specreel<span class="dot">.</span></span>
<span class="tag">the spec is the reel</span></div>
<div class="repo">__REPOBAR__</div>
</div>
<div class="hero">__KICKER__<h1>Your tests just shipped a <em>demo</em>.</h1>
<p>Every flow below is one artifact — a Playwright spec <i>and</i> a narrated demo,
regenerated from the same run. The demo can't be older than your last green build.</p></div>
<div class="stats">
<div class="stat"><div class="n">__NFLOWS__</div><div class="l">Flows tracked</div></div>
<div class="stat"><div class="n green">__NFRESH__</div><div class="l">Demos fresh</div></div>
<div class="stat"><div class="n __FAILCLASS__">__NFAIL__</div><div class="l">Failing</div></div>
<div class="stat"><div class="n">__TOTALDUR__</div><div class="l">Total runtime</div></div>
</div>
<div class="toolbar" id="toolbar">
<input id="q" type="search" placeholder="Filter flows…" autocomplete="off">
<button class="fbtn on" data-f="all">All</button>
<button class="fbtn" data-f="fresh">Fresh</button>
<button class="fbtn" data-f="fail">Failing</button>
</div>
<div class="grid" id="grid">
__CARDS__
</div>
<div class="nomatch" id="nomatch">No flows match.</div>
<div class="foot"><span>regenerated from the latest Playwright run · publish via the specreel GitHub Action</span>
<span>demo + test · one source</span></div>
</div>
<script>
(function(){
  var q=document.getElementById('q'), grid=document.getElementById('grid'),
      cards=[].slice.call(grid.querySelectorAll('.card')),
      nomatch=document.getElementById('nomatch'), filt='all';
  function apply(){
    var term=(q.value||'').trim().toLowerCase(), shown=0;
    cards.forEach(function(c){
      var okT=!term||(c.getAttribute('data-title')||'').indexOf(term)>=0,
          s=c.getAttribute('data-status'),
          okF=filt==='all'||(filt==='fail'?s==='fail':s!=='fail');
      var show=okT&&okF; c.classList.toggle('hide',!show); if(show)shown++;
    });
    nomatch.style.display=shown?'none':'block';
  }
  q.addEventListener('input',apply);
  [].forEach.call(document.querySelectorAll('.fbtn'),function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('.fbtn').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on'); filt=b.getAttribute('data-f'); apply();
    });
  });
})();
</script>
</body></html>"""


# The curated, customer-facing gallery (out/showcase/): passing public flows only.
# No health stats, no test jargon — but provenance (build + verify date) stays:
# "these demos are re-verified every release" is the pitch, in the customer's brand.
SHOWCASE_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
__OG__
<title>__HEADLINE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
__THEMEVARS__
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;
background-image:radial-gradient(900px 500px at 85% -10%,var(--green-dim),transparent 60%);
min-height:100vh;padding:22px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto}
.top{display:flex;align-items:center;justify-content:space-between;padding-bottom:18px;border-bottom:1px solid var(--line);margin-bottom:22px}
.brand{display:flex;align-items:center;gap:10px}
.plogo{height:26px;width:auto;display:block}
.pname{font-family:var(--serif);font-size:24px;line-height:1}
.logo{font-family:var(--serif);font-size:30px;line-height:1;color:var(--text)}
.logo .dot{color:var(--green)}
.build{display:inline-flex;align-items:center;gap:7px;color:var(--green);font-size:11.5px}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(95,241,155,.5)}70%{box-shadow:0 0 0 7px rgba(95,241,155,0)}100%{box-shadow:0 0 0 0 rgba(95,241,155,0)}}
.hero{margin-bottom:20px}
.kicker{color:var(--green);font-size:11px;text-transform:uppercase;letter-spacing:1.6px;margin-bottom:8px}
.hero h1{font-family:var(--serif);font-weight:400;font-size:34px;line-height:1.1}
.hero p{color:var(--muted);font-size:12.5px;margin-top:7px;max-width:620px}
.toolbar{display:flex;gap:9px;align-items:center;margin:20px 0 16px}
.toolbar input{flex:1;min-width:200px;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:9px 13px;color:var(--text);font-family:inherit;font-size:13px}
.toolbar input:focus{outline:none;border-color:var(--green)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{display:block;text-decoration:none;color:var(--text);background:var(--panel);
border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:.15s}
.card:hover{border-color:var(--green);transform:translateY(-2px)}
.thumb{height:150px;background:#0a0b0d;background-size:cover;background-position:top center;border-bottom:1px solid var(--line-soft)}
.thumb.empty{display:flex;align-items:center;justify-content:center}
.cbody{padding:13px 15px}
.ct{font-size:13.5px;font-weight:500;line-height:1.3}
.cmeta{display:flex;align-items:center;justify-content:space-between;margin-top:9px;font-size:11px}
.cmeta .dur{color:var(--muted)}.cmeta .ok{color:var(--green)}
.card.hide{display:none}
.nomatch{display:none;color:var(--faint);font-size:13px;padding:26px 2px}
.foot{display:flex;align-items:center;justify-content:space-between;margin-top:22px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:11px}
@media(max-width:880px){.hero h1{font-size:27px}}
__ACCENT__
__CUSTOMCSS__
</style>__ANALYTICS__</head><body><div class="wrap">
<div class="top">
<div class="brand">__BRAND__</div>
<div>__PROOF__</div>
</div>
<div class="hero"><div class="kicker">__KICKER__</div><h1>__HEADLINE__</h1>
<p>__TAGLINE__</p></div>
<div class="toolbar"><input id="q" type="search" placeholder="Search demos…" autocomplete="off"></div>
<div class="grid" id="grid">
__CARDS__
</div>
<div class="nomatch" id="nomatch">No demos match.</div>
<div class="foot"><span>__COUNT__ · each generated from a real, passing product flow</span>
<span>verified __DATE__</span></div>
</div>
<script>
(function(){
  var q=document.getElementById('q'), grid=document.getElementById('grid'),
      cards=[].slice.call(grid.querySelectorAll('.card')),
      nomatch=document.getElementById('nomatch');
  q.addEventListener('input',function(){
    var term=(q.value||'').trim().toLowerCase(), shown=0;
    cards.forEach(function(c){
      var ok=!term||(c.getAttribute('data-title')||'').indexOf(term)>=0;
      c.classList.toggle('hide',!ok); if(ok)shown++;
    });
    nomatch.style.display=shown?'none':'block';
  });
})();
</script>
</body></html>"""


# One self-contained file: gallery + every flow's player, all data inlined, hash-routed.
BUNDLE_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
__OG__
<title>Specreel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
__THEMEVARS__
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;
background-image:radial-gradient(900px 500px at 85% -10%,rgba(95,241,155,.06),transparent 60%),
radial-gradient(700px 400px at 10% 110%,rgba(127,182,255,.04),transparent 60%);
min-height:100vh;padding:22px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto}
.top{display:flex;align-items:center;justify-content:space-between;padding-bottom:18px;border-bottom:1px solid var(--line);margin-bottom:22px}
.brand{display:flex;align-items:baseline;gap:11px}
.logo{font-family:var(--serif);font-size:30px;line-height:1;color:var(--text)}
.logo .dot{color:var(--green);-webkit-text-fill-color:var(--green)}
.tag{color:var(--faint);font-size:11.5px;font-style:italic;font-family:var(--serif)}
.repo{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:12px}
.repo .branch{color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:3px 9px;font-size:11.5px}
.repo .build{display:inline-flex;align-items:center;gap:7px;color:var(--green);font-size:11.5px}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(95,241,155,.5)}70%{box-shadow:0 0 0 7px rgba(95,241,155,0)}100%{box-shadow:0 0 0 0 rgba(95,241,155,0)}}
.view{display:none}.view.on{display:block}
.kicker{color:var(--green);font-size:11px;text-transform:uppercase;letter-spacing:1.6px;margin-bottom:8px}
.hero{margin-bottom:20px}.hero h1{font-family:var(--serif);font-weight:400;font-size:32px;line-height:1.1}
.hero h1 em{color:var(--green);font-style:italic}.hero p{color:var(--muted);font-size:12.5px;margin-top:7px;max-width:620px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:22px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.stat .n{font-size:21px;font-weight:700}.stat .n.green{color:var(--green)}.stat .n.amber{color:var(--amber)}
.stat .l{color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:1px;margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{display:block;cursor:pointer;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:.15s}
.card:hover{border-color:#3a4147;transform:translateY(-2px)}.card.fail{border-left:3px solid var(--red)}.card.heal{border-left:3px solid var(--amber)}
.thumb{height:150px;background:#0a0b0d;background-size:cover;background-position:top center;border-bottom:1px solid var(--line-soft)}
.cbody{padding:13px 15px}.ctop{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.ct{font-size:13.5px;font-weight:500;line-height:1.3}
.src{font-size:9px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);border:1px solid var(--line);border-radius:5px;padding:1px 6px;margin-left:8px}
.badges{display:flex;gap:6px;flex-shrink:0}
.pill{font-size:10px;letter-spacing:.4px;border-radius:999px;padding:3px 8px;border:1px solid transparent;white-space:nowrap}
.pill.demo{color:var(--blue);background:rgba(127,182,255,.08);border-color:rgba(127,182,255,.25)}
.pill.test{color:var(--green);background:var(--green-dim);border-color:rgba(95,241,155,.3)}
.pill.test.fail{color:var(--red);background:rgba(255,122,122,.08);border-color:rgba(255,122,122,.3)}
.pill.test.heal{color:var(--amber);background:rgba(255,196,107,.1);border-color:rgba(255,196,107,.35)}
.cmeta{display:flex;align-items:center;justify-content:space-between;margin-top:10px;font-size:11px}
.cmeta .fresh{color:var(--green)}.cmeta .stale{color:var(--amber)}.cmeta .dur{color:var(--faint)}
.cm{color:var(--faint);font-size:11px;margin-top:7px}
.foot{margin-top:22px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:11px}
.back{display:inline-block;color:var(--muted);font-size:12px;cursor:pointer;margin-bottom:14px}
.pa{margin-top:14px;background:var(--green);color:var(--ink);border:none;border-radius:8px;padding:9px 16px;font-family:var(--mono);font-weight:700;font-size:12px;cursor:pointer}
.back:hover{color:var(--text)}
.ptitle{font-family:var(--serif);font-size:22px;margin-bottom:14px}
.layout{display:grid;grid-template-columns:1fr 320px;gap:18px}
.stage{background:#0a0b0d;border:1px solid var(--line);border-radius:12px;overflow:hidden;position:relative;cursor:pointer}
.stage.zoomed{cursor:grab}.stage.zoomed.grabbing{cursor:grabbing}
#shot{transition:transform .12s ease;transform-origin:0 0;will-change:transform}
.stage img{display:block;width:100%}
.cap{position:absolute;left:16px;right:16px;bottom:16px;background:rgba(8,9,11,.86);border:1px solid var(--line);border-left:3px solid var(--green);border-radius:10px;padding:11px 14px;backdrop-filter:blur(6px)}
.cap.check{border-left-color:var(--blue)}.cap.fail{border-left-color:var(--red)}
.cap .k{font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--green)}
.cap.check .k{color:var(--blue)}.cap.fail .k{color:var(--red)}
.cap .c{font-size:15px;margin-top:3px}.cap .lit{font-size:10.5px;margin-top:3px;color:var(--faint)}
.cap .why{font-size:12px;margin-top:6px;color:var(--red);display:none}
.cap.fail .why{display:block}
.row .why{font-size:11px;color:var(--red);margin-top:4px;line-height:1.3}
.controls{display:flex;align-items:center;gap:12px;margin-top:14px}
.controls button{font-family:var(--mono);background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
.controls button.play{background:var(--green);color:var(--ink);border:none;font-weight:700}
.bar{flex:1;height:5px;background:var(--line);border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--green),#9bf3c2);transition:width .2s}
.meta{color:var(--faint);font-size:12px}
.side{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;align-self:start;max-height:78vh;overflow-y:auto}
.side h3{font-size:11px;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);padding:14px 16px;border-bottom:1px solid var(--line)}
.row{display:flex;gap:10px;padding:10px 16px;border-bottom:1px solid #1b1e21;cursor:pointer}
.row:hover{background:#16191c}.row.on{background:#16191c;box-shadow:inset 2px 0 0 var(--green)}
.row .ix{color:var(--faint);font-size:11px;min-width:16px}.row .t{font-size:12.5px;line-height:1.35}
.row .pill2{font-size:9px;letter-spacing:.5px;text-transform:uppercase;border-radius:4px;padding:1px 5px;margin-top:4px;display:inline-block}
.p2.action{color:var(--green);background:rgba(95,241,155,.1)}.p2.nav{color:#cbb6ff;background:rgba(190,160,255,.1)}
.p2.check{color:var(--blue);background:rgba(127,182,255,.1)}.p2.fail{color:var(--red);background:rgba(255,122,122,.12)}
@media(max-width:860px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:repeat(3,1fr)}}
</style>__ANALYTICS__</head><body><div class="wrap">
<div class="top"><div class="brand"><span class="logo">specreel<span class="dot">.</span></span>
<span class="tag">the spec is the reel</span></div><div class="repo" id="repo"></div></div>
<div class="view" id="gallery">
<div class="hero"><div class="kicker" id="kicker"></div><h1>Your tests just shipped a <em>demo</em>.</h1>
<p>One file, every flow — each a Playwright spec <i>and</i> a narrated demo. Click any flow to play it.</p>
<button class="pa" id="playall">▶ Play all flows</button></div>
<div class="stats" id="stats"></div><div class="grid" id="grid"></div>
<div class="foot">Each demo is generated from the same trace its test produced — so it can't be older than the last passing build.</div>
</div>
<div class="view" id="player">
<span class="back" id="back">‹ all flows</span><div class="ptitle" id="ptitle"></div>
<div class="layout"><div><div class="stage"><img id="shot"><div class="cap" id="cap"><div class="k" id="k"></div><div class="c" id="c"></div><div class="lit" id="lit"></div><div class="why" id="why"></div></div></div>
<div class="controls"><button id="prev">‹ Prev</button><button class="play" id="play">▶ Play</button>
<button id="next">Next ›</button><button id="tts" title="Read steps aloud">🔊 Off</button><select id="voice" title="Voice (your browser's built-in voices)" style="background:var(--panel);color:var(--muted);border:1px solid var(--line);border-radius:7px;padding:4px 6px;font-family:inherit;font-size:11px;max-width:140px"></select><div class="bar"><i id="prog"></i></div><span class="meta" id="counter"></span></div></div>
<div class="side"><h3>Steps</h3><div id="list"></div></div></div>
</div></div>
<script>
const DATA=__DATA__;const BY={};
const esc=t=>String(t==null?'':t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));DATA.flows.forEach(f=>BY[f.slug]=f);
const el=id=>document.getElementById(id);
// header
let rb='';if(DATA.repo)rb+='<span>'+esc(DATA.repo)+'</span>';if(DATA.branch)rb+='<span class="branch">⌥ '+esc(DATA.branch)+'</span>';
if(DATA.build)rb+='<span class="build"><span class="pulse"></span>build '+DATA.build+(DATA.stats.failing?' failing':' passing')+'</span>';
el('repo').innerHTML=rb||'<span class="branch">portable bundle</span>';
el('kicker').textContent=DATA.title||'';
el('stats').innerHTML=['<div class="stat"><div class="n">'+DATA.stats.flows+'</div><div class="l">Flows</div></div>',
'<div class="stat"><div class="n green">'+DATA.stats.fresh+'</div><div class="l">Fresh</div></div>',
'<div class="stat"><div class="n '+(DATA.stats.failing?'amber':'green')+'">'+DATA.stats.failing+'</div><div class="l">Failing</div></div>'].join('');
// gallery cards
el('grid').innerHTML=DATA.flows.map(f=>{
const status=f.failed?'<span class="pill test fail">✕ test failed</span>':(f.healed?'<span class="pill test heal">⟳ updated</span>':'<span class="pill test">✓ test</span>');
const fresh=f.failed?'<span class="stale">● re-rendered</span>':(f.healed?'<span class="stale">● updated this build</span>':'<span class="fresh">● fresh</span>');
const src=f.public?'<span class="src">public</span>':'';
const thumb=f.thumb?'style="background-image:url('+"'"+f.thumb+"'"+')"':'';
return '<div class="card'+(f.failed?' fail':(f.healed?' heal':''))+'" data-slug="'+f.slug+'"><div class="thumb" '+thumb+'></div>'+
'<div class="cbody"><div class="ctop"><span class="ct">'+esc(f.title)+src+'</span><span class="badges"><span class="pill demo">▶ demo</span>'+status+'</span></div>'+
'<div class="cmeta">'+fresh+'<span class="dur">▶ '+f.duration+'</span></div>'+
'<div class="cm">'+f.n_steps+' steps · '+f.n_actions+' actions · '+f.n_checks+' checks</div></div></div>';
}).join('');
[...el('grid').children].forEach(c=>c.onclick=()=>{location.hash='#'+c.dataset.slug;});
// player
let STEPS=[],cur=0,playing=false,timer=null,touring=false,tourIdx=0;
const shot=el('shot'),cap=el('cap'),k=el('k'),c=el('c'),lit=el('lit'),why=el('why'),prog=el('prog'),counter=el('counter'),list=el('list');
function buildList(){list.innerHTML='';STEPS.forEach((s,i)=>{const r=document.createElement('div');r.className='row';
const kind=s.failed?'fail':s.kind;
r.innerHTML='<span class="ix">'+String(i+1).padStart(2,'0')+'</span><div><div class="t">'+esc(s.caption)+'</div><span class="pill2 p2 '+kind+'">'+(s.failed?'failed':s.kind)+'</span>'+((s.failed&&s.why)?'<div class="why">'+esc(s.why)+'</div>':'')+'</div>';
r.onclick=()=>{stop();show(i)};list.appendChild(r);});}
function show(i){cur=i;const s=STEPS[i];if(s.img)shot.src=s.img;cap.className='cap '+(s.failed?'fail':s.kind);
k.textContent=(s.failed?'FAILED · ':'')+(s.kind==='check'?'CHECK':(s.kind==='nav'?'NAVIGATE':'ACTION'))+' '+(i+1);
c.textContent=s.caption;lit.textContent=s.lit||'';lit.style.display=s.lit?'block':'none';if(why)why.textContent=s.why||'';playStep(i);
prog.style.width=((i+1)/STEPS.length*100)+'%';counter.textContent=(i+1)+' / '+STEPS.length;
[...list.children].forEach((r,j)=>r.classList.toggle('on',j===i));list.children[i].scrollIntoView({block:'nearest'});}
function next(){if(cur<STEPS.length-1)show(cur+1);else stop();}
function adv(){next();if(playing&&cur<STEPS.length-1)_sched(adv);else if(playing)stop();}
function play(){if(playing){stop();return;}playing=true;el('play').textContent='❚❚ Pause';if(cur>=STEPS.length-1)show(0);else playStep(cur);_sched(adv);}
function stop(){playing=false;clearTimeout(timer);_next=null;_gen++;if(_au)_au.pause();if(window.speechSynthesis)speechSynthesis.cancel();el('play').textContent='▶ Play';}
__TTSJS__
el('next').onclick=()=>{stop();next();};el('prev').onclick=()=>{stop();if(cur>0)show(cur-1);};
el('play').onclick=()=>{if(touring){touring=false;stop();return;}play();};
el('back').onclick=()=>{location.hash='';};
document.onkeydown=e=>{if(el('player').classList.contains('on')){if(e.key==='ArrowRight'){stop();next();}if(e.key==='ArrowLeft'){stop();if(cur>0)show(cur-1);}if(e.key===' '){e.preventDefault();play();}if(e.key==='Escape')location.hash='';}};
function openFlow(f){stop();STEPS=f.steps||[];cur=0;_initTTS();el('ptitle').textContent=f.title;buildList();if(STEPS.length)show(0);
el('gallery').classList.remove('on');el('player').classList.add('on');window.scrollTo(0,0);}
// Play-all tour: chain every flow's player back-to-back (chaptering).
function tourSchedule(){clearTimeout(timer);_sched(tourTick);}
function tourTick(){if(cur<STEPS.length-1){show(cur+1);tourSchedule();}
else if(touring&&tourIdx<DATA.flows.length-1){tourOpen(tourIdx+1);}else{touring=false;stop();}}
function tourOpen(i){tourIdx=i;openFlow(DATA.flows[i]);playing=true;el('play').textContent='❚❚ Pause';tourSchedule();}
el('playall').onclick=()=>{if(DATA.flows.length){touring=true;tourOpen(0);}};
function route(){touring=false;const slug=decodeURIComponent(location.hash.replace(/^#/,''));const f=BY[slug];
if(f){openFlow(f);}else{stop();el('player').classList.remove('on');el('gallery').classList.add('on');}}
window.addEventListener('hashchange',route);route();

/* Click = play/pause (what everyone expects of a player). Zoom is a DELIBERATE
   gesture: double-click toward a point, drag to pan, double-click to reset.
   Single-click used to zoom, so the natural "tap the video" instinct looked
   like the demo randomly zooming in on a step. */
(function(){var st=shot.parentElement,sc=1,ox=0,oy=0,drag=0,dbl=0;
function aT(){shot.style.transform='translate('+ox+'px,'+oy+'px) scale('+sc+')';}
function rs(){sc=1;ox=0;oy=0;shot.style.transformOrigin='0 0';aT();st.classList.remove('zoomed');}
window.__srResetZoom=rs;                     /* show() clears zoom on step change */
shot.addEventListener('load',function(){if(sc===1)rs();});  /* keep an intentional zoom while a clip plays */
st.addEventListener('click',function(e){
  if(drag||dbl)return;
  setTimeout(function(){if(!dbl)play();},220);   /* single click -> play/pause */
});
st.addEventListener('dblclick',function(e){
  dbl=1;setTimeout(function(){dbl=0;},420);
  if(sc===1){var r=st.getBoundingClientRect();
    shot.style.transformOrigin=((e.clientX-r.left)/r.width*100)+'% '+((e.clientY-r.top)/r.height*100)+'%';
    sc=2.2;ox=0;oy=0;st.classList.add('zoomed');aT();}
  else{rs();}
});
st.addEventListener('mousedown',function(e){if(sc===1)return;e.preventDefault();drag=0;var px=e.clientX,py=e.clientY,bx=ox,by=oy;st.classList.add('grabbing');
function mm(ev){ox=bx+(ev.clientX-px);oy=by+(ev.clientY-py);if(Math.abs(ev.clientX-px)+Math.abs(ev.clientY-py)>3)drag=1;aT();}
function mu(){document.removeEventListener('mousemove',mm);document.removeEventListener('mouseup',mu);st.classList.remove('grabbing');setTimeout(function(){drag=0;},0);}
document.addEventListener('mousemove',mm);document.addEventListener('mouseup',mu);});
})();
</script></body></html>"""

if __name__ == "__main__":
    main()
