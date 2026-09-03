"""
Contract tests for the specreel spike.

These lock the current behavior (step counts, kinds, captions, embedded frames)
*before* the caption-cleanup refactor, so changes that alter the contract are
caught loudly. Run:  python -m pytest -q
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "todo-trace.zip")

# import specreel.py as a module without requiring a package install
_spec = importlib.util.spec_from_file_location("specreel", os.path.join(ROOT, "specreel.py"))
specreel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(specreel)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

import pytest


@pytest.fixture(scope="module")
def rendered():
    """Run the parse->humanize pipeline on the bundled fixture trace."""
    tmp = tempfile.mkdtemp(prefix="specreel_test_")
    with zipfile.ZipFile(FIXTURE) as z:
        z.extractall(tmp)
    events = specreel.load_events(tmp)
    steps, frames = specreel.build_steps(events)
    steps = specreel.coalesce_steps(steps)
    specreel.attach_frames(steps, frames)
    return specreel.render_steps(steps, tmp), frames


# --------------------------------------------------------------------------- #
# contract: step counts and kinds
# --------------------------------------------------------------------------- #

def test_step_count(rendered):
    # 13 raw steps minus the 3 fill+Enter pairs that coalesce into Submit steps.
    steps, _ = rendered
    assert len(steps) == 10


def test_action_check_split(rendered):
    steps, _ = rendered
    actions = sum(1 for s in steps if s["kind"] != "check")
    checks = sum(1 for s in steps if s["kind"] == "check")
    assert (actions, checks) == (6, 4)


def test_fill_enter_coalesced(rendered):
    # no bare "Press Enter" steps survive; three Submit steps take their place.
    steps, _ = rendered
    captions = [s["caption"] for s in steps]
    assert not any(c.startswith("Press ") for c in captions)
    assert sum(1 for c in captions if c.startswith("Submit ")) == 3


def test_no_failures_in_fixture(rendered):
    steps, _ = rendered
    assert not any(s["failed"] for s in steps)


def test_frames_attached(rendered):
    steps, frames = rendered
    assert len(frames) > 0
    # every step should resolve to a frame image in this fixture
    assert all(s["img"] for s in steps)


def test_first_and_last_caption(rendered):
    steps, _ = rendered
    assert steps[0]["caption"] == "Open demo.playwright.dev/todomvc"
    assert steps[-1]["caption"] == 'Confirm the todo count reads "2 items left"'


# --------------------------------------------------------------------------- #
# contract: HTML embeds frames and is self-contained
# --------------------------------------------------------------------------- #

def test_html_embeds_frames():
    out = tempfile.mkdtemp(prefix="specreel_html_")
    cmd = [sys.executable, os.path.join(ROOT, "specreel.py"), FIXTURE, "-o", out,
           "--title", "Contract test"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html_path = os.path.join(out, "demo.html")
    assert os.path.exists(html_path)
    body = open(html_path).read()
    assert "data:image/jpeg;base64," in body          # frames embedded
    assert "Contract test" in body                     # title injected
    assert "6 actions" in body and "4 checks" in body  # summary line


# --------------------------------------------------------------------------- #
# unit: humanize_selector
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sel,expected", [
    ('internal:role=link[name="Active"i]', 'the "Active" link'),
    ('internal:role=checkbox >> internal:label="Toggle"', 'the "Toggle" checkbox'),
    ('internal:attr=[placeholder="What needs to be done?"i]', 'the "What needs to be done?" field'),
    ('internal:testid=[data-testid="todo-count"s]', "the todo count"),
    ('', "the element"),
])
def test_humanize_selector(sel, expected):
    assert specreel.humanize_selector(sel) == expected


@pytest.mark.parametrize("raw,expected", [
    ("todo-count", "todo count"),
    ("nav--primary", "nav primary"),
    ("submit_btn", "submit"),       # trailing chrome word dropped
    ("save-button", "save"),
    ("menu-icon", "menu"),
    ("btn", "btn"),                 # single word kept (never empties)
    ("items-counter", "items counter"),  # 'counter' is not chrome noise
])
def test_humanize_name(raw, expected):
    assert specreel.humanize_name(raw) == expected


# --------------------------------------------------------------------------- #
# unit: predicate (assertion expressions)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("expr,params,expected", [
    ("to.have.count", {"expectedNumber": 3}, "shows 3 item(s)"),
    ("to.have.text", {"expectedText": [{"string": "2 items left"}]}, 'reads "2 items left"'),
    ("to.have.class", {"expectedText": [{"string": "completed"}]}, "is marked complete"),
    ("to.be.visible", {}, "is visible"),
    ("to.be.checked", {}, "is checked"),
])
def test_predicate(expr, params, expected):
    assert specreel.predicate(expr, params) == expected


# --------------------------------------------------------------------------- #
# unit: humanize dispatch
# --------------------------------------------------------------------------- #

def test_password_value_masked():
    # secrets must never leak into a shareable caption
    cap, _ = specreel.humanize({"method": "fill", "params": {
        "value": "hunter2", "selector": 'internal:attr=[placeholder="Create a password"i]'}})
    assert "hunter2" not in cap and "••••••••" in cap
    # non-secret fields are shown verbatim
    cap2, _ = specreel.humanize({"method": "fill", "params": {
        "value": "demo@kumkuat.ai", "selector": 'internal:attr=[placeholder="Email"i]'}})
    assert "demo@kumkuat.ai" in cap2


def test_secret_masking_conservative():
    def fill(val, sel):
        return specreel.humanize({"method": "fill", "params": {"value": val, "selector": sel}})[0]
    # masked across selector kinds + a broader, curated keyword set
    assert "••••" in fill("123456", 'internal:label="Passcode"i')
    assert "••••" in fill("000000", 'internal:attr=[placeholder="OTP"i]')
    assert "••••" in fill("123", 'internal:role=textbox[name="CVV"i]')
    assert "••••" in fill("s3cret", "input[type=password]")          # the type=password workaround
    # NOT masked: an API *token-name* field is not itself a secret (our own cloud has one)
    assert "my-ci-token" in fill("my-ci-token", 'internal:attr=[placeholder="Token name (e.g. CI)"i]')
    # NOT masked: ambiguous words don't false-positive (we exclude "pin", etc.)
    assert "Springfield" in fill("Springfield", 'internal:attr=[placeholder="Shipping city"i]')
    # name-path: masks even when only the humanized target name carries the keyword
    assert specreel.display_value("x", "internal:role=textbox", "the password field") == "••••••••"
    # residual gap (documented): a keyword-less placeholder is NOT auto-masked
    assert specreel.display_value(
        "plaintext", 'internal:attr=[placeholder="at least 8 characters"i]') == "plaintext"


def test_plumbing_methods_skipped():
    # internal waits / setup calls must not surface as demo steps
    assert "waitForTimeout" in specreel.SKIP_METHODS
    assert "__waitInfo__" in specreel.SKIP_METHODS
    assert "scrollIntoViewIfNeeded" in specreel.SKIP_METHODS
    assert "innerText" in specreel.SKIP_METHODS
    assert "evaluate" in specreel.SKIP_METHODS
    assert "evaluateExpression" in specreel.SKIP_METHODS
    events = [
        {"type": "before", "callId": "1", "method": "click", "params": {}, "startTime": 1},
        {"type": "after", "callId": "1", "endTime": 2},
        {"type": "before", "callId": "2", "method": "waitForTimeout", "params": {}, "startTime": 3},
        {"type": "after", "callId": "2", "endTime": 4},
        # Playwright ≥1.53 wait instrumentation (was waitForEventInfo)
        {"type": "before", "callId": "3", "method": "__waitInfo__", "params": {}, "startTime": 5},
        {"type": "after", "callId": "3", "endTime": 6},
        {"type": "before", "callId": "4", "method": "__abort__", "params": {}, "startTime": 7},
        {"type": "after", "callId": "4", "endTime": 8},
    ]
    steps, _ = specreel.build_steps(events)
    assert [s["method"] for s in steps] == ["click"]
    # humanize must never be asked to title a protocol method (fallback = raw name)
    assert specreel.humanize({"method": "__waitInfo__", "params": {}})[0] == "__waitInfo__"


def test_outcome_waits_are_demo_steps():
    """URL/function waits are outcomes (chat reply, scorecard results) — not plumbing."""
    assert "waitForFunction" not in specreel.SKIP_METHODS
    assert "waitForURL" not in specreel.SKIP_METHODS
    events = [
        {"type": "before", "callId": "1", "method": "press", "params": {"key": "Enter"},
         "startTime": 1},
        {"type": "after", "callId": "1", "endTime": 2},
        {"type": "before", "callId": "2", "method": "waitForFunction",
         "params": {"expression": "document.body.innerText.length > 1100"},
         "startTime": 3},
        {"type": "after", "callId": "2", "endTime": 8000},
        {"type": "before", "callId": "3", "method": "waitForURL",
         "params": {"url": "persona-reaction"}, "startTime": 9000},
        {"type": "after", "callId": "3", "endTime": 12000},
    ]
    steps, _ = specreel.build_steps(events)
    assert [s["method"] for s in steps] == ["press", "waitForFunction", "waitForURL"]
    assert specreel.humanize(steps[1])[0] == "Wait for the response"
    assert specreel.humanize(steps[2])[0] == "Wait for the results"


def test_humanize_kinds():
    assert specreel.humanize({"method": "goto", "params": {"url": "https://x.io/"}})[1] == "nav"
    assert specreel.humanize({"method": "click", "params": {"selector": ""}})[1] == "action"
    assert specreel.humanize({"method": "expect", "params": {"expression": "to.be.visible"}})[1] == "check"


# --------------------------------------------------------------------------- #
# batch / gallery mode
# --------------------------------------------------------------------------- #

def test_gallery_over_directory():
    """Point specreel at a test-results-style dir -> per-demo pages + index."""
    src = tempfile.mkdtemp(prefix="specreel_tr_")
    for name in ("add-and-filter-chromium", "complete-todo-firefox"):
        d = os.path.join(src, name)
        os.makedirs(d)
        import shutil as _sh
        _sh.copy(FIXTURE, os.path.join(d, "trace.zip"))
    out = tempfile.mkdtemp(prefix="specreel_site_")
    cmd = [sys.executable, os.path.join(ROOT, "specreel.py"), src, "-o", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    index = os.path.join(out, "index.html")
    assert os.path.exists(index)
    assert os.path.exists(os.path.join(out, "add-and-filter-chromium", "demo.html"))
    assert os.path.exists(os.path.join(out, "complete-todo-firefox", "demo.html"))
    body = open(index).read()
    assert body.count('class="card') == 2          # one card per trace
    assert 'href="add-and-filter-chromium/demo.html"' in body
    # gallery chrome: hero + stat strip + pills + real end-state thumbnails
    assert "Your tests just shipped" in body
    assert "Flows tracked" in body
    assert "▶ demo" in body and "✓ test" in body
    assert body.count("background-image:url('data:image/jpeg;base64,") == 2


def test_gallery_filter_and_player_zoom(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces" / "a"
    src.mkdir(parents=True)
    _sh.copy(FIXTURE, src / "trace.zip")
    out = tmp_path / "site"
    specreel.generate_gallery(str(tmp_path / "traces"), str(out), bundle=True)
    idx = (out / "index.html").read_text()
    assert 'id="toolbar"' in idx and "data-status=" in idx and "data-title=" in idx
    assert "getElementById('q')" in idx                  # filter wired
    demo = next(out.glob("*/demo.html")).read_text()
    # a single click plays/pauses (the expected player gesture); zoom is a
    # deliberate double-click, so tapping the image never surprises the viewer
    assert "dblclick" in demo and ".stage.zoomed" in demo
    assert "single click -> play/pause" in demo
    assert "dblclick" in (out / "gallery.html").read_text()  # bundle too
    # smarter TTS: prefer a natural voice + a voice picker, not the robotic default
    assert "_vscore" in demo and 'id="voice"' in demo and "u.rate=0.97" in demo
    # autoplay must wait for TTS to finish (not just step.dur ~1.4s)
    assert "u.onend=" in demo and "_speakMs" in demo and "_gen" in demo
    # studio voiceover playback wiring (plays embedded audio when present)
    assert "function playStep" in demo and "new Audio()" in demo
    # the narration engine is ONE shared snippet — both players get identical wiring
    gal = (out / "gallery.html").read_text()
    for page in (demo, gal):
        assert "_initTTS" in page and "_hasVO" in page   # studio VO defaults sound ON
        assert "🔊 Voiceover" in page                     # paid narration labels itself
        assert "specreel-tts" in page and "specreel-voice" in page  # prefs persist
        assert "This step failed: " in page               # failures are audible
        assert "the hidden value" in page                 # masked ••• never read aloud


def test_synthesize_voiceover(monkeypatch):
    monkeypatch.setenv("SPECREEL_TTS_CACHE", "off")
    calls = []
    monkeypatch.setattr(specreel, "_openai_tts",
                        lambda text, key, voice, model, fmt="mp3", timeout=30,
                        instructions="":
                        (calls.append((text, voice, model, instructions)), b"MP3BYTES")[1])
    rendered = [
        {"caption": "Open /x", "kind": "nav", "img": "", "dur": 1.2, "failed": False},
        {"narration": "Sign up now", "caption": "Type", "kind": "action", "img": "", "dur": 1.2, "failed": False}]
    n = specreel.synthesize_voiceover(rendered, "key", voice="nova", model="tts-1")
    assert n == 2 and rendered[0]["audio"].startswith("data:audio/mpeg;base64,")
    texts = {c[0] for c in calls}                        # synthesis is parallel: no order
    assert "Sign up now" in texts                        # narration preferred over caption
    assert all(c[1] == "nova" and c[2] == "tts-1" for c in calls)
    assert all(c[3] == "" for c in calls)                # tts-1 rejects instructions
    assert specreel.step_payload(rendered)[0]["audio"]   # flows to the player payload
    # gpt- models take delivery notes: the default narrator unless overridden
    calls.clear()
    r_gpt = [{"caption": "x", "kind": "nav", "img": "", "dur": 1, "failed": False}]
    specreel.synthesize_voiceover(r_gpt, "key", model="gpt-4o-mini-tts")
    assert calls[0][3] == specreel.DEFAULT_TTS_INSTRUCTIONS
    calls.clear()
    specreel.synthesize_voiceover(r_gpt, "key", model="gpt-4o-mini-tts",
                                  instructions="brisk")
    assert calls[0][3] == "brisk"
    # no key -> no calls, no audio
    r2 = [{"caption": "x", "kind": "nav", "img": "", "dur": 1, "failed": False}]
    assert specreel.synthesize_voiceover(r2, "") == 0 and not r2[0].get("audio")
    # a per-step failure is graceful (no audio, no raise) -> falls back to browser
    # voice, and the one retry actually happened
    monkeypatch.setattr(specreel, "_TTS_RETRY_SLEEP", 0)
    tries = []
    def boom(*a, **k):
        tries.append(1)
        raise RuntimeError("boom")
    monkeypatch.setattr(specreel, "_openai_tts", boom)
    r3 = [{"caption": "x", "kind": "nav", "img": "", "dur": 1, "failed": False}]
    assert specreel.synthesize_voiceover(r3, "key") == 0 and not r3[0].get("audio")
    assert len(tries) == 2
    # a transient blip recovers on the retry
    flaky = {"n": 0}
    def flip(*a, **k):
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise RuntimeError("blip")
        return b"OK"
    monkeypatch.setattr(specreel, "_openai_tts", flip)
    r4 = [{"caption": "x", "kind": "nav", "img": "", "dur": 1, "failed": False}]
    assert specreel.synthesize_voiceover(r4, "key") == 1


def test_voiceover_tts_cache(tmp_path, monkeypatch):
    import base64
    monkeypatch.setenv("SPECREEL_TTS_CACHE", str(tmp_path / "tts"))
    calls = []
    monkeypatch.setattr(specreel, "_openai_tts",
                        lambda text, *a, **k: (calls.append(text), b"AUDIO")[1])
    step = lambda: [{"caption": "Open /x", "kind": "nav", "img": "", "dur": 1,
                     "failed": False}]
    assert specreel.synthesize_voiceover(step(), "key") == 1
    assert len(calls) == 1 and len(list((tmp_path / "tts").iterdir())) == 1
    # same words, same voice -> served from disk: a rebuild doesn't re-bill
    r = step()
    assert specreel.synthesize_voiceover(r, "key") == 1
    assert len(calls) == 1
    assert r[0]["audio"].endswith(base64.b64encode(b"AUDIO").decode())
    # a different voice is a different clip
    assert specreel.synthesize_voiceover(step(), "key", voice="onyx") == 1
    assert len(calls) == 2
    # cache disabled -> always synthesizes
    monkeypatch.setenv("SPECREEL_TTS_CACHE", "off")
    assert specreel.tts_cache_dir() is None
    assert specreel.synthesize_voiceover(step(), "key") == 1
    assert len(calls) == 3


def test_speakable():
    assert specreel.speakable({"caption": "Click Save", "failed": False}) == "Click Save"
    assert specreel.speakable({"narration": "Save it", "caption": "Click",
                               "failed": False}) == "Save it"
    # masked secrets are never read as "bullet bullet bullet"
    s = specreel.speakable({"caption": 'Enter ••• in "Password"', "failed": False})
    assert "•" not in s and "the hidden value" in s
    # url schemes are noise to the ear
    assert specreel.speakable({"caption": "Open https://app.example.com/login",
                               "failed": False}) == "Open app.example.com/login"
    # a listener can't see the red caption bar — failures say so, with the reason
    s = specreel.speakable({"caption": "Confirm the count reads 2",
                            "failed": True, "why": "it read 3"})
    assert s.startswith("This step failed: ") and s.endswith(". it read 3")
    assert specreel.speakable({"caption": "", "failed": True}) == ""


def test_plan_voiceover_timeline():
    # no clips -> pure silence, nothing extended
    ext, seg = specreel.plan_voiceover([1.0, 2.0], [None, None], lead=2.0, tail=2.6)
    assert ext == [0.0, 0.0]
    assert seg == [("silence", 2.0), ("silence", 1.0), ("silence", 2.0),
                   ("silence", 2.6)]
    # a clip that outruns its step extends the step's hold, never cut mid-sentence
    ext, seg = specreel.plan_voiceover([1.0, 2.0], [3.0, 1.0],
                                       lead=2.0, tail=2.6, breath=0.35)
    assert ext == [2.35, 0.0]
    assert seg[1] == ("clip", 0, 3.35) and seg[2] == ("clip", 1, 2.0)
    # the audio track covers lead + every (extended) step + tail exactly
    assert abs(sum(s[-1] for s in seg) - (2.0 + 3.35 + 2.0 + 2.6)) < 1e-6


def test_caption_frames_spans(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    import base64, io
    buf = io.BytesIO()
    Image.new("RGB", (64, 40), (10, 10, 10)).save(buf, "JPEG")
    img = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    rendered = [
        {"i": 1, "caption": "a", "kind": "nav", "img": img, "imgs": [img, img],
         "dts": [0, 500], "dur": 2.0, "failed": False},
        {"i": 2, "caption": "b", "kind": "check", "img": "", "imgs": [], "dts": [],
         "dur": 1.2, "failed": False},                # frameless step -> no span
        {"i": 3, "caption": "c", "kind": "action", "img": img, "imgs": [],
         "dts": [], "dur": 1.5, "failed": False}]
    items, spans = specreel._caption_frames(rendered, str(tmp_path), W=64)
    assert len(items) == 3                            # 2 clip frames + 1 still
    assert [(a, b) for _, a, b in spans] == [(0, 1), (2, 2)]
    assert spans[0][0] is rendered[0] and spans[1][0] is rendered[2]
    # per-step screen time is what the audio planner sees: clip gap + final hold
    assert abs(sum(d for _, d in items[0:2]) - 2.0) < 1e-6


def test_build_bundle_is_single_self_contained_file(tmp_path):
    entries = [{
        "slug": "alpha", "title": "Alpha Flow", "public": True,
        "n_steps": 2, "n_actions": 1, "n_checks": 1, "failed": False, "duration": 5.0,
        "thumb": "data:image/jpeg;base64,AAAA",
        "steps": [
            {"caption": "Open Alpha", "lit": "Open /", "kind": "nav", "img": "", "dur": 1.2, "failed": False},
            {"caption": "It loads", "lit": "", "kind": "check", "img": "", "dur": 1.2, "failed": False},
        ],
    }]
    path = specreel.build_bundle(entries, str(tmp_path), ctx={"build": "#7"}, gallery_title="G")
    assert path.endswith("gallery.html")
    body = open(path).read()
    assert "const DATA=" in body            # data inlined
    assert "Alpha Flow" in body and "Open Alpha" in body   # flow + step text present
    assert "demo.html" not in body          # no links to sibling files — fully self-contained


def test_gallery_bundle_inlines_all_flows(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces"
    for name in ("one", "two"):
        d = src / name
        d.mkdir(parents=True)
        _sh.copy(FIXTURE, d / "trace.zip")
    out = tmp_path / "site"
    rc = specreel.generate_gallery(str(src), str(out), bundle=True)
    assert rc == 0
    bundle = (out / "gallery.html").read_text()
    assert bundle.count('"slug"') >= 2                     # both flows inlined
    assert '"steps"' in bundle and "data:image/jpeg;base64," in bundle  # frames inlined


@pytest.mark.parametrize("remote,expected", [
    ("git@github.com:acme/dashboard.git", "https://acme.github.io/dashboard/"),
    ("https://github.com/acme/dashboard.git", "https://acme.github.io/dashboard/"),
    ("https://github.com/acme/dashboard", "https://acme.github.io/dashboard/"),
    ("https://gitlab.com/acme/x.git", None),
])
def test_pages_url_from_remote(remote, expected):
    assert specreel.pages_url_from_remote(remote) == expected


def test_embed_snippet():
    snip = specreel.embed_snippet("https://acme.github.io/dashboard/")
    assert "<iframe" in snip and "https://acme.github.io/dashboard/" in snip
    assert "#<flow-slug>" in snip


def test_publish_dir_copies_gallery(tmp_path):
    site = tmp_path / "site"
    (site / "signup").mkdir(parents=True)
    (site / "index.html").write_text("<html>gallery</html>")
    (site / "signup" / "demo.html").write_text("<html>demo</html>")
    dest = tmp_path / "out"
    rc = specreel.publish(str(site), f"dir:{dest}")
    assert rc == 0
    assert (dest / "index.html").exists()
    assert (dest / "signup" / "demo.html").exists()


def test_publish_rejects_non_gallery(tmp_path):
    # a dir with no index.html isn't a generated gallery
    (tmp_path / "empty").mkdir()
    assert specreel.publish(str(tmp_path / "empty"), "dir:/tmp/x") == 1


def test_self_healing_change_detection(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces"
    for n in ("one", "two"):
        d = src / n
        d.mkdir(parents=True)
        _sh.copy(FIXTURE, d / "trace.zip")
    out = tmp_path / "site"
    out.mkdir()
    # seed a previous build whose 'one' signature differs -> should flag as updated
    (out / "manifest.json").write_text(json.dumps({"flows": [{"slug": "one", "sig": "STALEHASH00"}]}))
    specreel.generate_gallery(str(src), str(out))
    man = json.loads((out / "manifest.json").read_text())
    by = {f["slug"]: f for f in man["flows"]}
    assert by["one"]["healed"] is True     # sig changed vs prior build
    assert by["two"]["healed"] is False    # no prior record -> fresh
    assert "⟳ updated" in (out / "index.html").read_text()
    # a second identical run flags nothing (sigs now match)
    specreel.generate_gallery(str(src), str(out))
    man2 = json.loads((out / "manifest.json").read_text())
    assert all(not f["healed"] for f in man2["flows"])


def test_healed_config_override(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces"
    (src / "alpha").mkdir(parents=True)
    _sh.copy(FIXTURE, src / "alpha" / "trace.zip")
    cfg = tmp_path / "specreel.yml"
    cfg.write_text("flows:\n  alpha:\n    healed: true\n")
    out = tmp_path / "site"
    specreel.generate_gallery(str(src), str(out), config_path=str(cfg))
    man = json.loads((out / "manifest.json").read_text())
    assert man["flows"][0]["healed"] is True   # forced by config even on first run


SAMPLE_HTML = """<html><head><title>Sign up — Acme</title></head><body>
<h1>Create your account</h1>
<form action="/signup" method="post">
<input name="email" type="email" placeholder="Enter your email">
<input name="password" type="password" placeholder="Create a password">
<input name="csrf" type="hidden" value="x">
<button>Create Account</button></form>
<a href="/pricing">Pricing</a><a href="/logo.png">logo</a>
<a href="https://other.com/x">offsite</a>
<input type="search" placeholder="Search docs"></body></html>"""


def test_mcp_server_registers_tools():
    pytest.importorskip("mcp")          # optional [mcp] extra
    import asyncio
    spec = importlib.util.spec_from_file_location(
        "specreel_mcp", os.path.join(ROOT, "specreel_mcp.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    names = {t.name for t in asyncio.run(m.mcp.list_tools())}
    assert {"recommend", "render", "publish", "summary", "init_config"} <= names
    assert all((t.description or "").strip()
               for t in asyncio.run(m.mcp.list_tools()))   # agents read these


def test_extract_page():
    pg = specreel.extract_page(SAMPLE_HTML, "http://app/signup")
    assert pg["title"] == "Sign up — Acme"
    assert pg["headings"][0] == "Create your account"
    assert len(pg["forms"]) == 1
    fields = pg["forms"][0]["fields"]
    assert {f["name"] for f in fields} == {"email", "password", "csrf"}
    # hidden field excluded from page-level inputs; email/password/search kept
    types = {f["type"] for f in pg["inputs"]}
    assert "hidden" not in types and "search" in types and "email" in types


def test_crawl_same_origin_with_injected_fetch():
    pages = {
        "http://app/": '<a href="/a">A</a><a href="/b.css">x</a><a href="http://x/y">o</a>',
        "http://app/a": '<title>A</title><a href="/">home</a>',
    }
    seen = []
    def fake_fetch(url):
        seen.append(url)
        return pages.get(url.rstrip("/") if url != "http://app/" else url, pages.get(url, ""))
    got = specreel.crawl("http://app/", max_pages=10, fetch=fake_fetch)
    urls = {p["url"].rstrip("/") for p in got}
    assert "http://app" in urls or "http://app/" in {p["url"] for p in got}
    assert any(u.endswith("/a") for u in {p["url"] for p in got})   # followed same-origin link
    # never fetched the offsite or the .css asset
    assert not any("x/y" in u or ".css" in u for u in seen)


def test_recommend_flows_ranking_and_scaffold():
    pg = specreel.extract_page(SAMPLE_HTML, "http://app/signup")
    flows = specreel.recommend_flows([pg])
    assert flows[0]["type"] == "form"                 # forms rank first
    assert any(f["type"] == "search" for f in flows)
    py = specreel.scaffold_script(flows, "http://app", lang="py")
    assert "async_playwright" in py and "tracing.start" in py
    assert 'get_by_placeholder("Enter your email")' in py and "demo@example.com" in py
    js = specreel.scaffold_script(flows, "http://app", lang="js")
    assert "@playwright/test" in js and "getByPlaceholder" in js


def test_scaffold_quality_contract():
    """The generated flows must make sense as tests and not cut off early:
    stable page-title assert (not a content heading), a settle so the trace
    captures the RESULT of the last action, honest titles, nav scrolls."""
    flows = [
        {"type": "search", "title": "Search on Dylan Roy", "url": "http://x/",
         "heading": "Easily Make Youtube Compilations Using Python (a long content headline)",
         "page_title": "Dylan Roy",
         "fields": [{"name": "q", "placeholder": "Enter keyword...", "id": "", "type": "text"}]},
        {"type": "nav", "title": "Open About", "url": "http://x/about",
         "heading": "About", "page_title": "About", "fields": []},
    ]
    py = specreel.scaffold_script([dict(f) for f in flows], "http://x", lang="py")
    # stable title assert, NOT the long content heading
    assert 'to_have_title(re.compile("Dylan\\\\ Roy", re.I))' in py or "to_have_title" in py
    assert "Easily Make Youtube" not in py
    # search presses Enter and then settles so results are captured
    # Prefer load+pause over networkidle: chat/websocket apps never go idle,
    # and a timed-out settle still lands as a FAIL step in the demo trace.
    assert '.press("Enter")' in py
    assert 'wait_for_load_state("load")' in py and "wait_for_timeout(1200)" in py
    assert "networkidle" not in py
    # nav flows scroll (demo shows the page) instead of a dangling submit-TODO
    assert "page.mouse.wheel(0, 600)" in py
    js = specreel.scaffold_script([dict(f) for f in flows], "http://x", lang="js")
    assert "toHaveTitle" in js and "waitForLoadState('load')" in js
    # form titles no longer claim a submit that doesn't happen
    pg = specreel.extract_page(SAMPLE_HTML, "http://app/signup")
    titles = [f["title"] for f in specreel.recommend_flows([pg])]
    assert any(t.startswith("Fill the ") for t in titles)
    assert not any("Fill & submit" in t for t in titles)
    # scroll steps humanize nicely
    assert specreel.humanize({"method": "mouseWheel", "params": {}})[0] == "Scroll the page"
    # page-level title assert captions read as "the page", with re.escape stripped
    cap, kind = specreel.humanize({"method": "expect", "params": {
        "expression": "to.have.title",
        "expectedText": [{"regexSource": "Dylan\\ Roy", "regexFlags": "i"}]}})
    assert cap == 'Confirm the page title is "Dylan Roy"' and kind == "check"
    # a site-wide widget yields ONE flow, not one per crawled page
    pages = [specreel.extract_page(
        f'<title>P{i}</title><input type="search" name="q" placeholder="Enter keyword...">',
        f"http://x/p{i}") for i in range(4)]
    searches = [f for f in specreel.recommend_flows(pages) if f["type"] == "search"]
    assert len(searches) == 1


def test_scaffold_honors_nl_code_block():
    flows = [{"title": "Login then create", "type": "custom", "url": "http://x",
              "heading": "", "fields": [],
              "code": 'await page.goto(BASE + "/login")\nawait page.get_by_label("Email").fill("a@b.co")'}]
    py = specreel.scaffold_script(flows, "http://x", lang="py")
    assert 'get_by_label("Email")' in py and "/login" in py
    assert "async def login_then_create" in py            # slug -> fn name


def test_nl_flow(monkeypatch):
    import json as _json
    monkeypatch.setattr(specreel, "_anthropic_messages", lambda b, k, timeout=60: {
        "content": [{"type": "text", "text": _json.dumps(
            {"title": "Log in", "code": "await page.goto(BASE)"})}]})
    fl = specreel.nl_flow("log in", [], "http://x", "py", "key")
    assert fl["code"] == "await page.goto(BASE)" and fl["title"] == "Log in" and fl["nl"]
    monkeypatch.setattr(specreel, "_anthropic_messages",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert specreel.nl_flow("x", [], "http://x", "py", "key") is None     # graceful


def test_ai_curate_instruction_drops(monkeypatch):
    import json as _json
    flows = [{"type": "form", "title": "A", "url": "/a", "heading": ""},
             {"type": "nav", "title": "B", "url": "/b", "heading": ""},
             {"type": "nav", "title": "C", "url": "/c", "heading": ""}]
    monkeypatch.setattr(specreel, "_anthropic_messages", lambda b, k: {
        "content": [{"type": "text", "text": _json.dumps({"order": [{"index": 0, "title": "Sign up"}]})}]})
    out = specreel.ai_curate(flows, "key", instruction="only keep the form", allow_drop=True)
    assert [f["title"] for f in out] == ["Sign up"]       # B and C dropped per instruction
    out2 = specreel.ai_curate(flows, "key")               # no drop -> all kept
    assert len(out2) == 3


def test_assemble_scaffold_mixed(monkeypatch):
    monkeypatch.setattr(specreel, "nl_flow", lambda prompt, ctx, base, lang, key, **kw: {
        "title": "Custom step", "type": "custom", "url": base, "heading": "", "fields": [],
        "code": 'await page.goto(BASE + "/x")', "nl": True})
    spec = {"base_url": "http://x", "lang": "py", "items": [
        {"type": "form", "title": "Sign up", "url": "http://x/signup", "heading": "",
         "fields": [{"name": "email", "placeholder": "Email", "id": "", "type": "email"}]},
        {"nl_prompt": "do a custom thing"}]}
    out = specreel.assemble_scaffold(spec, api_key="key")
    assert "Sign up" in out and "Custom step" in out and "/signup" in out and 'goto(BASE + "/x")' in out
    # no key -> the NL item degrades to a TODO stub but still appears
    out2 = specreel.assemble_scaffold(spec, api_key=None)
    assert "TODO: do a custom thing" in out2


def test_recommend_json_mode(monkeypatch, capsys):
    import json as _json
    pg = specreel.extract_page(SAMPLE_HTML, "http://app/signup")
    monkeypatch.setattr(specreel, "crawl", lambda url, max_pages=12, headers=None: [pg])
    rc = specreel.recommend_main(["http://app", "--json"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)        # stdout is pure JSON
    assert out["url"] == "http://app" and out["pages"] == 1
    assert out["flows"] and out["flows"][0]["type"] == "form"
    assert "slug" in out["flows"][0] and "async_playwright" in out["scaffold"]
    # page_title survives --json, so wizard-assembled scaffolds keep the STABLE
    # to_have_title assert instead of falling back to a content-heading check
    assert out["flows"][0]["page_title"]
    reassembled = specreel.assemble_scaffold(
        {"base_url": "http://app", "lang": "py", "items": out["flows"]})
    assert "to_have_title" in reassembled


def test_recommend_json_error_is_structured(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(specreel, "crawl", lambda url, max_pages=12, headers=None: [])
    rc = specreel.recommend_main(["http://app", "--json"])
    assert rc == 1
    assert _json.loads(capsys.readouterr().out)["error"]   # JSON error, not a traceback


def test_steps_show_their_own_result_not_the_prior_state():
    """A frame slightly BEFORE an action's end predates the paint; picking it makes
    every step run one beat behind its caption. Prefer the first frame a paint-lag
    past the end."""
    steps = [{"end": 1000, "start": 900}, {"end": 2000, "start": 1900}]
    frames = [{"timestamp": 990, "sha1": "before"},     # pre-paint (10ms early)
              {"timestamp": 1050, "sha1": "skewed"},    # within screencast skew
              {"timestamp": 1150, "sha1": "result1"},   # the real result
              {"timestamp": 2200, "sha1": "result2"}]
    specreel.attach_frames(steps, frames)
    assert steps[0]["frame"] == "result1"               # not "before"/"skewed"
    assert steps[1]["frame"] == "result2"


def test_press_sequentially_humanizes_like_fill():
    cap, kind = specreel.humanize({"method": "type", "params": {
        "selector": 'internal:attr=[placeholder="you@company.com"i]',
        "text": "demo@specreel.dev"}})
    assert cap == 'Type "demo@specreel.dev" into the "you@company.com" field'
    # masking applies to keystroke-typed secrets too
    cap2, _ = specreel.humanize({"method": "type", "params": {
        "selector": "input[type=password]", "text": "hunter2"}})
    assert "hunter2" not in cap2 and "••••••••" in cap2


def test_final_step_pins_to_last_frame():
    """The demo must END on the outcome: the final step shows the trace's last
    frame, even when tracing stopped so fast that no frame postdates the action."""
    steps = [{"end": 100, "start": 90}, {"end": 880, "start": 800}]
    frames = [{"timestamp": 95, "sha1": "a"}, {"timestamp": 300, "sha1": "b"},
              {"timestamp": 821, "sha1": "c"}]          # last frame BEFORE last end
    specreel.attach_frames(steps, frames)
    assert steps[-1]["frame"] == "c"                    # end state, not none/stale
    assert steps[0]["frame"] in ("a", "b")


def test_doctor_warns_no_outcome_frame(tmp_path):
    import shutil as _sh
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "todo-trace.zip")
    d = tmp_path / "t" / "flow"
    d.mkdir(parents=True)
    _sh.copy(fixture, d / "trace.zip")
    results, ok = specreel.diagnose(str(tmp_path / "t"))
    assert ok
    # the fixture ends with a settled state, so it should be a clean ok — the
    # warn branch text is locked here so the message stays actionable
    assert any(lvl == "ok" for lvl, _ in results) or any(
        "none AFTER the last action" in msg for _, msg in results)


def test_doctor_diagnose(tmp_path):
    import shutil as _sh
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "todo-trace.zip")
    # a good trace -> ok, no fatal
    traces = tmp_path / "test-results" / "todo"
    traces.mkdir(parents=True)
    _sh.copy(fixture, traces / "trace.zip")
    results, ok = specreel.diagnose(str(tmp_path / "test-results"))
    assert ok and any(lvl == "ok" and "steps" in msg for lvl, msg in results)
    # empty dir -> fatal, no traces
    (tmp_path / "empty").mkdir()
    res2, ok2 = specreel.diagnose(str(tmp_path / "empty"))
    assert not ok2 and res2[0][0] == "fail"
    # a corrupt zip -> fatal
    bad = tmp_path / "bad" / "trace.zip"
    bad.parent.mkdir()
    bad.write_bytes(b"not a zip")
    res3, ok3 = specreel.diagnose(str(tmp_path / "bad"))
    assert not ok3 and any(lvl == "fail" for lvl, _ in res3)
    # missing path -> fatal
    res4, ok4 = specreel.diagnose(str(tmp_path / "nope"))
    assert not ok4


def test_og_meta():
    m = specreel.og_meta("Acme demos", "5 flows · all fresh")
    assert 'og:title" content="Acme demos"' in m
    assert 'og:description" content="5 flows · all fresh"' in m
    assert 'twitter:card" content="summary"' in m and 'name="description"' in m
    big = specreel.og_meta("T", "D", image="https://x/og.png")
    assert 'og:image" content="https://x/og.png"' in big and "summary_large_image" in big
    assert "&amp;" in specreel.og_meta("A & B", "")          # HTML-escaped


def test_short_url_localhost_shows_path():
    assert specreel.short_url("http://127.0.0.1:53074/signup") == "/signup"
    assert specreel.short_url("http://localhost:3000/") == "/"
    assert specreel.short_url("https://app.acme.com/checkout") == "app.acme.com/checkout"
    cap, kind = specreel.humanize({"method": "goto", "params": {"url": "http://127.0.0.1:8800/signup"}})
    assert cap == "Open /signup" and kind == "nav"


def test_recommend_login_hint(monkeypatch, capsys):
    html = ('<title>Log in</title><form>'
            '<input name="email" type="email" placeholder="Email">'
            '<input name="password" type="password"></form>')
    pg = specreel.extract_page(html, "http://app/login")
    monkeypatch.setattr(specreel, "crawl", lambda url, max_pages=12, headers=None: [pg])
    specreel.recommend_main(["http://app", "--json"])         # no --cookie given
    assert "--cookie" in capsys.readouterr().err              # nudge surfaced


def test_crawl_passes_auth_headers(monkeypatch):
    seen = {}

    def fake_fetch(url, timeout=8, headers=None):
        seen["headers"] = headers
        return "<title>X</title>"
    monkeypatch.setattr(specreel, "_fetch_html", fake_fetch)
    specreel.crawl("http://app/", max_pages=1, headers={"Cookie": "session=abc"})
    assert seen["headers"] == {"Cookie": "session=abc"}     # logged-in crawl


def test_recommend_builds_auth_headers(monkeypatch, capsys):
    captured = {}

    def fake_crawl(url, max_pages=12, headers=None):
        captured["headers"] = headers
        return [specreel.extract_page(SAMPLE_HTML, "http://app/signup")]
    monkeypatch.setattr(specreel, "crawl", fake_crawl)
    rc = specreel.recommend_main(["http://app", "--json", "--cookie", "s=1",
                                  "--header", "Authorization: Bearer t"])
    assert rc == 0
    assert captured["headers"]["Cookie"] == "s=1"
    assert captured["headers"]["Authorization"] == "Bearer t"


def test_recommend_browser_renders_spa(tmp_path):
    pytest.importorskip("playwright")
    import functools
    import http.server
    import socketserver
    import threading
    # a single-page app: empty HTML, the form is injected by JS
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><title>SPA</title></head><body>'
        '<div id="root"></div><script>'
        'document.getElementById("root").innerHTML = `<h1>Sign up</h1><form>'
        '<input name="email" type="email" placeholder="Enter your email">'
        '<input name="password" type="password" placeholder="Create a password">'
        '<button>Go</button></form>`;</script></body></html>')
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/"
        static = specreel.recommend_flows(specreel.crawl(url, max_pages=2))
        rendered = specreel.crawl_browser(url, max_pages=2, wait_ms=400)
        if not rendered:
            pytest.skip("chromium not installed")
        browser = specreel.recommend_flows(rendered)
        assert not static                                  # server HTML misses the JS form
        assert any(f["type"] == "form" for f in browser)   # rendered DOM finds it
    finally:
        httpd.shutdown()


def test_markdown_summary():
    man = {"title": "App", "build": "#3", "flows": [
        {"slug": "a", "title": "A", "demo": "a/demo.html", "steps": 5, "failed": False, "healed": False},
        {"slug": "b", "title": "B", "demo": "b/demo.html", "steps": 3, "failed": True, "healed": False}]}
    md = specreel.markdown_summary(man, url="https://x.io/")
    assert "🔴 1 failing" in md and "<!-- specreel-summary -->" in md
    assert "build #3" in md
    md2 = specreel.markdown_summary(man)          # no url -> plain names, no links
    assert "](http" not in md2


def test_manifest_diff_and_summary_block():
    old = {"flows": [
        {"slug": "a", "title": "A", "failed": False}, {"slug": "b", "title": "B", "failed": False},
        {"slug": "c", "title": "C", "failed": True}, {"slug": "d", "title": "D", "failed": False}]}
    new = {"title": "App", "flows": [
        {"slug": "a", "title": "A", "demo": "a/demo.html", "steps": 5, "failed": True, "healed": False},   # regressed
        {"slug": "b", "title": "B", "demo": "b/demo.html", "steps": 2, "failed": False, "healed": False},  # unchanged
        {"slug": "c", "title": "C", "demo": "c/demo.html", "steps": 4, "failed": False, "healed": False},  # recovered
        {"slug": "e", "title": "E", "demo": "e/demo.html", "steps": 1, "failed": False, "healed": False}]} # added
    diff = specreel.manifest_diff(old, new)
    assert [f["slug"] for f in diff["regressed"]] == ["a"]
    assert [f["slug"] for f in diff["recovered"]] == ["c"]
    assert [f["slug"] for f in diff["added"]] == ["e"]
    assert [f["slug"] for f in diff["removed"]] == ["d"]
    md = specreel.markdown_summary(new, url="https://x.io/", diff=diff)
    assert "Changes vs previous build" in md and "Regressed" in md and "Recovered" in md
    # no diff -> no Changes block
    assert "Changes vs previous build" not in specreel.markdown_summary(new, url="https://x.io/")
    assert "https://x.io/a/demo.html" in md and "❌ failed" in md


def test_release_notes(monkeypatch):
    import json as _json
    man = {"title": "App", "flows": [
        {"slug": "a", "title": "Sign up", "failed": False},
        {"slug": "b", "title": "Checkout", "failed": True}]}
    diff = {"regressed": [{"title": "Checkout", "slug": "b"}], "recovered": [],
            "added": [], "removed": [], "still_failing": []}
    body = specreel.build_notes_request(man, diff, "m", product="App")
    user = _json.loads(body["messages"][0]["content"])
    assert "flows" in user and user["changes_since_last_build"]["regressed"] == ["Checkout"]
    monkeypatch.setattr(specreel, "_anthropic_messages",
                        lambda b, k, timeout=60: {"content": [{"type": "text",
                                                  "text": "## Release\n- Checkout broke"}]})
    assert "Checkout broke" in specreel.release_notes(man, diff, "key")
    # any failure degrades to '' (never raises)
    monkeypatch.setattr(specreel, "_anthropic_messages",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert specreel.release_notes(man, diff, "key") == ""


def test_analytics_injected(tmp_path):
    import shutil as _sh
    src = tmp_path / "t"
    (src / "a").mkdir(parents=True)
    _sh.copy(FIXTURE, src / "a" / "trace.zip")
    cfg = tmp_path / "specreel.yml"
    cfg.write_text("analytics: '<script data-x=\"1\"></script>'\n")
    out = tmp_path / "site"
    specreel.generate_gallery(str(src), str(out), config_path=str(cfg), bundle=True)
    snippet = '<script data-x="1"></script>'
    assert snippet in (out / "index.html").read_text()
    assert snippet in (out / "gallery.html").read_text()
    assert snippet in (out / "a" / "demo.html").read_text()


def test_notify_payload_variants():
    green = [{"failed": False, "healed": False}, {"failed": False, "healed": False}]
    p = specreel.build_notify_payload(green, {"build": "#9"}, "My App", "https://x/")
    assert p["text"].startswith("🟢") and "2 flows" in p["text"]
    assert "2 fresh" in p["text"] and "build #9" in p["text"] and "View demos" in p["text"]

    healed = [{"failed": False, "healed": True}, {"failed": False, "healed": False}]
    assert specreel.build_notify_payload(healed, {}, "App")["text"].startswith("🟡")

    failing = [{"failed": True, "healed": False}, {"failed": False, "healed": False}]
    pf = specreel.build_notify_payload(failing, {}, "App")
    assert pf["text"].startswith("🔴") and "1 failing" in pf["text"]


def test_gallery_skips_notify_without_webhook(tmp_path, monkeypatch):
    # no webhook configured -> notify_slack must never be called
    import shutil as _sh
    monkeypatch.delenv("SPECREEL_SLACK_WEBHOOK", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(specreel, "notify_slack", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    src = tmp_path / "t"
    (src / "a").mkdir(parents=True)
    _sh.copy(FIXTURE, src / "a" / "trace.zip")
    specreel.generate_gallery(str(src), str(tmp_path / "out"))
    assert called["n"] == 0


def test_init_config_scaffold(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces"
    for n in ("one", "two"):
        d = src / n
        d.mkdir(parents=True)
        _sh.copy(FIXTURE, d / "trace.zip")
    out = tmp_path / "specreel.yml"
    assert specreel.init_config(str(src), str(out)) == 0
    cfg = specreel.load_config(str(out))
    assert set(cfg["flows"]) == {"one", "two"}        # slugs match gallery output
    assert specreel.init_config(str(src), str(out)) == 1   # refuses to overwrite


def test_theme_injection(tmp_path):
    rendered = [{"caption": "x", "kind": "nav", "img": "", "dur": 1.0, "failed": False}]
    specreel.build_html(rendered, "T", str(tmp_path), theme="light")
    assert "--bg:#f6f5f1" in (tmp_path / "demo.html").read_text()
    d = tmp_path / "d"; d.mkdir()
    specreel.build_html(rendered, "T", str(d))            # default dark
    assert "--bg:#0c0d0f" in (d / "demo.html").read_text()
    # unknown theme falls back to dark via theme_block
    assert specreel.theme_block("nope") == specreel.theme_block("dark")


def test_find_traces_and_slugify():
    assert specreel.slugify("Add & Filter Todos / chromium") == "add-filter-todos-chromium"
    assert specreel.slugify("") == "demo"


@pytest.mark.parametrize("secs,expected", [
    (0, "0:00"), (8.4, "0:08"), (48.6, "0:49"), (72, "1:12"), (130, "2:10"),
])
def test_fmt_duration(secs, expected):
    assert specreel.fmt_duration(secs) == expected


# --------------------------------------------------------------------------- #
# config: specreel.yml parsing + setup trimming + gallery application
# --------------------------------------------------------------------------- #

def test_parse_yaml_subset():
    text = (
        "title: My Gallery\n"
        "setup_urls:\n"
        "  - /test-api-key\n"
        "  - /login\n"
        "flows:\n"
        "  signup:\n"
        "    title: Sign up\n"
        "    public: true\n"
        "  internal-thing:\n"
        "    hidden: true\n"
        "    public: false\n"
    )
    cfg = specreel._parse_yaml(text)
    assert cfg["title"] == "My Gallery"
    assert cfg["setup_urls"] == ["/test-api-key", "/login"]
    assert cfg["flows"]["signup"] == {"title": "Sign up", "public": True}
    assert cfg["flows"]["internal-thing"] == {"hidden": True, "public": False}


def test_trim_setup_steps():
    steps = [
        {"method": "goto", "params": {"url": "http://x/test-api-key"}},
        {"method": "goto", "params": {"url": "http://x/"}},
        {"method": "click", "params": {"selector": "a"}},
    ]
    out = specreel.trim_setup_steps(steps, ["/test-api-key"])
    assert [s["params"].get("url", s["method"]) for s in out] == ["http://x/", "click"]
    # no patterns -> unchanged
    assert specreel.trim_setup_steps(steps, []) == steps


def test_gallery_honors_config(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces"
    for name in ("alpha", "secret"):
        d = src / name
        d.mkdir(parents=True)
        _sh.copy(FIXTURE, d / "trace.zip")
    cfg = tmp_path / "specreel.yml"
    cfg.write_text(
        "title: Cfg Gallery\n"
        "flows:\n"
        "  alpha:\n"
        "    title: Alpha Flow\n"
        "    public: true\n"
        "  secret:\n"
        "    hidden: true\n"
    )
    out = tmp_path / "site"
    rc = specreel.generate_gallery(str(src), str(out), config_path=str(cfg))
    assert rc == 0
    body = (out / "index.html").read_text()
    assert "Alpha Flow" in body            # title override applied
    assert "Cfg Gallery" in body           # gallery kicker
    assert "public" in body                # public tag rendered
    assert not (out / "secret").exists()   # hidden flow skipped
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["title"] == "Cfg Gallery"
    assert [f["slug"] for f in manifest["flows"]] == ["alpha"]   # secret excluded
    assert manifest["flows"][0]["public"] is True


def _showcase_entry(slug, public, failed):
    return {"slug": slug, "title": slug.replace("-", " ").title(), "public": public,
            "failed": failed, "duration": 12.0, "n_steps": 3, "n_actions": 2,
            "n_checks": 1, "thumb": ""}


def _fake_flow_dirs(out, slugs):
    for slug in slugs:
        d = out / slug
        d.mkdir(parents=True)
        (d / "demo.html").write_text(f"<h1>{slug}</h1>")


def test_showcase_curates_public_passing(tmp_path):
    out = tmp_path / "site"
    _fake_flow_dirs(out, ["pub-ok", "pub-fail", "internal"])
    entries = [_showcase_entry("pub-ok", public=True, failed=False),
               _showcase_entry("pub-fail", public=True, failed=True),
               _showcase_entry("internal", public=False, failed=False)]
    path = specreel.build_showcase(entries, str(out), ctx={"build": "#7"},
                                   cfg={"product_name": "Acme"})
    body = open(path, encoding="utf-8").read()
    # only the passing public flow's files ship — absence, not hiding
    assert (out / "showcase" / "pub-ok" / "demo.html").exists()
    assert not (out / "showcase" / "pub-fail").exists()
    assert not (out / "showcase" / "internal").exists()
    assert "Pub Ok" in body
    assert "Pub Fail" not in body and "Internal" not in body
    assert "failing" not in body.lower()          # no test jargon on the page
    assert "build #7" in body and "verified" in body   # provenance kept
    assert "Acme" in body
    man = json.loads((out / "showcase" / "manifest.json").read_text())
    assert [f["slug"] for f in man["flows"]] == ["pub-ok"]
    assert "failed" not in man["flows"][0]        # curated manifest: no health fields


def test_showcase_skipped_without_includable_flows(tmp_path):
    out = tmp_path / "site"
    _fake_flow_dirs(out, ["a"])
    # a stale showcase/ from a previous build must be removed either way
    (out / "showcase").mkdir()
    (out / "showcase" / "index.html").write_text("stale")
    # nothing marked public
    assert specreel.build_showcase(
        [_showcase_entry("a", public=False, failed=False)], str(out)) is None
    assert not (out / "showcase").exists()
    # public flows exist but every one is failing
    (out / "showcase").mkdir()
    assert specreel.build_showcase(
        [_showcase_entry("a", public=True, failed=True)], str(out)) is None
    assert not (out / "showcase").exists()


def test_gallery_showcase_end_to_end(tmp_path):
    import shutil as _sh
    src = tmp_path / "traces"
    for name in ("signup", "admin"):
        d = src / name
        d.mkdir(parents=True)
        _sh.copy(FIXTURE, d / "trace.zip")
    cfg = tmp_path / "specreel.yml"
    cfg.write_text(
        "title: Acme flows\n"
        "product_name: Acme\n"
        "showcase: true\n"
        "showcase_accent: \"#7c5cff\"\n"
        "flows:\n"
        "  signup:\n"
        "    public: true\n"
    )
    out = tmp_path / "site"
    assert specreel.generate_gallery(str(src), str(out), config_path=str(cfg)) == 0
    body = (out / "showcase" / "index.html").read_text()
    assert (out / "showcase" / "signup" / "demo.html").exists()
    assert not (out / "showcase" / "admin").exists()
    assert "admin" not in body
    assert "--green:#7c5cff" in body              # accent override applied
    # the internal gallery is untouched: both flows, plus the manifest flag
    assert (out / "admin" / "demo.html").exists()
    man = json.loads((out / "manifest.json").read_text())
    assert man["showcase"] is True


def test_accent_css():
    assert specreel._accent_css("") == ""
    hexcss = specreel._accent_css("#7c5cff")
    assert "--green:#7c5cff" in hexcss and "--green-dim:rgba(124,92,255,.09)" in hexcss
    assert "@keyframes pulse" in hexcss           # glow re-tinted too
    named = specreel._accent_css("rebeccapurple")
    assert named == ":root{--green:rebeccapurple}"


def test_build_context_shape():
    ctx = specreel.gather_build_context()
    assert set(ctx) == {"repo", "branch", "build"}   # all keys present, may be empty


# --------------------------------------------------------------------------- #
# Phase 3: AI narration (opt-in) — pure logic only, no network calls
# --------------------------------------------------------------------------- #

def test_build_narration_request_shape():
    rendered = [{"caption": "Open /signup", "kind": "nav"},
                {"caption": "Confirm the heading is visible", "kind": "check"}]
    body = specreel.build_narration_request(rendered, "Sign up", "claude-haiku-4-5")
    assert body["model"] == "claude-haiku-4-5"
    # no banned sampling/thinking params that would 400 on current models
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in body
    # structured outputs + cached system block
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    # the steps are carried in the user message as JSON
    assert "Confirm the heading is visible" in body["messages"][0]["content"]
    # product name is threaded into the prompt when provided
    b2 = specreel.build_narration_request(rendered, "Sign up", "claude-haiku-4-5",
                                          product="Polarys")
    assert "Polarys" in b2["messages"][0]["content"]


def test_parse_narrations():
    resp = {"content": [{"type": "text", "text": '{"narrations": ["a", "b"]}'}]}
    assert specreel.parse_narrations(resp) == ["a", "b"]
    assert specreel.parse_narrations({"content": []}) == []


def test_apply_narration_matches_and_mismatches():
    rendered = [{"caption": "x"}, {"caption": "y"}]
    assert specreel.apply_narration(rendered, ["Open the app", "It loads"]) is True
    assert rendered[0]["narration"] == "Open the app"
    # mismatched count -> no-op, literal captions preserved
    rendered2 = [{"caption": "x"}, {"caption": "y"}]
    assert specreel.apply_narration(rendered2, ["only one"]) is False
    assert "narration" not in rendered2[0]


def test_narration_end_to_end_mocked(tmp_path, monkeypatch):
    # stub the network: return one narration per step, confirm it lands in the HTML
    def fake_call(body, api_key, timeout=60):
        n = len(json.loads(body["messages"][0]["content"])["steps"])
        return {"content": [{"type": "text",
                             "text": json.dumps({"narrations": [f"Step {i+1} narrated"
                                                                for i in range(n)]})}]}
    monkeypatch.setattr(specreel, "_anthropic_messages", fake_call)
    out = tmp_path / "out"
    specreel.generate_demo(FIXTURE, str(out), title="Narrated", verbose=False,
                           ai=True, api_key="test-key")
    body = (out / "demo.html").read_text()
    assert "Step 1 narrated" in body                  # narration shown
    assert 'Open demo.playwright.dev/todomvc' in body  # literal kept as the sub-line


def test_resolve_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert specreel.resolve_api_key() == ""
    assert specreel.resolve_api_key("explicit") == "explicit"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert specreel.resolve_api_key() == "from-env"


def test_ai_without_key_degrades_gracefully(tmp_path, monkeypatch):
    # --ai with no key must still produce a normal demo (literal captions, no crash)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "out"
    stats = specreel.generate_demo(FIXTURE, str(out), title="No key",
                                   verbose=False, ai=True, api_key="")
    assert stats["n_steps"] == 10
    body = (out / "demo.html").read_text()
    assert "Submit" in body            # deterministic captions present
    assert '"lit":""' in body or '"lit": ""' in body  # no narration sublines


def test_captions_escaped_in_player(tmp_path):
    """Trace-derived text (captions/selector names) is untrusted — it must not
    break out of the JSON <script> embed or land raw in innerHTML."""
    evil = '</script><img src=x onerror="alert(1)">'
    rendered = [{"caption": evil, "kind": "action", "img": "", "dur": 1.0, "failed": False}]
    specreel.build_html(rendered, "T", str(tmp_path))
    html = (tmp_path / "demo.html").read_text()
    assert "</script><img" not in html          # can't close the <script> block
    assert "<\\/script>" in html                # JSON-embedded, script-safe form
    assert "const esc=" in html                 # runtime escaping before innerHTML


def test_script_json_escapes_closing_tags():
    out = specreel._script_json({"c": "</script><b>"})
    assert "</script>" not in out and "<\\/script>" in out
    import json as _json
    assert _json.loads(out) == {"c": "</script><b>"}    # round-trips unchanged


def test_gallery_skips_corrupt_trace(tmp_path, capsys):
    """One unreadable trace.zip must not abort the other demos in a gallery."""
    import shutil as _sh
    src = tmp_path / "t"
    (src / "good").mkdir(parents=True)
    _sh.copy(FIXTURE, src / "good" / "trace.zip")
    (src / "bad").mkdir()
    (src / "bad" / "trace.zip").write_bytes(b"this is not a zip file")
    specreel.generate_gallery(str(src), str(tmp_path / "out"))
    assert (tmp_path / "out" / "good" / "demo.html").exists()
    assert not (tmp_path / "out" / "bad" / "demo.html").exists()
    assert "SKIPPED" in capsys.readouterr().err


# ---- capture-coverage guard (systematic "don't miss first/last step") --------

def test_capture_coverage_flags_truncated_end():
    steps = [{"start": 0, "end": 100}, {"start": 200, "end": 300}]
    frames_ok = [{"timestamp": t, "sha1": f"s{t}"} for t in (50, 150, 250, 350)]
    frames_cut = [{"timestamp": t, "sha1": f"s{t}"} for t in (50, 150, 250)]  # none >= 300
    assert specreel.capture_coverage(steps, frames_ok)["issues"] == []
    cut = specreel.capture_coverage(steps, frames_cut)
    assert cut["outcome"] is False and any("last action" in m for m in cut["issues"])


def test_capture_coverage_flags_truncated_opening():
    steps = [{"start": 0, "end": 100}, {"start": 200, "end": 300}]
    frames = [{"timestamp": 50, "sha1": "a"}]           # nothing at/after first_end=100
    cov = specreel.capture_coverage(steps, frames)
    assert cov["opening"] is False and any("open" in m for m in cov["issues"])


def test_capture_coverage_neutral_on_empties():
    assert specreel.capture_coverage([], [])["issues"] == []
    assert specreel.capture_coverage([{"start": 0, "end": 1}], [])["issues"] == []


def test_attach_frames_invariants_first_settled_and_last_is_last():
    """The two systematic guarantees: the last step pins to the trace's last
    frame, and no non-final step shows a frame that predates its own action's
    end (the 'one beat behind' / 'wrong opening frame' regression)."""
    steps = [{"start": 0, "end": 100}, {"start": 200, "end": 300},
             {"start": 400, "end": 500}]
    frames = [{"timestamp": t, "sha1": f"s{t}"} for t in (50, 150, 250, 350, 450, 550)]
    specreel.attach_frames(steps, frames)
    assert steps[-1]["frame"] == "s550"                 # last step == last frame
    by = {f["sha1"]: f["timestamp"] for f in frames}
    for i, s in enumerate(steps[:-1]):                  # every non-final step is settled
        assert by[s["frame"]] >= s["end"], f"step {i} shows a pre-action frame"


# ---- motion clips, quality, click points, url pill ---------------------------

def _mkframes(ts):
    return [{"timestamp": t, "sha1": f"s{t}"} for t in ts]


def test_attach_clip_frames_builds_motion_window():
    steps = [{"start": 0, "end": 100}, {"start": 500, "end": 600}]
    frames = _mkframes([0, 100, 200, 300, 400, 500, 600, 700])
    specreel.attach_frames(steps, frames)
    specreel.attach_clip_frames(steps, frames, 14)
    # step 0's clip covers its own window (before step 1 starts) and ends on its
    # settled frame; timings are present and the first hold is always 0
    assert len(steps[0]["clip"]) > 1
    assert steps[0]["clip"][-1] == steps[0]["frame"]
    assert steps[0]["clip_dts"][0] == 0
    assert len(steps[0]["clip_dts"]) == len(steps[0]["clip"])
    # the final step still ends on the trace's last frame
    assert steps[-1]["clip"][-1] == "s700"


def test_attach_clip_frames_respects_quality_cap():
    steps = [{"start": 0, "end": 10}]
    frames = _mkframes(list(range(0, 2000, 50)))     # 40 frames
    specreel.attach_frames(steps, frames)
    specreel.attach_clip_frames(steps, frames, 6)
    assert len(steps[0]["clip"]) <= 6
    # low quality == a single still (the smallest-file mode)
    specreel.attach_clip_frames(steps, frames, 1)
    assert steps[0]["clip"] == [steps[0]["frame"]]


def test_quality_levels_defined_high_default():
    assert specreel.DEFAULT_QUALITY == "high"
    assert specreel.QUALITY_FRAMES["high"] > specreel.QUALITY_FRAMES["medium"]
    assert specreel.QUALITY_FRAMES["low"] == 1


def test_extract_viewport_and_snapshot_urls():
    events = [{"type": "context-options", "options": {"viewport": {"width": 1280, "height": 800}}},
              {"type": "frame-snapshot", "snapshot": {"callId": "c1", "snapshotName": "before@c1",
                                                      "frameUrl": "http://x/a", "isMainFrame": True}},
              {"type": "frame-snapshot", "snapshot": {"callId": "c1", "snapshotName": "after@c1",
                                                      "frameUrl": "http://x/b", "isMainFrame": True}}]
    assert specreel.extract_viewport(events) == {"width": 1280, "height": 800}
    # the AFTER snapshot wins: a click that navigates reports where it landed
    assert specreel._snapshot_urls(events)["c1"] == "http://x/b"
    assert specreel.final_snapshot_url(events) == "http://x/b"


def test_step_payload_motion_toggle_keeps_bundle_small():
    rendered = [{"caption": "c", "kind": "action", "img": "i2", "imgs": ["i1", "i2"],
                 "dts": [0, 120], "dur": 1.4, "failed": False, "px": 10.0, "py": 20.0,
                 "url": "/x"}]
    full = specreel.step_payload(rendered, motion=True)[0]
    lite = specreel.step_payload(rendered, motion=False)[0]
    assert full["imgs"] == ["i1", "i2"] and full["dts"] == [0, 120]
    assert "imgs" not in lite          # the bundle stays email-sized
    assert lite["img"] == "i2" and lite["px"] == 10.0 and lite["url"] == "/x"


def test_snapshot_urls_ignore_third_party_iframes():
    """Pages embed Stripe/analytics/chat iframes whose snapshots carry their own
    frameUrl. The browser-chrome pill must show the PAGE's address, never an
    embedded frame's (a real demo showed 'm.stripe.network/inner.html?...')."""
    def snap(cid, name, url, main):
        return {"type": "frame-snapshot",
                "snapshot": {"callId": cid, "snapshotName": name,
                             "frameUrl": url, "isMainFrame": main}}
    events = [
        snap("c1", "after@c1", "https://blog.test/", True),
        snap("c1", "after@c1", "https://m.stripe.network/inner.html#x", False),
        snap("c2", "after@c2", "https://blog.test/a-post/", True),
        snap("c2", "after@c2", "https://m.stripe.network/inner.html#y", False),
    ]
    urls = specreel._snapshot_urls(events)
    assert urls["c1"] == "https://blog.test/"
    assert urls["c2"] == "https://blog.test/a-post/"
    # and the flow's end URL is the page's, not the last iframe's
    assert specreel.final_snapshot_url(events) == "https://blog.test/a-post/"


