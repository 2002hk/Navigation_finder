"""
Navigation Auto-Discovery Tool
==============================

Give it an app URL and it AUTOMATICALLY extracts the navigations/actions across the
application's pages - no hand-written selectors or FLOWS required.

How it works
------------
- Attaches to a real, logged-in Chrome via CDP (so auth & bot-detection are handled).
- Crawls the app's pages (sitemap.xml + same-domain link BFS). For Single Page Apps
  that don't change URL, it still enumerates the one app shell thoroughly.
- On each page it ENUMERATES every visible interactive element (buttons, links, menu
  items, inputs) with their data-test-id / aria-label / role / text, and CLASSIFIES
  each into an intent (Create, Upload, Delete, Rename, Share, Settings, Logout, ...).
- It also opens "menu" type controls (three-dots / more / overflow / options) to reveal
  NESTED actions, recording the path taken. It NEVER clicks destructive controls
  (Delete, Sign out, etc.) - those are only discovered/recorded.

Usage
-----
1) Log in once (opens a normal Chrome; the site's sign-in works, no bot block):
       python nav_autodiscover.py --serve https://your-app.com

   Sign in, leave that Chrome window OPEN.

2) Auto-discover navigations:
       python nav_autodiscover.py https://your-app.com

Output (named after the app host):
   <host>_navigations.json     machine-readable: every page, action, path, selector
   <host>_NAVIGATIONS.md        human-readable navigation map
   nav_discovery_screenshots/   one screenshot per visited page
"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright

try:
    # line_buffering=True so progress prints appear live (not buffered until exit).
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ── Tunables ────────────────────────────────────────────────────────────────
CDP_PORT = 9222
MAX_PAGES = 20            # safety cap on how many pages to crawl
MAX_PER_TEMPLATE = 2      # max pages to visit per URL template (e.g. /app/<id>)
PAGE_LOAD_WAIT_MS = 3000  # settle time after navigating to a page
MAX_MENUS_PER_PAGE = 10   # how many "more/options" menus to expand per page
LOAD_WAIT_SECONDS = 20
NAV_TIMEOUT = 30000
SCREENSHOT_DIR = "nav_discovery_screenshots"

# ── Intent classification (label/text/test-id -> category). Order matters. ───
CATEGORY_PATTERNS = [
    ("Logout",        [r"\bsign ?out\b", r"\blog ?out\b"]),
    ("Login",         [r"\bsign ?in\b", r"\blog ?in\b"]),
    ("Upload/Attach", [r"\bupload\b", r"\battach\b", r"\bimport\b", r"add file"]),
    ("Download/Export", [r"\bdownload\b", r"\bexport\b", r"save as"]),
    ("Delete",        [r"\bdelete\b", r"\bremove\b", r"\btrash\b", r"\bdiscard\b"]),
    ("Rename",        [r"\brename\b"]),
    ("Edit",          [r"\bedit\b", r"\bmodify\b"]),
    ("Share",         [r"\bshare\b", r"\binvite\b", r"\bpublish\b", r"public link", r"copy link"]),
    ("Create/New",    [r"\bnew\b", r"\bcreate\b", r"\bcompose\b", r"\badd\b", r"\bstart\b"]),
    ("Send/Submit",   [r"\bsend\b", r"\bpost\b", r"\bsubmit\b", r"\breply\b"]),
    ("Search",        [r"\bsearch\b", r"\bfind\b"]),
    ("Settings",      [r"\bsettings\b", r"\bpreferences\b", r"\bconfig"]),
    ("Account/Profile", [r"\baccount\b", r"\bprofile\b", r"\bavatar\b"]),
    ("Menu/More",     [r"\bmore\b", r"\boptions\b", r"overflow", r"kebab", r"three dots",
                       r"⋮", r"…", r"\bmenu\b", r"\bactions\b"]),
    ("Navigation",    [r"\bhome\b", r"\bdashboard\b", r"\bback\b", r"\bnext\b",
                       r"\bsidebar\b", r"\btab\b"]),
]

# Never CLICK these (we still record them as discovered actions).
DESTRUCTIVE = [r"\bsign ?out\b", r"\blog ?out\b", r"\bdelete\b", r"\bremove\b",
               r"\btrash\b", r"\bdiscard\b", r"\bdeactivate\b", r"\bunsubscribe\b"]

# Controls that are safe to click to REVEAL nested actions (and aren't destructive).
MENU_TRIGGER = [r"\bmore\b", r"\boption", r"overflow", r"kebab", r"ellipsis",
                r"three dots", r"⋮", r"…", r"\bmenu\b", r"\bactions\b"]


def matches_any(text, patterns):
    t = (text or "").lower()
    return any(re.search(p, t) for p in patterns)


def classify(text):
    for category, pats in CATEGORY_PATTERNS:
        if matches_any(text, pats):
            return category
    return "Other"


def find_chrome():
    candidates = [
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def host_slug(url):
    host = urlparse(url).netloc.replace(":", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", host) or "app"


def path_template(url):
    """Collapse dynamic id-like path segments to '*' so near-identical pages
    (e.g. /app/<chatId>) share one template and aren't all crawled."""
    p = urlparse(url)
    segs = []
    for s in p.path.split("/"):
        if not s:
            continue
        if (re.fullmatch(r"[0-9a-fA-F]{10,}", s) or re.fullmatch(r"\d{5,}", s)
                or len(s) >= 20 or re.search(r"\d", s) and len(s) >= 12):
            segs.append("*")
        else:
            segs.append(s)
    return f"{p.netloc}/{'/'.join(segs)}"


