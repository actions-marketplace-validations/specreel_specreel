# `recommend` — flow discovery for new apps

No Playwright tests yet? Point Specreel at your **running app** and it will crawl
it, suggest demo-worthy flows, and scaffold a runnable Playwright script — so you
go from "an app" to "a gallery" without writing the first flow by hand.

```bash
python specreel.py recommend http://localhost:3000 --max 10
```
```
  crawling http://localhost:3000 (max 10 pages) ...
  found 6 pages → 5 suggested flows:
    1. [form]   Fill & submit the Sign up form  (/signup · 3 fields)
    2. [form]   Fill & submit the Login form  (/login · 2 fields)
    3. [search] Search on Docs  (/docs · 1 field)
    4. [nav]    Open Pricing  (/pricing)
    5. [nav]    Open Features  (/features)
  scaffolded -> specreel_flows.py
  edit the TODOs, then:  python specreel_flows.py && specreel.py test-results -o site --bundle
```

## What it does
1. **Crawls** the app, same-origin, breadth-first (stdlib `urllib` + `html.parser`).
2. **Maps** each page: title, headings, forms (+ fields), search boxes, nav links.
3. **Ranks** candidate flows: **forms > search > nav**.
4. **Scaffolds** a Playwright script with tracing on, real selectors, and sample
   values pulled from the DOM — each flow writes `test-results/<slug>/trace.zip`.

## The full loop
```bash
python specreel.py recommend http://localhost:3000   # 1. suggest + scaffold
# (edit the TODOs in specreel_flows.py — add assertions / clicks you care about)
python specreel_flows.py                             # 2. run -> test-results/*/trace.zip
python specreel.py test-results -o site --bundle     # 3. render the gallery
```

## Options
- `--max N` — pages to crawl (default 12).
- `--lang py|js` — scaffold language (auto-detected: `package.json` → `js`, else `py`).
- `-o FILE` — output path.
- `--ai` `[--api-key KEY]` — curate & rename the suggestions with AI (BYO key).
  It only reorders/renames the discovered flows — it never invents new ones.

## Server HTML vs. rendered DOM
By default `recommend` reads the **server HTML** (fast, zero-dep). For
**client-rendered SPAs** (React/Vue/etc., where the initial HTML is near-empty),
add `--browser` to render each page with Playwright first and read the live DOM:
```bash
python specreel.py recommend http://localhost:5173 --browser
```
Needs Playwright (`pip install playwright && playwright install chromium`) — the
same thing you need to run the scaffold. If a static crawl finds nothing, the CLI
tells you to retry with `--browser`. Use `--wait <ms>` to give a slow SPA more time.

## Logged-in crawls
Public pages crawl out of the box. For pages behind a login, pass your session as a
cookie (or any header) — it's sent with every request, in both static and
`--browser` mode:
```bash
python specreel.py recommend http://localhost:3000 --cookie "session=abc; csrf=xyz"
python specreel.py recommend http://localhost:3000 --header "Authorization: Bearer <token>"
```
Grab the cookie from your browser's devtools (Application → Cookies) after logging in.

The scaffold is a starting point, not a finished suite — it leaves `TODO`s for the
assertions and submit clicks only you can decide — but it turns a blank page into
editable, runnable flows in one command.