def test_snapshot_urls_skip_about_blank():
    events = [{"type": "frame-snapshot",
               "snapshot": {"callId": "c1", "snapshotName": "before@c1",
                            "frameUrl": "about:blank", "isMainFrame": True}},
              {"type": "frame-snapshot",
               "snapshot": {"callId": "c1", "snapshotName": "after@c1",
                            "frameUrl": "https://blog.test/", "isMainFrame": True}}]
    assert specreel._snapshot_urls(events)["c1"] == "https://blog.test/"


# ---- failure legibility + locator grounding ---------------------------------

def test_humanize_error_explains_a_missing_element():
    err = {"message": "Timeout 4000ms exceeded.", "name": "TimeoutError"}
    why = specreel.humanize_error(err, 'the "Read more" link')
    assert "Couldn't find" in why and '"Read more"' in why and "4s" in why
    assert "Timeout" not in why          # no raw Playwright jargon


def test_humanize_error_explains_an_ambiguous_locator():
    err = {"message": 'Error: strict mode violation: get_by_role("link") resolved to '
                      '2 elements:\n 1) <a>A</a>\n 2) <a>B</a>'}
    why = specreel.humanize_error(err, "the link")
    assert "matched 2 elements" in why and "ambiguous" in why


def test_humanize_error_quiet_when_nothing_useful():
    assert specreel.humanize_error(None) == ""
    assert specreel.humanize_error({"message": ""}) == ""