# ── JS: enumerate visible interactive elements with rich attributes ──────────
ENUMERATE_JS = r"""
() => {
    const out = [];
    const sel = 'button, a[href], a[role=button], input, textarea, select, '
              + '[role=button], [role=menuitem], [role=link], [role=tab], '
              + '[role=checkbox], [role=switch], [role=option], [contenteditable=true], '
              + '[data-test-id], [data-testid]';
    const actionableTags = new Set(['button','a','input','textarea','select']);
    const actionableRoles = new Set(['button','menuitem','link','tab','checkbox','switch','option']);
    const els = document.querySelectorAll(sel);
    const seen = new Set();
    for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') continue;

        const tag = el.tagName.toLowerCase();
        const roleAttr = (el.getAttribute('role') || '').toLowerCase();
        const editable = el.getAttribute('contenteditable') === 'true';
        const clickableStyle = style.cursor === 'pointer';
        const hasTabindex = el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1';
        // Only keep elements that are genuinely actionable - this filters out big
        // wrapper containers that merely carry a data-test-id.
        const actionable = actionableTags.has(tag) || actionableRoles.has(roleAttr)
                           || editable || clickableStyle || hasTabindex;
        if (!actionable) continue;
        // Skip very large elements (likely layout containers, not single controls).
        if (r.width > 0.8 * window.innerWidth && r.height > 0.5 * window.innerHeight) continue;

        const testid = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const ph = el.getAttribute('placeholder') || '';
        const text = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60);
        const label = (aria || title || ph || text).trim().slice(0, 60);
        // Drop entries whose only "label" is a long blob of inner text (containers).
        if (!aria && !title && !ph && text.length > 45 && !testid) continue;
        const role = roleAttr || tag;
        const href = tag === 'a' ? (el.href || '') : '';
        const key = testid + '|' + role + '|' + label + '|' + href;
        if (seen.has(key)) continue;
        if (!label && !testid) continue;
        seen.add(key);
        out.push({ testid, aria, title, placeholder: ph, text, label, role, href });
    }
    return out;
}
"""


def build_selector(el):
    """Build a best-effort, stable selector string for an enumerated element."""
    tid = el.get("testid")
    if tid:
        return f"[data-test-id='{tid}'], [data-testid='{tid}']"
    aria = el.get("aria")
    if aria:
        safe = aria.replace("'", "\\'")
        return f"[aria-label='{safe}']"
    text = (el.get("text") or "").strip()
    if text:
        safe = text.replace("'", "\\'")[:40]
        return f"{el.get('role','*') if el.get('role') in ('button','a') else '*'}:has-text('{safe}')"
    return None


async def get_links(page, base_host):
    """Return same-host page links found on the current page."""
    try:
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    except Exception:
        return set()
    links = set()
    for h in hrefs:
        try:
            u = urlparse(h)
            if u.scheme.startswith("http") and u.netloc == base_host:
                clean = f"{u.scheme}://{u.netloc}{u.path}"  # drop query/fragment
                links.add(clean.rstrip("/") or clean)
        except Exception:
            continue
    return links


def fetch_sitemap_urls(start_url, base_host, limit=40):
    """Best-effort sitemap.xml fetch to seed page URLs (unique paths only)."""
    sm = f"{urlparse(start_url).scheme}://{base_host}/sitemap.xml"
    try:
        req = urllib.request.Request(sm, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read().decode("utf-8", "ignore")
    except Exception:
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", data)
    seen, urls = set(), []
    for loc in locs:
        u = urlparse(loc)
        if u.netloc != base_host:
            continue
        clean = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)
        if len(urls) >= limit:
            break
    return urls


