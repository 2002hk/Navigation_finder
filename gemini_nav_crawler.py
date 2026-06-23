"""
Gemini Navigation Crawler
==========================

Gemini is a Single Page App (SPA): the sitemap only has "/" and "/app".
So we can't "crawl pages" by URL. Instead we drive the UI like a user,
following click-paths to each action and recording the navigation route.

This validates actions the way you described, e.g.:
    login -> type a prompt -> [Send]
    login -> click '+'      -> [Upload file]
    login -> open sidebar -> hover a chat -> three dots -> [Rename]/[Delete]/[Share]
    login -> account menu -> [Sign out]

Output:
    gemini_navigation_report.json   (machine readable)
    GEMINI_NAVIGATION.md            (human readable documentation)
    nav_screenshots/                (evidence for each step)

NOTE: Destructive actions (Delete/Rename) are only DETECTED, never clicked.
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from playwright.async_api import async_playwright

# Make stdout UTF-8 so it never crashes on special characters (Windows cp1252 fix)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

APP_URL = "https://gemini.google.com/app"
HEADLESS = False
NAV_TIMEOUT = 30000
SCREENSHOT_DIR = "nav_screenshots"

# We attach to a normal Chrome started with remote debugging. Because Chrome is
# launched as a plain process (NOT via automation), there's no "controlled by
# automated test software" bar and navigator.webdriver is false, so Google lets
# you sign in. The crawler then attaches to that SAME running browser over CDP,
# so the logged-in session can never be "lost" between runs.
CDP_PORT = 9222
CDP_PROFILE = os.path.join(os.getcwd(), "gemini_cdp_profile")

# How long to wait (seconds) for the app shell to render after navigation.
LOAD_WAIT_SECONDS = 25


def find_chrome():
    """Locate chrome.exe on Windows."""
    candidates = [
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

# A short prompt we type to make the Send button appear (we never actually send).
SAMPLE_TEXT = "hello"

# ──────────────────────────────────────────────────────────────────────────
# Flow definitions
#
# Each flow is a navigation route to one or more target actions.
# Steps run in order on the SAME page (state builds up). Between flows we
# reload /app to reset to a clean state.
#
# Step types:
#   "click"  - click the first matching element (used for navigation)
#   "hover"  - hover the first matching element (reveals hidden buttons)
#   "type"   - focus first match and type SAMPLE_TEXT
#   "detect" - just check whether the element exists/visible (the validation;
#              never clicks, safe for destructive actions like Delete)
# ──────────────────────────────────────────────────────────────────────────

# Reusable step: open the composer "+" menu (which in the signed-in UI is the
# combined "Upload and tools" menu).
OPEN_PLUS_MENU = {
    "type": "click", "desc": "click the '+' (Upload and tools) button", "selectors": [
        "button[aria-label='Upload and tools']",
        "button[aria-label*='Upload and tools' i]",
        "button[aria-label*='upload' i]",
    ],
}

# The signed-in sidebar is COLLAPSED to an icon rail by default. We must expand it
# (sparkle button) before the recent-conversation list items render.
# Optional: Gemini remembers the expanded state, so on later flows the "Open sidebar"
# button may already be gone. click_optional clicks it if present, else just continues.
EXPAND_SIDEBAR = {
    "type": "click_optional", "desc": "open (expand) the sidebar", "selectors": [
        "[data-test-id='side-nav-sparkle-button']",
        "button[aria-label='Open sidebar']",
        "button[aria-label*='Open sidebar' i]",
    ],
}

# Recent chats load asynchronously, so we WAIT for a conversation, hover it to reveal
# its menu button, then open the menu.
WAIT_RECENT_CHAT = {
    "type": "wait", "desc": "wait for recent conversations to load", "selectors": [
        "[data-test-id='conversation']",
        "[data-test-id='all-conversations'] [role='button']",
        "[data-test-id='all-conversations'] a",
    ],
}
HOVER_RECENT_CHAT = {
    "type": "hover", "desc": "hover the first recent chat", "selectors": [
        "[data-test-id='conversation']",
        "[data-test-id='all-conversations'] [role='button']",
        "[data-test-id='all-conversations'] a",
    ],
}
OPEN_CHAT_MENU = {
    "type": "click", "desc": "click the chat's three-dots menu", "selectors": [
        "[data-test-id='conversation'] button[aria-label*='Open menu' i]",
        "[data-test-id='conversation'] button[aria-label*='more' i]",
        "button[data-test-id='actions-menu-button']",
        "[data-test-id='all-conversations'] button[aria-label*='more' i]",
        "button[aria-label*='Open menu' i]",
        "button[aria-label*='More options' i]",
    ],
}

FLOWS = [
    {
        "action": "Post / Send message",
        "steps": [
            {"type": "type", "desc": "type a prompt in the input box", "selectors": [
                "div.ql-editor[contenteditable='true']",
                "rich-textarea div[contenteditable='true']",
                "div[contenteditable='true']",
                "textarea",
            ]},
            {"type": "detect", "desc": "Send button", "selectors": [
                "button[aria-label*='Send' i]",
                "button.send-button",
                "button[mattooltip*='Send' i]",
                "button:has-text('Send')",
            ]},
        ],
    },
    {
        "action": "Upload / Attach file",
        "steps": [
            OPEN_PLUS_MENU,
            {"type": "detect", "desc": "Upload files option", "selectors": [
                "[data-test-id='local-images-files-uploader-button']",
                "[data-test-id='uploader-images-files-button-basic']",
                "[data-test-id='uploader-drive-button']",
                "button:has-text('Upload files')",
                "[role='menuitem']:has-text('Upload')",
                "input[type='file']",
            ]},
        ],
    },
    {
        "action": "Tools (Canvas / Create image / Deep research)",
        "steps": [
            OPEN_PLUS_MENU,
            {"type": "detect", "desc": "a tool option appears", "selectors": [
                "[data-test-id='toolbox-drawer-item-content']",
                "[data-test-id='more-tools-button']",
                "button:has-text('Canvas')",
                "button:has-text('Create image')",
                "button:has-text('Deep research')",
            ]},
        ],
    },
    {
        "action": "Create new chat",
        "steps": [
            {"type": "detect", "desc": "New chat button", "selectors": [
                "[data-test-id='new-chat-button']",
                "button[aria-label='New chat']",
                "button[aria-label*='New chat' i]",
            ]},
        ],
    },
    {
        "action": "Search chats",
        "steps": [
            {"type": "detect", "desc": "Search chats button", "selectors": [
                "[data-test-id='search-chats-button']",
                "button[aria-label*='Search' i]",
            ]},
        ],
    },
    {
        "action": "Rename a conversation",
        "steps": [EXPAND_SIDEBAR, WAIT_RECENT_CHAT, HOVER_RECENT_CHAT, OPEN_CHAT_MENU,
            {"type": "detect", "desc": "Rename option", "selectors": [
                "button[data-test-id='rename-button']",
                "button:has-text('Rename')",
                "[role='menuitem']:has-text('Rename')",
            ]},
        ],
    },
    {
        "action": "Delete a conversation",
        "steps": [EXPAND_SIDEBAR, WAIT_RECENT_CHAT, HOVER_RECENT_CHAT, OPEN_CHAT_MENU,
            {"type": "detect", "desc": "Delete option", "selectors": [
                "button[data-test-id='delete-button']",
                "button:has-text('Delete')",
                "[role='menuitem']:has-text('Delete')",
            ]},
        ],
    },
    {
        "action": "Share / Export a conversation",
        "steps": [EXPAND_SIDEBAR, WAIT_RECENT_CHAT, HOVER_RECENT_CHAT, OPEN_CHAT_MENU,
            {"type": "detect", "desc": "Share or Export option", "selectors": [
                "button[data-test-id='share-button']",
                "button:has-text('Share')",
                "button:has-text('Export')",
                "[role='menuitem']:has-text('Share')",
                "[role='menuitem']:has-text('Export')",
            ]},
        ],
    },
    {
        "action": "Open Settings",
        "steps": [
            {"type": "click", "desc": "click the Settings gear", "selectors": [
                "button[aria-label='Settings']",
                "button[data-test-id='settings-and-help-button']",
                "button[aria-label*='Settings' i]",
            ]},
            {"type": "detect", "desc": "a settings menu item appears", "selectors": [
                "button:has-text('Theme')",
                "button:has-text('Activity')",
                "button:has-text('Help')",
                "[role='menuitem']",
            ]},
        ],
    },
    {
        "action": "Logout / Sign out",
        "steps": [
            EXPAND_SIDEBAR,
            {"type": "wait", "desc": "wait for the account avatar", "selectors": [
                "a.mavatar-footer-left",
                "[aria-label^='Google Account']",
                "[aria-label*='Google Account' i]",
            ]},
            {"type": "click", "desc": "open the Google account menu (avatar)", "selectors": [
                "a.mavatar-footer-left",
                "a[aria-label*='Google Account' i]",
                "[aria-label^='Google Account']",
                "[aria-label*='Account' i]",
            ]},
            {"type": "detect", "desc": "Sign out option", "selectors": [
                "a:has-text('Sign out')",
                "button:has-text('Sign out')",
                "[aria-label*='Sign out' i]",
            ]},
        ],
    },
]


async def first_visible(page, selectors):
    """Return (locator, selector) of the first visible match in the page OR any iframe.

    Searching frames matters for the Google account popover (Sign out), which Gemini
    renders inside an accounts.google.com iframe.
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
                # Check several matches: the first match may be hidden (e.g. a
                # duplicate OneGoogle avatar) while a later one is the visible target.
                for i in range(min(count, 8)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        return el, sel
            except Exception:
                continue
    return None, None


async def snapshot_visible_buttons(page, limit=120):
    """Capture visible interactive elements (label + data-test-id) to ground the docs
    and to help discover real selectors (e.g. conversation list items)."""
    try:
        return await page.evaluate(
            """(limit) => {
                const out = [];
                const els = document.querySelectorAll(
                    'button, a, [role=menuitem], [role=button], [data-test-id]');
                const seen = new Set();
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const label = (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 50);
                    const testid = el.getAttribute('data-test-id');
                    const key = (testid || '') + '|' + label;
                    if (seen.has(key) || (!label && !testid)) continue;
                    seen.add(key);
                    out.push(testid ? `[${testid}] ${label}` : label);
                    if (out.length >= limit) break;
                }
                return out;
            }""",
            limit,
        )
    except Exception:
        return []


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
                # Poll: menus/lists often render a beat after the triggering click.
                loc, sel = await wait_for_selectors(page, step["selectors"], timeout_s=6)
                found = loc is not None
                step_records.append({
                    "step": desc, "type": stype, "found": found, "selector": sel,
                })
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
                # Click if present; if missing, assume the desired state already holds.
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
                step_records.append({
                    "step": desc, "type": stype, "found": loc is not None, "selector": sel,
                })
                path_taken.append(desc)
                continue

            if stype == "wait":
                # Wait for dynamic content (e.g. conversations) before acting on it.
                loc, sel = await wait_for_selectors(page, step["selectors"], timeout_s=12)
                found = loc is not None
                step_records.append({
                    "step": desc, "type": stype, "found": found, "selector": sel,
                })
                if found:
                    path_taken.append(desc)
                    print(f"   ... wait: {desc} (ready)")
                    continue
                path_taken.append(f"[BLOCKED at] {desc}")
                reached = False
                print(f"   [x] timed out waiting: {desc}")
                break

            # navigation steps: click / hover / type  (poll briefly so late-rendering
            # elements like the composer and the '+' menu button are caught reliably)
            loc, sel = await wait_for_selectors(page, step["selectors"], timeout_s=6)
            if loc is None:
                step_records.append({
                    "step": desc, "type": stype, "found": False, "selector": None,
                })
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
                        # Retry once after a short settle (composer may have re-rendered).
                        await page.wait_for_timeout(1500)
                        loc2, _ = await wait_for_selectors(page, step["selectors"], timeout_s=5)
                        if loc2 is None:
                            raise
                        await loc2.click()
                        await page.keyboard.type(SAMPLE_TEXT)
                await page.wait_for_timeout(1200)
                step_records.append({
                    "step": desc, "type": stype, "found": True, "selector": sel,
                })
                path_taken.append(desc)
                print(f"   ... {stype}: {desc}")
            except Exception as e:
                step_records.append({
                    "step": desc, "type": stype, "found": False,
                    "selector": sel, "error": str(e)[:120],
                })
                path_taken.append(f"[BLOCKED at] {desc}")
                reached = False
                print(f"   [x] failed to {stype}: {desc}")
                break

        # capture what's actually on screen at the end state (for documentation)
        on_screen = await snapshot_visible_buttons(page)

        self.results.append({
            "action": action,
            "reached": reached,
            "navigation_path": " -> ".join(path_taken),
            "steps": step_records,
            "visible_elements_at_end": on_screen,
        })

    async def seed_conversation(self, page):
        """If the account has no saved chats, send one real message so the
        Rename/Delete/Share flows have a conversation to act on. Runs at most once."""
        # Expand the sidebar first so the recent list (if any) renders.
        exp, _ = await first_visible(page, [
            "[data-test-id='side-nav-sparkle-button']", "button[aria-label='Open sidebar']"])
        if exp is not None:
            try:
                await exp.click()
                await page.wait_for_timeout(1500)
            except Exception:
                pass

        existing, _ = await wait_for_selectors(page, ["[data-test-id='conversation']"], timeout_s=6)
        if existing is not None:
            return True  # already have at least one conversation

        print(">> No recent conversations - seeding one (sending a test message)")
        box, _ = await first_visible(page, [
            "div.ql-editor[contenteditable='true']",
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true']",
        ])
        if box is None:
            print("   could not find the composer to seed a chat")
            return False
        try:
            await box.click()
            await page.keyboard.type("Hello Gemini, this is a UI validation test.")
            await page.wait_for_timeout(500)
            send, _ = await first_visible(page, [
                "button[aria-label*='Send' i]", "button.send-button"])
            if send is None:
                print("   could not find Send button to seed a chat")
                return False
            await send.click()
            # Wait for the conversation to be created and appear in Recent.
            conv, _ = await wait_for_selectors(page, ["[data-test-id='conversation']"], timeout_s=30)
            if conv is not None:
                print("   seeded a conversation successfully")
                return True
            print("   sent message but no conversation appeared in time")
            return False
        except Exception as e:
            print(f"   seeding failed: {str(e)[:100]}")
            return False

    async def reset(self, page):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        # Give the SPA time to fully render its shell before the next flow.
        await wait_until_loaded(page, timeout_s=15)
        await page.wait_for_timeout(1500)


def write_markdown(report):
    lines = []
    lines.append("# Gemini Navigation Validation\n")
    lines.append(f"_Generated: {report['generated_at']}_\n")
    lines.append(f"App URL: `{report['app_url']}`\n")
    s = report["summary"]
    lines.append(f"**Reached: {s['reached']} / {s['total']} actions**\n")
    lines.append("\n---\n")
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
    with open("GEMINI_NAVIGATION.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


async def is_logged_in(page):
    """Reliable, Gemini-specific login check.

    Logged in when ALL of:
      - we're on a gemini.google.com page (not accounts.google.com mid-login)
      - no visible 'Sign in' button (signed-out Gemini always shows one)
      - the chat composer has rendered (app shell finished loading)
    """
    try:
        if "gemini.google.com" not in (page.url or ""):
            return False
    except Exception:
        return False
    signin, _ = await first_visible(page, [
        "a:has-text('Sign in')", "button:has-text('Sign in')"])
    if signin is not None:
        return False
    composer, _ = await first_visible(page, [
        "div.ql-editor[contenteditable='true']",
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true']",
    ])
    return composer is not None


async def shell_ready(page):
    """True once the signed-in shell AND the side-nav footer (account avatar) loaded.

    The account avatar is the last thing to render, so it's a reliable 'fully loaded'
    signal that avoids racing the asynchronously-loaded side navigation.
    """
    if not await is_logged_in(page):
        return False
    anchor, _ = await first_visible(page, [
        "[aria-label^='Google Account']",
        "[data-test-id='mavatar-footer-settings-button']",
        "[data-test-id='new-chat-button']",
    ])
    return anchor is not None


async def wait_for_selectors(page, selectors, timeout_s=10):
    """Poll until one of the selectors is visible (in page or a frame). Returns (loc, sel)."""
    waited = 0
    while waited < timeout_s:
        loc, sel = await first_visible(page, selectors)
        if loc is not None:
            return loc, sel
        await page.wait_for_timeout(1000)
        waited += 1
    return await first_visible(page, selectors)


async def wait_until_loaded(page, timeout_s=25):
    """Poll until the full signed-in shell is ready (or timeout)."""
    waited = 0
    while waited < timeout_s:
        if await shell_ready(page):
            await page.wait_for_timeout(1500)  # small settle
            return True
        await page.wait_for_timeout(2000)
        waited += 2
    return await is_logged_in(page)


def serve_mode():
    """Start a normal Chrome with remote debugging so you can log in by hand.

    Chrome is launched as an ordinary process (no automation flags), so Google's
    sign-in works normally. Leave this window OPEN and signed in, then run the
    crawl in another command.
    """
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
    # Detach so Chrome keeps running after this command exits.
    DETACHED = 0x00000008
    NEW_GROUP = 0x00000200
    subprocess.Popen(args, creationflags=DETACHED | NEW_GROUP)

    print("Chrome is starting on debug port", CDP_PORT)
    print("\nNEXT STEPS:")
    print("  1. In the Chrome window that opened, click 'Sign in' and log in.")
    print("  2. Wait until the Gemini chat interface is fully loaded.")
    print("  3. LEAVE that Chrome window OPEN.")
    print("  4. Run:  python gemini_nav_crawler.py")
    print("\n(That Chrome stays logged in, so you only sign in once.)")


async def connect_and_crawl():
    print("Gemini Navigation Crawler (CDP attach)")
    print("=" * 50)

    crawler = NavCrawler()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        except Exception:
            print("Could not connect to Chrome on port", CDP_PORT)
            print("Start it first with:  python gemini_nav_crawler.py --serve")
            print("then sign in and leave the window open.")
            return

        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        # Reuse the existing Gemini tab if there is one; otherwise open the app.
        page = None
        for pg in context.pages:
            try:
                if "gemini.google.com" in (pg.url or ""):
                    page = pg
                    break
            except Exception:
                continue
        if page is None:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)

        try:
            logged_in = await wait_until_loaded(page, timeout_s=LOAD_WAIT_SECONDS)
            await crawler.shot(page, "startup_state")

            if logged_in:
                print("Authenticated session confirmed (signed-in Gemini app loaded).")
                # Ensure a conversation exists so chat-menu flows are reachable.
                await crawler.seed_conversation(page)
                # Clean slate before the first flow (reused tab may be mid-state).
                await crawler.reset(page)
                await page.wait_for_timeout(1500)
            else:
                print("WARNING: Not logged in - Gemini is showing the signed-out UI.")
                print("In the served Chrome window, sign in first, then re-run this.")
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
            with open("gemini_navigation_report.json", "w", encoding="utf-8") as f:
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
            print("Report : gemini_navigation_report.json")
            print("Doc    : GEMINI_NAVIGATION.md")
            print(f"Shots  : {SCREENSHOT_DIR}/")

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
        finally:
            # Do NOT close the user's browser; just detach.
            try:
                await browser.close()
            except Exception:
                pass


def main():
    if "--serve" in sys.argv:
        serve_mode()
    else:
        asyncio.run(connect_and_crawl())


if __name__ == "__main__":
    main()