def test_page_parser_reads_aria_label_for_textless_links():
    """Overlay/icon links carry their accessible name in aria-label — that's what
    get_by_role(name=…) matches, so a crawl that ignores it reports the page as
    having no clickable post titles (and the AI then invents 'Read more')."""
    html_doc = ('<html><body>'
                '<a href="/a/" aria-label="Deploy to Cloud Run"></a>'
                '<a href="/b/">Plain text link</a>'
                '<button aria-label="Open search"><svg></svg></button>'
                '</body></html>')
    p = specreel._PageParser()
    p.feed(html_doc)
    texts = [l["text"] for l in p.links]
    assert "Deploy to Cloud Run" in texts      # from aria-label
    assert "Plain text link" in texts          # normal text still wins
    assert "Open search" in p.buttons


def test_page_labels_dedupes_and_caps():
    pg = {"links": [{"text": "Home"}, {"text": "home"}, {"text": ""}, {"text": "Docs"}],
          "buttons": ["Search"]}
    labels = specreel.page_labels(pg)
    # buttons first (page CTAs), then links — nav used to crowd out "From library"
    assert labels == ["Search", "Home", "Docs"]     # case-insensitive dedupe, no blanks
    many = {"links": [{"text": f"L{i}"} for i in range(40)], "buttons": []}
    assert len(specreel.page_labels(many, limit=5)) == 5