class AutoDiscoverer:
    def __init__(self, start_url, allowed_host=None):
        self.start_url = start_url
        # Crawling is locked to this exact host. Pages that redirect off-host are
        # skipped. You can override it with --host on the command line.
        self.base_host = (allowed_host or urlparse(start_url).netloc).lower()
        self.visited = set()
        self.template_counts = {}  # url-template -> how many visited
        self.pages = []  # list of {url, title, actions:[...]}
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def same_host(self, url):
        try:
            return urlparse(url).netloc.lower() == self.base_host
        except Exception:
            return False

    async def safe_title(self, page):
        try:
            return await page.title()
        except Exception:
            return ""

    async def shot(self, page, name):
        try:
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:60] or "page"
            path = f"{SCREENSHOT_DIR}/{safe}_{datetime.now().strftime('%H%M%S')}.png"
            await page.screenshot(path=path)
            return path
        except Exception:
            return None

    async def enumerate_actions(self, page, page_url):
        """Enumerate + classify every visible interactive element on the current page."""
        try:
            raw = await page.evaluate(ENUMERATE_JS)
        except Exception:
            raw = []
        actions = []
        for el in raw:
            label = el.get("label") or el.get("testid")
            if not label:
                continue
            hay = " ".join([el.get("aria", ""), el.get("text", ""),
                            el.get("title", ""), el.get("testid", "")])
            actions.append({
                "label": label,
                "category": classify(hay),
                "role": el.get("role"),
                "testid": el.get("testid") or None,
                "href": el.get("href") or None,
                "selector": build_selector(el),
                "path": page_url,
                "via": "page",
            })
        return actions, raw

    async def expand_menus(self, page, page_url, raw_elements):
        """Click safe 'more/options' menus to reveal nested actions, then Escape."""
        nested = []
        # Pick menu-trigger elements that are safe (not destructive) and selectable.
        triggers = []
        for el in raw_elements:
            hay = " ".join([el.get("aria", ""), el.get("text", ""), el.get("testid", "")])
            if matches_any(hay, MENU_TRIGGER) and not matches_any(hay, DESTRUCTIVE):
                selector = build_selector(el)
                if selector:
                    triggers.append((el.get("label") or el.get("testid"), selector))
            if len(triggers) >= MAX_MENUS_PER_PAGE:
                break

        # Snapshot of labels visible before opening any menu (to detect new items).
        before = {a["label"] for a in (await self.enumerate_actions(page, page_url))[0]}

        for trigger_label, selector in triggers:
            try:
                loc = page.locator(selector).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click()
                await page.wait_for_timeout(900)

                # If the click navigated the page (or went off-host), restore and stop
                # touching menus on this page - the state is no longer reliable.
                if page.url != page_url:
                    try:
                        await page.goto(page_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                        await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
                    except Exception:
                        pass
                    break

                after_actions, _ = await self.enumerate_actions(page, page_url)
                for a in after_actions:
                    if a["label"] in before:
                        continue
                    a = dict(a)
                    a["path"] = f"{page_url} -> open '{trigger_label}'"
                    a["via"] = f"menu:{trigger_label}"
                    nested.append(a)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return nested

    async def visit(self, page, url):
        if url in self.visited or len(self.visited) >= MAX_PAGES:
            return set()
        # Don't crawl many near-identical pages (e.g. lots of /app/<id> chats).
        tmpl = path_template(url)
        if self.template_counts.get(tmpl, 0) >= MAX_PER_TEMPLATE:
            return set()
        self.visited.add(url)
        self.template_counts[tmpl] = self.template_counts.get(tmpl, 0) + 1
        print(f"\n[page {len(self.visited)}/{MAX_PAGES}] {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        except Exception as e:
            print(f"   could not load: {str(e)[:80]}")
            return set()

        # Skip pages that redirected to a DIFFERENT host (e.g. /faq -> support.google.com).
        if not self.same_host(page.url):
            print(f"   skipped: redirected off-host to {urlparse(page.url).netloc}")
            return set()

        # The whole per-page step is wrapped so one bad page can't abort the run.
        try:
            title = await self.safe_title(page)
            await self.shot(page, f"{urlparse(url).path or 'home'}")

            actions, raw = await self.enumerate_actions(page, url)
            nested = await self.expand_menus(page, url, raw)
            all_actions = actions + nested

            # Deduplicate by (label, category, path)
            uniq, seen = [], set()
            for a in all_actions:
                k = (a["label"], a["category"], a["path"])
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(a)

            by_cat = {}
            for a in uniq:
                by_cat.setdefault(a["category"], 0)
                by_cat[a["category"]] += 1
            summary = ", ".join(f"{c}:{n}" for c, n in sorted(by_cat.items()))
            print(f"   {len(uniq)} navigations  ({summary})")
        except Exception as e:
            print(f"   error while reading page (skipped): {str(e)[:80]}")
            return set()

        self.pages.append({"url": url, "title": title, "actions": uniq})
        return await get_links(page, self.base_host)

    async def run(self, page):
        # Seed queue: start URL + sitemap URLs.
        queue = [self.start_url]
        for u in fetch_sitemap_urls(self.start_url, self.base_host):
            if u not in queue:
                queue.append(u)

        while queue and len(self.visited) < MAX_PAGES:
            url = queue.pop(0)
            new_links = await self.visit(page, url)
            for link in new_links:
                if link not in self.visited and link not in queue:
                    queue.append(link)


def write_reports(start_url, discoverer):
    host = host_slug(start_url)
    total_actions = sum(len(p["actions"]) for p in discoverer.pages)

    report = {
        "generated_at": datetime.now().isoformat(),
        "start_url": start_url,
        "pages_crawled": len(discoverer.pages),
        "total_navigations": total_actions,
        "pages": discoverer.pages,
    }
    with open(f"{host}_navigations.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    lines = [
        f"# Navigation Map: {start_url}\n",
        f"_Generated: {report['generated_at']}_\n",
        f"**Pages crawled: {report['pages_crawled']} | Navigations found: {total_actions}**\n",
        "\n> Destructive actions (Delete, Sign out, ...) are detected but never triggered.\n",
        "\n---\n",
    ]
    for pg in discoverer.pages:
        lines.append(f"\n## {pg['title'] or '(untitled)'}")
        lines.append(f"`{pg['url']}`\n")
        # group by category
        by_cat = {}
        for a in pg["actions"]:
            by_cat.setdefault(a["category"], []).append(a)
        for cat in sorted(by_cat.keys()):
            lines.append(f"\n**{cat}**")
            for a in by_cat[cat]:
                loc = a["selector"] or "(no stable selector)"
                via = "" if a["via"] == "page" else f"  _(via {a['via']})_"
                lines.append(f"- {a['label']}  ->  `{loc}`{via}")
        lines.append("")
    with open(f"{host}_NAVIGATIONS.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return host, total_actions


# ── CDP serve / connect ──────────────────────────────────────────────────────

def get_url_arg(default=None):
    for a in sys.argv[1:]:
        if a.startswith("http://") or a.startswith("https://"):
            return a
    return default


def serve_mode(url):
    print("SERVE MODE - starting Chrome for manual login")
    print("=" * 60)
    chrome = find_chrome()
    if not chrome:
        print("ERROR: Chrome not found. Install Google Chrome and retry.")
        return
    profile = os.path.join(os.getcwd(), f"{host_slug(url)}_cdp_profile")
    os.makedirs(profile, exist_ok=True)
    args = [chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", url]
    creationflags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    import subprocess
    subprocess.Popen(args, creationflags=creationflags) if creationflags else subprocess.Popen(args)
    print(f"Chrome started on debug port {CDP_PORT} with profile {profile}")
    print("\nNEXT STEPS:")
    print("  1. Sign in within the Chrome window that opened.")
    print("  2. Leave that Chrome window OPEN.")
    print(f"  3. Run:  python {os.path.basename(__file__)} {url}")


async def discover_mode(url, allowed_host=None):
    allowed_host = (allowed_host or urlparse(url).netloc).lower()
    print("Navigation Auto-Discovery")
    print("=" * 50)
    print(f"Target: {url}")
    print(f"Locked to host: {allowed_host}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        except Exception:
            print(f"Could not connect to Chrome on port {CDP_PORT}.")
            print(f"Start it first:  python {os.path.basename(__file__)} --serve {url}")
            return

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            discoverer = AutoDiscoverer(url, allowed_host=allowed_host)
            await discoverer.run(page)
            host, total = write_reports(url, discoverer)
            print("\n" + "=" * 50)
            print("DISCOVERY COMPLETE")
            print("=" * 50)
            print(f"Pages crawled : {len(discoverer.pages)}")
            print(f"Navigations   : {total}")
            print(f"Report (JSON) : {host}_navigations.json")
            print(f"Map (Markdown): {host}_NAVIGATIONS.md")
            print(f"Screenshots   : {SCREENSHOT_DIR}/")
        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
        finally:
            try:
                await browser.close()  # detach only; user's Chrome stays open
            except Exception:
                pass


def get_flag_value(name):
    """Return the value after a flag like --host (supports --host=value too)."""
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main():
    url = get_url_arg()
    allowed_host = get_flag_value("--host")  # optional explicit hostname lock
    if "--serve" in sys.argv:
        if not url:
            print("Usage: python nav_autodiscover.py --serve <app_url>")
            return
        serve_mode(url)
    else:
        if not url:
            print("Usage: python nav_autodiscover.py <app_url> [--host <hostname>]")
            print("First time:  python nav_autodiscover.py --serve <app_url>  (to sign in)")
            return
        asyncio.run(discover_mode(url, allowed_host=allowed_host))


if __name__ == "__main__":
    main()
