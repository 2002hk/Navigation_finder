"""
Generic Navigation Crawler (reusable template)
==============================================

A reusable, app-agnostic version of the Gemini navigation crawler. It drives a real,
logged-in browser via Chrome DevTools Protocol (CDP) and validates that each target
action is reachable, recording the click-path to it.

It works for ANY web app: fill in the CONFIG block and the FLOWS list below.

---------------------------------------------------------------------------
HOW TO USE
---------------------------------------------------------------------------
1) Edit the CONFIG block (APP_NAME, APP_URL, APP_DOMAIN, login signals).

2) Log in once (opens a normal Chrome so the site's sign-in works, no bot block):
       python nav_crawler_template.py --serve
   Sign in, leave that Chrome window OPEN.

3) Discover the REAL selectors (avoids guessing):
       python nav_crawler_template.py --discover
   This dumps every visible interactive element (with data-test-id / aria-label /
   role / text) to "<APP_NAME>_elements.txt" and a screenshot. Use those attributes
   to write your FLOWS selectors.

4) Fill in FLOWS, then run the validation:
       python nav_crawler_template.py

OUTPUT:
   <APP_NAME>_navigation_report.json   (machine readable)
   <APP_NAME>_NAVIGATION.md            (human readable)
   <APP_NAME>_elements.txt             (discovery dump, from --discover)
   nav_screenshots/                    (evidence per action + startup)

NOTE: Destructive actions (Delete/Rename/Sign-out) should use step type "detect"
      so they're only VERIFIED, never actually clicked.
---------------------------------------------------------------------------
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from playwright.async_api import async_playwright

# Make stdout UTF-8 so it never crashes on special characters (Windows cp1252 fix).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ══════════════════════════════════════════════════════════════════════════
# CONFIG  — edit this block per application
# ══════════════════════════════════════════════════════════════════════════

# A short slug used to name the profile dir and output files (no spaces).
APP_NAME = "evernote"

# The signed-in landing URL of the app.
APP_URL = "https://www.evernote.com/client/web"

# A substring that should be in the URL when you're on the APP (not on an SSO/login
# domain). Used to avoid false "logged in" during a mid-login redirect.
APP_DOMAIN = "www.evernote.com"

# Presence of ANY of these (visible) means the user is SIGNED OUT.
SIGNED_OUT_SELECTORS = [
    "a:has-text('Sign in')",
    "button:has-text('Sign in')",
    "a:has-text('Log in')",
    "button:has-text('Login')",
]

# Presence of ANY of these (visible) means the signed-in app shell has fully loaded.
# Pick stable elements that only exist when logged in (use --discover to find them).
READY_SELECTORS = [
    # e.g. "[data-test-id='new-chat-button']", "nav[aria-label='Main']",
]

# Text typed into inputs for "type" steps (we never actually submit unless seeding).
SAMPLE_TEXT = "test"

# Remote-debugging port + local Chrome profile (so you log in only once).
CDP_PORT = 9222
CDP_PROFILE = os.path.join(os.getcwd(), f"{APP_NAME}_cdp_profile")

# How long (seconds) to wait for the app shell to load after navigation.
LOAD_WAIT_SECONDS = 25

# Per-navigation timeout in milliseconds.
NAV_TIMEOUT = 30000

# Derived output paths (usually no need to change).
SCREENSHOT_DIR = "nav_screenshots"
OUTPUT_JSON = f"{APP_NAME}_navigation_report.json"
OUTPUT_MD = f"{APP_NAME}_NAVIGATION.md"
DISCOVER_FILE = f"{APP_NAME}_elements.txt"

# ── Optional one-time SEED ──────────────────────────────────────────────────
# Some actions (rename/delete/share an item) need at least one existing item.
# If your app starts empty, configure this to create one. Set SEED = None to skip.
# "exists_selectors": if any is visible, data already exists (skip seeding).
# "input_selectors" + "submit_selectors": create one item by typing + submitting.
SEED = None
# Example for a chat app:
# SEED = {
#     "exists_selectors": ["[data-test-id='conversation']"],
#     "input_selectors": ["div[contenteditable='true']", "textarea"],
#     "text": "Hello, this is a UI validation test.",
#     "submit_selectors": ["button[aria-label*='Send' i]", "button[type='submit']"],
#     "appears_selectors": ["[data-test-id='conversation']"],
# }

# ══════════════════════════════════════════════════════════════════════════
# FLOWS  — define one entry per action you want to validate
# ══════════════════════════════════════════════════════════════════════════
#
# Each flow: {"action": <name>, "steps": [ <step>, ... ]}
# Step types:
#   "click"          - click first matching element (navigation)
#   "hover"          - hover first match (reveals hidden buttons)
#   "type"           - focus first match and type SAMPLE_TEXT
#   "wait"           - poll up to ~12s for dynamic content to appear, then continue
#   "click_optional" - click if present; if missing, continue (for remembered state)
#   "detect"         - poll up to ~6s and record found/not-found. ALWAYS the LAST step;
#                      use this for the action being validated (never clicks).
#
# Each step: {"type": ..., "desc": "<human description>", "selectors": [ ... ]}
# Put the most specific/stable selector first (prefer data-test-id / aria-label).
#
# EXAMPLE (delete this and write your own):
FLOWS = [
    # {
    #     "action": "Create new item",
    #     "steps": [
    #         {"type": "detect", "desc": "New item button", "selectors": [
    #             "[data-test-id='new-item-button']",
    #             "button[aria-label*='New' i]",
    #         ]},
    #     ],
    # },
    # {
    #     "action": "Delete an item",
    #     "steps": [
    #         {"type": "click_optional", "desc": "open the sidebar", "selectors": [
    #             "button[aria-label*='sidebar' i]"]},
    #         {"type": "wait", "desc": "wait for items to load", "selectors": [
    #             "[data-test-id='item']"]},
    #         {"type": "hover", "desc": "hover the first item", "selectors": [
    #             "[data-test-id='item']"]},
    #         {"type": "click", "desc": "open the item's menu", "selectors": [
    #             "[data-test-id='item'] button[aria-label*='more' i]"]},
    #         {"type": "detect", "desc": "Delete option", "selectors": [
    #             "button:has-text('Delete')", "[role='menuitem']:has-text('Delete')"]},
    #     ],
    # },
]


# ══════════════════════════════════════════════════════════════════════════
# ENGINE  — generally no need to edit below this line
# ══════════════════════════════════════════════════════════════════════════

def find_chrome():
    """Locate chrome.exe on Windows (extend for other OSes if needed)."""
    candidates = [
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
        "/usr/bin/google-chrome",  # Linux
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


async def first_visible(page, selectors):
    """Return (locator, selector) of the first visible match in the page OR any iframe.

    Iterates up to 8 matches per selector because the first match is often a hidden
    duplicate (e.g. an off-screen menu/avatar) while a later one is the visible target.
    Searching frames matters for SSO/account popovers rendered in cross-origin iframes.
    """
    try:
        frames = [page] + [f for f in page.frames]
    except Exception:
        frames = [page]
    for sel in selectors:
        for ctx in frames:
            try:
                loc = ctx.locator(sel)
                count = await loc.count()
                for i in range(min(count, 8)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        return el, sel
            except Exception:
                continue
    return None, None


async def wait_for_selectors(page, selectors, timeout_s=10):
    """Poll until one of the selectors is visible (in page or a frame)."""
    waited = 0
    while waited < timeout_s:
        loc, sel = await first_visible(page, selectors)
        if loc is not None:
            return loc, sel
        await page.wait_for_timeout(1000)
        waited += 1
    return await first_visible(page, selectors)


async def snapshot_visible_elements(page, limit=150):
    """Capture visible interactive elements (data-test-id + role + label/text).
    This grounds the docs in reality and is the key to discovering real selectors."""
    try:
        return await page.evaluate(
            """(limit) => {
                const out = [];
                const els = document.querySelectorAll(
                    'button, a, input, [role=button], [role=menuitem], [role=link], [data-test-id]');
                const seen = new Set();
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const label = (el.getAttribute('aria-label')
                                   || el.getAttribute('placeholder')
                                   || el.innerText || '').trim().slice(0, 60);
                    const testid = el.getAttribute('data-test-id')
                                   || el.getAttribute('data-testid');
                    const role = el.getAttribute('role') || el.tagName.toLowerCase();
                    const key = (testid || '') + '|' + role + '|' + label;
                    if (seen.has(key) || (!label && !testid)) continue;
                    seen.add(key);
                    const tid = testid ? `[testid=${testid}] ` : '';
                    out.push(`${tid}(${role}) ${label}`);
                    if (out.length >= limit) break;
                }
                return out;
            }""",
            limit,
        )
    except Exception:
        return []


async def is_logged_in(page):
    """Logged in when on the app domain, no 'sign in' visible, and a READY element shows."""
    try:
        if APP_DOMAIN not in (page.url or ""):
            return False
    except Exception:
        return False
    signed_out, _ = await first_visible(page, SIGNED_OUT_SELECTORS)
    if signed_out is not None:
        return False
    if not READY_SELECTORS:
        return True  # no ready-signal configured; assume loaded once sign-in is gone
    ready, _ = await first_visible(page, READY_SELECTORS)
    return ready is not None


async def wait_until_loaded(page, timeout_s=LOAD_WAIT_SECONDS):
    """Poll until the signed-in shell is ready (or timeout)."""
    waited = 0
    while waited < timeout_s:
        if await is_logged_in(page):
            await page.wait_for_timeout(1500)  # small settle
            return True
        await page.wait_for_timeout(2000)
        waited += 2
    return await is_logged_in(page)


class NavCrawler:
    def __init__(self):
        self.results = []
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    async def shot(self, page, name):
        try:
            safe = "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()
            path = f"{SCREENSHOT_DIR}/{safe}_{datetime.now().strftime('%H%M%S')}.png"
            await page.screenshot(path=path)
            return path
        except Exception:
            return None

    async def run_flow(self, page, flow):
        action = flow["action"]
        print(f"\n>> {action}")
        path_taken = ["login"]
        step_records = []
        reached = True

        for step in flow["steps"]:
            stype = step["type"]
            desc = step["desc"]

            if stype == "detect":
                loc, sel = await wait_for_selectors(page, step["selectors"], timeout_s=6)
                found = loc is not None
                step_records.append({"step": desc, "type": stype, "found": found, "selector": sel})
                if found:
                    path_taken.append(f"[FOUND] {desc}")
                    await self.shot(page, f"{action}_{desc}")
                    print(f"   [+] {desc}  (selector: {sel})")
                else:
                    path_taken.append(f"[NOT FOUND] {desc}")
                    reached = False
                    print(f"   [-] {desc}  (not found)")
                break  # detect is always the final step of a flow

            if stype == "click_optional":
                loc, sel = await first_visible(page, step["selectors"])
                if loc is not None:
                    try:
                        await loc.click()
                        await page.wait_for_timeout(1200)
                        print(f"   ... click: {desc}")
                    except Exception:
                        pass
                else:
                    print(f"   ... skip (already done): {desc}")
                step_records.append({"step": desc, "type": stype, "found": loc is not None, "selector": sel})
                path_taken.append(desc)
                continue

            if stype == "wait":
                loc, sel = await wait_for_selectors(page, step["selectors"], timeout_s=12)
                found = loc is not None
                step_records.append({"step": desc, "type": stype, "found": found, "selector": sel})
                if found:
                    path_taken.append(desc)
                    print(f"   ... wait: {desc} (ready)")
                    continue
                path_taken.append(f"[BLOCKED at] {desc}")
                reached = False
                print(f"   [x] timed out waiting: {desc}")
                break

            # navigation steps: click / hover / type (poll briefly for late renders)
            loc, sel = await wait_for_selectors(page, step["selectors"], timeout_s=6)
            if loc is None:
                step_records.append({"step": desc, "type": stype, "found": False, "selector": None})
                path_taken.append(f"[BLOCKED at] {desc}")
                reached = False
                print(f"   [x] could not {stype}: {desc} (element missing)")
                break

            try:
                if stype == "click":
                    await loc.click()
                elif stype == "hover":
                    await loc.hover()
                elif stype == "type":
                    try:
                        await loc.click()
                        await page.keyboard.type(SAMPLE_TEXT)
                    except Exception:
                        await page.wait_for_timeout(1500)
                        loc2, _ = await wait_for_selectors(page, step["selectors"], timeout_s=5)
                        if loc2 is None:
                            raise
                        await loc2.click()
                        await page.keyboard.type(SAMPLE_TEXT)
                await page.wait_for_timeout(1200)
                step_records.append({"step": desc, "type": stype, "found": True, "selector": sel})
                path_taken.append(desc)
                print(f"   ... {stype}: {desc}")
            except Exception as e:
                step_records.append({"step": desc, "type": stype, "found": False,
                                     "selector": sel, "error": str(e)[:120]})
                path_taken.append(f"[BLOCKED at] {desc}")
                reached = False
                print(f"   [x] failed to {stype}: {desc}")
                break

        on_screen = await snapshot_visible_elements(page)
        self.results.append({
            "action": action,
            "reached": reached,
            "navigation_path": " -> ".join(path_taken),
            "steps": step_records,
            "visible_elements_at_end": on_screen,
        })

    async def seed_if_needed(self, page):
        """Create one item if the app is empty and SEED is configured. Runs at most once."""
        if not SEED:
            return
        existing, _ = await wait_for_selectors(page, SEED.get("exists_selectors", []), timeout_s=6)
        if existing is not None:
            return
        print(">> No existing items - seeding one")
        box, _ = await first_visible(page, SEED.get("input_selectors", []))
        if box is None:
            print("   could not find an input to seed")
            return
        try:
            await box.click()
            await page.keyboard.type(SEED.get("text", "validation test"))
            await page.wait_for_timeout(500)
            submit, _ = await first_visible(page, SEED.get("submit_selectors", []))
            if submit is None:
                print("   could not find a submit control to seed")
                return
            await submit.click()
            appeared, _ = await wait_for_selectors(page, SEED.get("appears_selectors", []), timeout_s=30)
            print("   seeded successfully" if appeared else "   submitted but item didn't appear in time")
        except Exception as e:
            print(f"   seeding failed: {str(e)[:100]}")

    async def reset(self, page):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await wait_until_loaded(page, timeout_s=15)
        await page.wait_for_timeout(1500)


def write_markdown(report):
    lines = [
        f"# {APP_NAME} Navigation Validation\n",
        f"_Generated: {report['generated_at']}_\n",
        f"App URL: `{report['app_url']}`\n",
        f"**Reached: {report['summary']['reached']} / {report['summary']['total']} actions**\n",
        "\n---\n",
    ]
    for r in report["results"]:
        status = "REACHED" if r["reached"] else "NOT REACHED"
        lines.append(f"\n## {r['action']}  ({status})\n")
        lines.append(f"**Path:** {r['navigation_path']}\n")
        if r.get("steps"):
            lines.append("\nSteps:\n")
            for st in r["steps"]:
                mark = "x" if st.get("found") else " "
                lines.append(f"- [{mark}] ({st['type']}) {st['step']}")
        lines.append("")
    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


async def open_page(browser):
    """Reuse an existing app tab if present, else open APP_URL."""
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    for pg in context.pages:
        try:
            if APP_DOMAIN in (pg.url or ""):
                return pg
        except Exception:
            continue
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    return page


def serve_mode():
    """Start a normal Chrome with remote debugging so you can log in by hand."""
    print("SERVE MODE - starting Chrome for manual login")
    print("=" * 60)
    chrome = find_chrome()
    if not chrome:
        print("ERROR: Chrome not found. Install Google Chrome and retry.")
        return
    os.makedirs(CDP_PROFILE, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CDP_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        APP_URL,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=creationflags) if creationflags else subprocess.Popen(args)
    print(f"Chrome started on debug port {CDP_PORT}")
    print("\nNEXT STEPS:")
    print("  1. Sign in within the Chrome window that opened.")
    print("  2. Wait until the app is fully loaded.")
    print("  3. LEAVE that Chrome window OPEN.")
    print(f"  4. Discover selectors:  python {os.path.basename(__file__)} --discover")
    print(f"  5. Run validation:      python {os.path.basename(__file__)}")


async def connect():
    """Connect to the served Chrome; returns (browser, page) or (None, None)."""
    p = await async_playwright().start()
    try:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    except Exception:
        print(f"Could not connect to Chrome on port {CDP_PORT}.")
        print(f"Start it first:  python {os.path.basename(__file__)} --serve")
        await p.stop()
        return None, None, None
    page = await open_page(browser)
    return p, browser, page


async def discover_mode():
    """Dump all visible interactive elements so you can build real selectors."""
    print("DISCOVER MODE - dumping visible elements")
    print("=" * 50)
    p, browser, page = await connect()
    if not browser:
        return
    try:
        await wait_until_loaded(page, timeout_s=LOAD_WAIT_SECONDS)
        crawler = NavCrawler()
        await crawler.shot(page, "discover_state")
        elements = await snapshot_visible_elements(page, limit=400)
        with open(DISCOVER_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Visible interactive elements on {page.url}\n")
            f.write(f"# Generated {datetime.now().isoformat()}\n\n")
            f.write("\n".join(elements))
        print(f"Wrote {len(elements)} elements to {DISCOVER_FILE}")
        print(f"Screenshot saved in {SCREENSHOT_DIR}/")
        print("Use the [testid=...] / (role) / label values to write your FLOWS selectors.")
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await p.stop()


async def crawl_mode():
    print(f"{APP_NAME} Navigation Crawler (CDP attach)")
    print("=" * 50)
    if not FLOWS:
        print("FLOWS is empty. Run --discover, then define FLOWS at the top of this file.")
    p, browser, page = await connect()
    if not browser:
        return
    crawler = NavCrawler()
    try:
        logged_in = await wait_until_loaded(page, timeout_s=LOAD_WAIT_SECONDS)
        await crawler.shot(page, "startup_state")
        if logged_in:
            print("Authenticated session confirmed (app shell loaded).")
            await crawler.seed_if_needed(page)
            await crawler.reset(page)
        else:
            print("WARNING: Not logged in - app is showing the signed-out UI.")
            print(f"Run:  python {os.path.basename(__file__)} --serve   then sign in.")
            print("Continuing anyway to document the signed-out state...")

        for flow in FLOWS:
            await crawler.run_flow(page, flow)
            await crawler.reset(page)

        reached = sum(1 for r in crawler.results if r["reached"])
        report = {
            "generated_at": datetime.now().isoformat(),
            "app_url": APP_URL,
            "logged_in": logged_in,
            "summary": {"reached": reached, "total": len(crawler.results)},
            "results": crawler.results,
        }
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        write_markdown(report)

        print("\n" + "=" * 50)
        print("NAVIGATION SUMMARY")
        print("=" * 50)
        for r in crawler.results:
            tag = "[REACHED]" if r["reached"] else "[MISSED ]"
            print(f"{tag} {r['action']}")
            print(f"          {r['navigation_path']}")
        print(f"\nReached {reached}/{len(crawler.results)} actions")
        print(f"Report : {OUTPUT_JSON}")
        print(f"Doc    : {OUTPUT_MD}")
        print(f"Shots  : {SCREENSHOT_DIR}/")
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
    finally:
        try:
            await browser.close()  # detaches; does NOT close the user's Chrome
        except Exception:
            pass
        await p.stop()


def main():
    if "--serve" in sys.argv:
        serve_mode()
    elif "--discover" in sys.argv:
        asyncio.run(discover_mode())
    else:
        asyncio.run(crawl_mode())


if __name__ == "__main__":
    main()