def test_page_labels_prefer_buttons_over_nav_links():
    pg = {"buttons": ["From library", "Run scorecard"],
          "links": [{"text": f"Nav{i}", "href": f"/{i}"} for i in range(30)]}
    labels = specreel.page_labels(pg, limit=10)
    assert labels[0] == "From library" and "Run scorecard" in labels


def test_lint_flow_against_context_catches_invented_controls():
    ctx = [{"url": "https://x/simulations",
            "labels": ["From library", "Add all", "Run scorecard"],
            "buttons": ["From library", "Add all", "Run scorecard"],
            "roles": {"checkbox": 0, "option": 0},
            "fields": [{"placeholder": "Search recent documents…"}]}]
    bad = ('await page.get_by_role("checkbox").nth(0).check()\n'
           'await page.get_by_role("button", name="Select from library").first.click()\n')
    issues = specreel.lint_flow_against_context(bad, ctx)
    assert any("checkbox" in i for i in issues)
    assert any("Select from library" in i for i in issues)
    good = ('await page.get_by_role("button", name="Add all").first.click()\n'
            'await page.get_by_role("button", name="From library").first.click()\n')
    assert specreel.lint_flow_against_context(good, ctx) == []


def test_strip_embedded_login_and_ensure_first():
    """NL sometimes re-emits sign-in (already handled by login_prelude) and omits
    .first — both caused the latest Kumkuat failures."""
    nested = (
        'await page.goto(BASE + "/login")\n'
        'await page.get_by_placeholder("Enter your email").fill("{{EMAIL}}")\n'
        'await page.get_by_role("button", name="Sign In Signing in...").click()\n'
        'await page.goto(BASE + "/simulations")\n'
        'await page.get_by_role("button", name="Add all").nth(0).click()\n'
    )
    out = specreel.normalize_flow_code(nested, "py")
    assert out.startswith('await page.goto(BASE + "/simulations")')
    assert "/login" not in out
    bare = 'await page.get_by_role("button", name="Pick an Audience").click()'
    assert ".first.click()" in specreel.ensure_locator_first(bare)


def test_recommend_flows_carry_real_clickable_labels():
    pages = [{"url": "http://x/", "title": "Blog", "headings": ["Blog"], "forms": [],
              "inputs": [], "buttons": [],
              "links": [{"text": "Deploy to Cloud Run", "href": "/a/"},
                        {"text": "Archive", "href": "/b/"}]}]
    flows = specreel.recommend_flows(pages)
    assert flows and "Deploy to Cloud Run" in flows[0]["labels"]


def test_keyboard_press_humanized():
    """page.keyboard.press lands as 'keyboardPress' — it must not leak the raw
    method name into a caption (the NL resolver is told to use it for scrolling)."""
    cap, kind = specreel.humanize({"method": "keyboardPress", "params": {"key": "End"}})
    assert cap == "Jump to the bottom of the page" and kind == "action"
    assert specreel.humanize({"method": "keyboardPress",
                              "params": {"key": "Escape"}})[0] == "Dismiss with Escape"
    # an unmapped key still reads as English, never as a method name
    cap2, _ = specreel.humanize({"method": "keyboardPress", "params": {"key": "Tab"}})
    assert cap2 == "Press Tab" and "keyboardPress" not in cap2


def test_nl_flow_prompt_forbids_invented_and_fragile_locators():
    """The system prompt is the guardrail against the two failures we hit on a
    real site: an invented link name, and a scroll that breaks mid-navigation."""
    s = specreel.NL_FLOW_SYSTEM
    assert "NEVER invent the text of a link or button" in s
    assert "clickable" in s
    assert "page.evaluate(window.scrollTo" in s or "window.scrollTo" in s
    assert "mouse.wheel" in s
    assert "NEVER invent URL paths" in s
    assert "known_pages is empty" in s
    assert "roles.checkbox" in s
    assert "From library" in s


def test_context_from_recommend_and_merge():
    from cloud import onboard
    raw = {"flows": [
        {"title": "Audiences", "url": "https://app.test/audiences/overview",
         "type": "nav", "labels": ["Pin Audiences"], "fields": []},
        {"title": "bad", "url": "", "labels": [], "fields": []},   # dropped
    ]}
    ctx = onboard.context_from_recommend(raw)
    assert len(ctx) == 1 and ctx[0]["labels"] == ["Pin Audiences"]
    assert onboard.context_from_recommend({"error": "x", "flows": []}) == []
    merged = onboard.merge_flow_context(
        [{"title": "A", "url": "/a"}],
        [{"title": "A", "url": "/a"}, {"title": "B", "url": "/b"}])
    assert [m["title"] for m in merged] == ["A", "B"]


# ---- generated-code safety ---------------------------------------------------

def test_normalize_flow_code_fixes_js_isms_in_python():
    """A model asked for Python sometimes emits JS habits. Unfixed, the flow dies
    with a SyntaxError that has nothing to do with the user's app."""
    code = ('await page.goto(BASE + "/")\n'
            '// TODO: tighten this\n'
            'await expect(page).to_have_title(/Dylan Roy/i)')
    out = specreel.normalize_flow_code(code, "py")
    assert "# TODO: tighten this" in out and "//" not in out
    assert 're.compile("Dylan Roy", re.I)' in out
    compile("async def _f(page, BASE, expect, re):\n" +
            "\n".join("    " + l for l in out.splitlines()), "<f>", "exec")


def test_normalize_flow_code_discards_uncompilable_python():
    assert specreel.normalize_flow_code("await page.click(", "py") == ""
    assert specreel.normalize_flow_code("", "py") == ""


def test_normalize_flow_code_leaves_js_alone_but_fixes_comments():
    out = specreel.normalize_flow_code('# note\nawait page.click("a");', "js")
    assert out.startswith("// note")


def test_nl_prompt_covers_hidden_fields_and_unobservable_content():
    s = specreel.NL_FLOW_SYSTEM
    assert '"visible": false' in s and "opened_by" in s      # open the modal first
    assert "do NOT invent class names" in s


# ---- crawl-quality fixes from the multi-site sweep ---------------------------

def test_page_parser_ignores_svg_titles_and_text():
    """SVG <title> is icon metadata, not page content — Stripe's page title came
    out as '…Stripe logoStripe logoGuidesCard_32' before this."""
    doc = ('<html><head><title>Pricing</title></head><body>'
           '<a href="/x"><svg><title>Stripe logo</title></svg>Pricing</a>'
           '<h1>Plans<svg><title>badge</title></svg></h1>'
           '</body></html>')
    p = specreel._PageParser(); p.feed(doc)
    assert p.title.strip() == "Pricing"
    assert p.headings == ["Plans"]
    assert p.links[0]["text"] == "Pricing"          # no 'Stripe logo' bleed


def test_page_labels_collapse_doubled_responsive_text():
    pg = {"links": [{"text": "Sign inSign in"}, {"text": "Pricing"}], "buttons": []}
    assert specreel.page_labels(pg) == ["Sign in", "Pricing"]


def test_recommend_flows_drops_form_twin_of_a_search():
    """A search box lives inside a <form>, so the same field surfaced as BOTH
    'Fill the X form' and 'Search on X' (4 of 6 real sites tested)."""
    pg = {"url": "http://x/", "title": "Wiki", "headings": ["Wiki"],
          "forms": [{"action": "", "method": "get",
                     "fields": [{"name": "q", "placeholder": "Search here",
                                 "type": "search", "id": ""}]}],
          "inputs": [{"name": "q", "placeholder": "Search here", "type": "search", "id": ""}],
          "buttons": [], "links": []}
    flows = specreel.recommend_flows([pg])
    assert [f["type"] for f in flows] == ["search"]   # one flow, the better one


# ---- sign-in for apps behind a login ----------------------------------------

def _login_page():
    return {"url": "http://app/login", "title": "Log in", "headings": ["Log in"],
            "inputs": [{"name": "email", "placeholder": "you@company.com",
                        "type": "email", "id": ""},
                       {"name": "password", "placeholder": "", "type": "password", "id": ""}],
            "forms": [], "buttons": ["Log in"], "links": []}


def test_find_login_fields_identifies_user_and_password():
    got = specreel.find_login_fields(_login_page())
    assert got["password"]["type"] == "password"
    assert got["user"]["name"] == "email"
    assert got["submit"] == "Log in"


def test_login_prelude_uses_placeholders_never_secrets():
    code = specreel.login_prelude("http://app/login", _login_page(), lang="py")
    assert "{{SPECREEL_USER}}" in code and "{{SPECREEL_PASSWORD}}" in code
    assert "page.goto(\"http://app/login\")" in code
    assert 'name="Log in"' in code            # submits via the real button
    assert "wait_for_url" in code             # SPA auth settle (not just load)
    # a generated prelude must be valid python
    compile("import re\nasync def _f(page):\n" + "\n".join("    " + l for l in code.splitlines()),
            "<p>", "exec")


def test_ensure_login_settle_appends_when_missing():
    weak = ('await page.goto("https://app/login")\n'
            'await page.get_by_role("button", name="Sign In").click()\n'
            'await page.wait_for_load_state("load")')
    hard = specreel.ensure_login_settle(weak)
    assert "wait_for_url" in hard and weak in hard
    # idempotent
    assert specreel.ensure_login_settle(hard) == hard


def test_login_prelude_flags_undetectable_fields():
    blank = {"inputs": [], "forms": [], "buttons": [], "links": []}
    assert "TODO" in specreel.login_prelude("http://app/login", blank, lang="py")


def test_trim_setup_steps_removes_credential_fills_not_just_the_goto():
    """Dropping only the navigation left 'Type demo@acme.test into…' in the demo —
    the test account's address in a shareable artifact."""
    steps = [
        {"method": "goto", "params": {"url": "http://app/login"}},
        {"method": "fill", "params": {"value": "demo@acme.test", "selector": "x"}},
        {"method": "fill", "params": {"value": "pw", "selector": "internal:attr=[type=password]"}},
        {"method": "click", "params": {"selector": 'internal:role=button[name="Log in"i]'}},
        {"method": "goto", "params": {"url": "http://app/dashboard"}},
        {"method": "click", "params": {"selector": 'internal:role=link[name="Reports"i]'}},
    ]
    out = specreel.trim_setup_steps(steps, ["/login"])
    assert [s["method"] for s in out] == ["goto", "click"]
    assert all("demo@acme.test" not in str(s["params"]) for s in out)
    # without a setup pattern nothing is dropped
    assert len(specreel.trim_setup_steps(steps, [])) == 6


def test_empty_after_setup_trim_is_failed(tmp_path):
    """Hosted runs put the login URL in setup_urls. If the flow dies on sign-in,
    every recorded step is trimmed away — that must not ship as a green empty
    'ok' demo (the Kumkuat gallery false-success)."""
    out = tmp_path / "demo"
    stats = specreel.generate_demo(
        FIXTURE, str(out), title="Trimmed", verbose=False,
        setup_urls=["https://demo.playwright.dev/todomvc/"])
    assert stats["n_steps"] == 0
    assert stats["failed"] is True


def test_scaffold_script_prepends_login_to_every_flow():
    """Each flow runs in a fresh context, so one login at the top of the file
    would not carry — it has to repeat per flow."""
    flows = [{"title": "A", "type": "nav", "url": "http://app/a", "heading": "",
              "page_title": "", "fields": [], "labels": []},
             {"title": "B", "type": "nav", "url": "http://app/b", "heading": "",
              "page_title": "", "fields": [], "labels": []}]
    script = specreel.scaffold_script(flows, "http://app", lang="py",
                                      login_steps='await page.goto("http://app/login")')
    assert script.count('await page.goto("http://app/login")') == 2


# ---- login-wall detection ----------------------------------------------------

def _pg(url, inputs=(), links=()):
    return {"url": url, "title": "T", "headings": ["H"], "forms": [],
            "inputs": list(inputs), "buttons": [], "links": list(links)}


def test_detect_login_wall_on_a_sign_in_page():
    pages = [_pg("https://app.test/login",
                 inputs=[{"name": "email", "placeholder": "", "type": "email", "id": ""},
                         {"name": "pw", "placeholder": "", "type": "password", "id": ""}])]
    got = specreel.detect_login_wall(pages)
    assert got["needed"] is True and got["login_url"].endswith("/login")


def test_detect_login_wall_quiet_on_an_open_site():
    pages = [_pg("https://blog.test/", links=[{"text": "Archive", "href": "/a"}]),
             _pg("https://blog.test/a", links=[])]
    assert specreel.detect_login_wall(pages)["needed"] is False


def test_detect_login_wall_suggests_credentials_without_demanding_them():
    """A public site that merely HAS a login page shouldn't block onboarding —
    it should just mention that credentials would unlock more."""
    pages = [_pg("https://shop.test/", links=[{"text": "Log in", "href": "/login"}]),
             _pg("https://shop.test/products"),
             _pg("https://shop.test/login",
                 inputs=[{"name": "pw", "placeholder": "", "type": "password", "id": ""}])]
    got = specreel.detect_login_wall(pages)
    assert got["needed"] is False and "credentials" in got["reason"]


def test_login_prelude_uses_placeholders_not_literals():
    pg = {"inputs": [{"name": "email", "placeholder": "Email", "type": "email", "id": ""},
                     {"name": "password", "placeholder": "", "type": "password", "id": ""}],
          "forms": [], "buttons": ["Log in"]}
    code = specreel.login_prelude("https://app.test/login", pg, "py")
    assert "{{SPECREEL_USER}}" in code and "{{SPECREEL_PASSWORD}}" in code
    assert "hunter2" not in code                      # never a literal credential
    assert 'name="Log in"' in code and "wait_for_url" in code


def test_login_prelude_flags_undetected_fields():
    code = specreel.login_prelude("https://app.test/login",
                                  {"inputs": [], "forms": [], "buttons": []}, "py")
    assert "TODO" in code


def test_crawl_failed_carries_the_reason():
    """A crawl where every page failed must say WHY. Swallowing it produced
    'no pages fetched — is the app running?' for sites that were plainly up."""
    assert issubclass(specreel.CrawlFailed, Exception)
    e = specreel.CrawlFailed("https://x/: TimeoutError: Timeout 20000ms exceeded")
    assert "Timeout" in str(e)


def test_goto_settled_tolerates_self_navigation():
    """SPAs replace the URL on mount; if that lands mid-goto Playwright aborts
    with 'interrupted by another navigation'. That's a race, not a broken site —
    it made kumkuat.ai unscannable from Cloud Run while working locally."""
    class FakePage:
        def __init__(self, fail_times):
            self.fail_times, self.gotos, self.waited = fail_times, 0, False
        def goto(self, url, wait_until=None, timeout=None):
            self.gotos += 1
            if self.gotos <= self.fail_times:
                raise RuntimeError('Page.goto: Navigation to "/" is interrupted '
                                   'by another navigation to "/"')
        def wait_for_load_state(self, state, timeout=None):
            self.waited = True
        def wait_for_timeout(self, ms):
            pass

    pg = FakePage(fail_times=1)
    specreel._goto_settled(pg, "https://app.test/", wait_ms=0)
    assert pg.waited and pg.gotos == 1          # settled, not blindly re-navigated

    # an unrelated error retries once, then propagates
    class Broken(FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            self.gotos += 1
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
    b = Broken(0)
    try:
        specreel._goto_settled(b, "https://nope.test/", wait_ms=0)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "ERR_NAME_NOT_RESOLVED" in str(e) and b.gotos == 2
