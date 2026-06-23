"""
CASB Navigation + XPath Crawler
================================

Auto-discovers CASB-relevant activities (login, upload_file, download_file,
post, edit, delete, share, logout, ...) across a web application and records
the full click-path to each activity with XPath, CSS selector, and HTML snippet
for every step — suitable for casb_automation test scripts.

Based on CASB activity types from the project tracker (see
.cursor/skills/casb-status/reference.md).

Features
--------
- Attaches to a real logged-in Chrome via CDP (same workflow as nav_autodiscover).
- Traverses every tab on the home/landing page FIRST, then crawls other pages.
- Hovers chat/note rows to reveal three-dot overflow menus (delete, rename, edit, share).
- Expands safe menu/overflow controls to reveal nested actions.
- Records XPath + selector + outerHTML for each navigation step.
- Never clicks destructive controls (Delete, Sign out, …) — only detects them.
- For Gen-AI apps (ChatGPT, Claude, Gemini, …), seeds download_file by sending a
  chat prompt to generate a downloadable file, then records download controls from
  the response (sidebar titles containing "download" are ignored).
- If a click opens or navigates to a different domain, closes the off-domain
  page/tab and returns to the original app URL.

Usage
-----
1) Log in once:
       python casb_nav_xpath_crawler.py --serve https://www.evernote.com/client/web

2) Discover activities + xpath paths:
       python casb_nav_xpath_crawler.py https://www.evernote.com/client/web

   Evernote uses hash routes (/client/web#/tasks, #/files, #/calendar, #/templates).
   The crawler seeds those routes and probes the sidebar after the home page loads.

Output (named after app host):
   <host>_casb_navigations.json
   <host>_CASB_NAVIGATIONS.md
   casb_xpath_screenshots/
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from playwright.async_api import async_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ── Tunables ────────────────────────────────────────────────────────────────
CDP_PORT = 9222
MAX_PAGES = 20
MAX_PER_TEMPLATE = 2
PAGE_LOAD_WAIT_MS = 2500
MAX_MENUS_PER_PAGE = 8
MAX_TABS_PER_PAGE = 8
MAX_HOVER_ITEMS = 6          # chat/note rows to hover for hidden overflow menus
MAX_OVERFLOW_PER_ITEM = 2    # three-dot menus to try per hovered row
NAV_TIMEOUT = 30000
APP_SHELL_WAIT_SECONDS = 45
MIN_INTERACTIVE_ELEMENTS = 3
SCREENSHOT_DIR = "casb_xpath_screenshots"
HTML_SNIPPET_MAX = 500

# URL path patterns to skip during BFS (dynamic chat threads, app connectors, etc.)
SKIP_CRAWL_PATH_RE = [
    re.compile(r"^/c(?:/|$)"),           # ChatGPT conversation threads
    re.compile(r"^/g(?:/|$)"),           # group/shared chats
    re.compile(r"^/share(?:/|$)"),       # shared links
    re.compile(r"^/chat(?:/|$)"),         # Claude chat threads
    re.compile(r"^/apps/[^/]+/.+"),      # individual app connector pages
    re.compile(r"^/apps/[^/]+$"),        # per-vendor app landing pages
]

# SPA entry URLs (e.g. /new, /app) — do NOT restrict BFS to that segment.
SPA_ENTRY_SEGMENTS = frozenset({
    "new", "app", "home", "index", "dashboard", "web", "client",
})

# When start URL has no dedicated app prefix, only crawl these in-app routes.
ROOT_APP_ROUTES_RE = re.compile(
    r"^/(?:"
    r"library|projects|apps|gpts|codex|"                              # ChatGPT
    r"new|artifacts|recents|customize|downloads|upgrade|projects"       # Claude
    r")?$"
)

# Routes to seed per host (root-domain SPAs only; filtered by crawlable_url).
APP_ROUTE_SEEDS_BY_HOST = {
    "chatgpt.com": ("/library", "/projects", "/apps", "/gpts", "/codex"),
    "claude.ai": ("/new", "/artifacts", "/projects", "/recents", "/customize"),
}

# Apps whose in-app navigation uses hash routes (e.g. Evernote /client/web#/tasks).
HASH_ROUTER_ROOTS = frozenset({"evernote.com"})

# Auto path prefix when start URL uses SPA entry segments (e.g. /client/web on Evernote).
APP_PATH_PREFIX_BY_ROOT = {
    "evernote.com": "/client",
}

# Known hash routes to seed before sidebar discovery (fragment includes leading slash).
APP_HASH_ROUTE_SEEDS_BY_ROOT = {
    "evernote.com": (
        "/notes", "/tasks", "/files", "/calendar", "/templates", "/home",
    ),
}

# Evernote sidebar button ids -> hash fragment (qa-NAV_* elements).
EVERNOTE_NAV_ID_TO_FRAGMENT = {
    "qa-NAV_TASK_KINGDOM": "/tasks",
    "qa-NAV_FILES": "/files",
    "qa-NAV_CALENDAR": "/calendar",
    "qa-NAV_TEMPLATES": "/templates",
    "qa-NAV_SHORTCUTS": "/shortcuts",
    "qa-NAV_NOTES": "/notes",
    "qa-NAV_HOME": "/home",
}

# Gen-AI apps: download_file needs a generated file in chat (not sidebar titles containing "download").
GEN_AI_ROOT_DOMAINS = frozenset({
    "chatgpt.com", "claude.ai", "gemini.google.com",
    "perplexity.ai", "deepseek.com", "copilot.microsoft.com",
    "grok.com", "x.com", "rytr.me", "synthesia.io",
})

GEN_AI_CHAT_URLS = {
    "chatgpt.com": "/",
    "claude.ai": "/new",
    "gemini.google.com": "/app",
    "perplexity.ai": "/",
    "deepseek.com": "/",
    "copilot.microsoft.com": "/",
    "grok.com": "/",
    "x.com": "/",
    "rytr.me": "/",
    "synthesia.io": "/",
}

DOWNLOAD_FILE_PROMPT = (
    "Create a small text file named casb_test.txt containing the word hello "
    "and provide a download link or download button so I can download it."
)

CHAT_INPUT_SELECTORS = [
    "div[data-testid='prompt-textarea']",
    "#prompt-textarea",
    "textarea[data-testid='prompt-textarea']",
    "div.ProseMirror[contenteditable='true']",
    "div.ql-editor[contenteditable='true']",
    "rich-textarea div[contenteditable='true']",
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true']",
    "textarea",
]

SEND_BUTTON_SELECTORS = [
    "[data-testid='send-button']",
    "button[data-testid='send-button']",
    "button[aria-label*='Send message' i]",
    "button[aria-label^='Send' i]",
    "button[aria-label*='Send' i]:not([aria-label*='feedback' i])",
    "button.send-button",
    "button[mattooltip*='Send' i]",
]

GENERATING_SELECTORS = [
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop generating' i]",
    "button[aria-label*='Stop response' i]",
    "button[aria-label^='Stop' i]",
]

GEN_AI_RESPONSE_WAIT_SECONDS = 90

# Dedicated app pages where download controls exist without chat seeding (Codex installer, etc.)
GEN_AI_DOWNLOAD_PAGE_RE = re.compile(r"/(?:downloads|codex|export)(?:/|$)", re.I)

# Action-like download labels (not conversation titles containing "download" as a word).
DOWNLOAD_ACTION_LABEL_RE = [
    re.compile(r"^download(?:\s+(?:file|for|as|all|now|image|pdf|csv|zip|txt))?\s*$", re.I),
    re.compile(r"^export(?:\s+(?:file|as|chat|conversation|all))?\s*$", re.I),
    re.compile(r"^save as\s", re.I),
    re.compile(r"^download\s", re.I),
    re.compile(r"^export\s", re.I),
]

SIDEBAR_OPEN_SELECTORS = [
    "[data-testid='pin-sidebar-toggle'][aria-label*='Open sidebar' i]",
    "[data-testid='pin-sidebar-toggle'][aria-label*='Show sidebar' i]",
    "[aria-label*='Open sidebar' i]",
    "[aria-label*='Show sidebar' i]",
]

# CASB activity types (reference.md column B)
CASB_ACTIVITIES = {
    "login", "login_successful", "login_failed", "logout",
    "upload_file", "download_file", "post", "edit", "delete", "share",
    "create", "rename", "search", "settings", "navigation", "other",
}

# Category classification (label/text/test-id -> UI category). Order matters.
CATEGORY_PATTERNS = [
    ("Logout", [r"\bsign ?out\b", r"\blog ?out\b"]),
    ("Login", [r"\bsign ?in\b", r"\blog ?in\b"]),
    ("Upload/Attach", [r"\bupload\b", r"\battach\b", r"\bimport\b", r"add file"]),
    ("Download/Export", [r"\bdownload\b", r"\bexport\b", r"save as", r"\bsave\b"]),
    ("Delete", [r"\bdelete\b", r"\bremove\b", r"\btrash\b", r"\bdiscard\b"]),
    ("Rename", [r"\brename\b"]),
    ("Edit", [r"\bedit\b", r"\bmodify\b"]),
    ("Share", [r"\bshare\b", r"\binvite\b", r"\bpublish\b", r"public link", r"copy link"]),
    ("Create/New", [r"\bnew\b", r"\bcreate\b", r"\bcompose\b", r"\badd note\b", r"\bstart\b"]),
    ("Send/Submit", [r"\bsend\b", r"\bpost\b", r"\bsubmit\b", r"\breply\b"]),
    ("Search", [r"\bsearch\b", r"\bfind\b", r"\bfilter\b"]),
    ("Settings", [r"\bsettings\b", r"\bpreferences\b", r"\bconfig"]),
    ("Account/Profile", [r"\baccount\b", r"\bprofile\b", r"\bavatar\b"]),
    ("Menu/More", [r"\bmore\b", r"\boptions\b", r"overflow", r"kebab", r"three dots",
                   r"⋮", r"…", r"\bmenu\b", r"\bactions\b"]),
    ("Navigation", [r"\bhome\b", r"\bdashboard\b", r"\bback\b", r"\bnext\b",
                     r"\bsidebar\b", r"\btab\b", r"\bnotes\b", r"\btasks\b"]),
]

# Map UI category -> CASB activity name (column B in tracker)
CATEGORY_TO_CASB = {
    "Login": "login",
    "Logout": "logout",
    "Upload/Attach": "upload_file",
    "Download/Export": "download_file",
    "Delete": "delete",
    "Rename": "rename",
    "Edit": "edit",
    "Share": "share",
    "Create/New": "create",
    "Send/Submit": "post",
    "Search": "search",
    "Settings": "settings",
    "Account/Profile": "settings",
    "Menu/More": "other",
    "Navigation": "navigation",
    "Other": "other",
}

DESTRUCTIVE = [
    r"\bsign ?out\b", r"\blog ?out\b", r"\bdelete\b", r"\bremove\b",
    r"\btrash\b", r"\bdiscard\b", r"\bdeactivate\b", r"\bunsubscribe\b",
]

MENU_TRIGGER = [
    r"\bmore\b", r"\boption", r"overflow", r"kebab", r"ellipsis",
    r"three dots", r"⋮", r"…", r"\bmenu\b", r"\bactions\b",
]

# Overflow controls revealed on hover (CASB: hover chat -> three dots -> delete/share/edit).
ITEM_OVERFLOW_TRIGGER = MENU_TRIGGER + [
    r"organize", r"conversation options", r"chat options", r"note options",
    r"show more", r"item menu", r"trailing", r"popover", r"dustbin",
]

# Categories we always want from hover menus (matches Win Br3 tracker activities).
CASB_MENU_CATEGORIES = frozenset({"Delete", "Rename", "Edit", "Share", "Download/Export"})

# ── JS helpers injected into the page ───────────────────────────────────────

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

    function getXPath(el) {
        if (!el || el.nodeType !== 1) return '';
        const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid');
        if (testId) {
            return "//*[@data-test-id='" + testId + "' or @data-testid='" + testId + "']";
        }
        const aria = el.getAttribute('aria-label');
        if (aria) {
            const safe = aria.replace(/'/g, "\\'");
            const tag = el.tagName.toLowerCase();
            return "//" + tag + "[@aria-label='" + safe + "']";
        }
        if (el.id) {
            return '//*[@id="' + el.id.replace(/"/g, '\\"') + '"]';
        }
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1) {
            let idx = 1;
            let sib = node.previousElementSibling;
            while (sib) {
                if (sib.tagName === node.tagName) idx++;
                sib = sib.previousElementSibling;
            }
            parts.unshift(node.tagName.toLowerCase() + '[' + idx + ']');
            node = node.parentElement;
        }
        return '/' + parts.join('/');
    }

    function visibleLabel(el) {
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const ph = el.getAttribute('placeholder') || '';
        const sr = el.querySelector('.sr-only, [class*="sr-only"]');
        const srText = sr ? (sr.innerText || sr.textContent || '').trim() : '';
        const text = (el.innerText || '').trim().replace(/\s+/g, ' ');
        return (aria || title || ph || srText || text).trim().slice(0, 60);
    }

    function snippet(el) {
        const html = el.outerHTML || '';
        return html.length > 500 ? html.slice(0, 500) + '…' : html;
    }

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
        const actionable = actionableTags.has(tag) || actionableRoles.has(roleAttr)
                           || editable || clickableStyle || hasTabindex;
        if (!actionable) continue;
        if (r.width > 0.8 * window.innerWidth && r.height > 0.5 * window.innerHeight) continue;

        const testid = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const ph = el.getAttribute('placeholder') || '';
        const text = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60);
        const sr = el.querySelector('.sr-only, [class*="sr-only"]');
        const srText = sr ? (sr.innerText || sr.textContent || '').trim().slice(0, 60) : '';
        const label = visibleLabel(el);
        if (!aria && !title && !ph && !srText && text.length > 45 && !testid) continue;
        const role = roleAttr || tag;
        const href = tag === 'a' ? (el.href || '') : '';
        const downloadAttr = el.getAttribute('download') || '';
        const key = testid + '|' + role + '|' + label + '|' + href;
        if (seen.has(key)) continue;
        if (!label && !testid) continue;
        seen.add(key);

        out.push({
            testid, aria, title, placeholder: ph, text: srText || text, label, role, href,
            download: downloadAttr,
            has_download_attr: el.hasAttribute('download'),
            xpath: getXPath(el),
            html: snippet(el),
        });
    }
    return out;
}
"""

FIND_TABS_JS = r"""
() => {
    function getXPath(el) {
        if (!el || el.nodeType !== 1) return '';
        const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid');
        if (testId) {
            return "//*[@data-test-id='" + testId + "' or @data-testid='" + testId + "']";
        }
        const aria = el.getAttribute('aria-label');
        if (aria) {
            const safe = aria.replace(/'/g, "\\'");
            return "//" + el.tagName.toLowerCase() + "[@aria-label='" + safe + "']";
        }
        if (el.id) return '//*[@id="' + el.id + '"]';
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1) {
            let idx = 1;
            let sib = node.previousElementSibling;
            while (sib) {
                if (sib.tagName === node.tagName) idx++;
                sib = sib.previousElementSibling;
            }
            parts.unshift(node.tagName.toLowerCase() + '[' + idx + ']');
            node = node.parentElement;
        }
        return '/' + parts.join('/');
    }
    function visibleLabel(el) {
        const aria = el.getAttribute('aria-label') || '';
        const sr = el.querySelector('.sr-only, [class*="sr-only"]');
        const srText = sr ? (sr.innerText || sr.textContent || '').trim() : '';
        const text = (el.innerText || '').trim().replace(/\s+/g, ' ');
        return (aria || srText || text).trim().slice(0, 60);
    }
    const tabs = [];
    const seen = new Set();
    const selectors = [
        '[role=tab]',
        '[data-sidebar-item="true"]',
        '[data-testid^="sidebar-item"]',
        '[data-testid="apps-button"]',
        'nav a[aria-label]',
        'aside a[aria-label]',
        'nav [data-testid] a',
        'nav [data-testid] button',
        '[role=tablist] button',
        '[role=tablist] a',
        '[class*="sidebar" i] a',
        '[class*="sidebar" i] button',
        'nav a[href*="#/"]',
        'aside a[href*="#/"]',
        'nav button',
        'aside button',
        'button[id^="qa-NAV_"]',
    ];
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const label = visibleLabel(el);
            if (!label || label.length < 2 || seen.has(label)) continue;
            seen.add(label);
            const testid = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
            tabs.push({
                label,
                testid,
                id: el.id || '',
                aria: el.getAttribute('aria-label') || '',
                href: el.tagName.toLowerCase() === 'a' ? (el.href || '') : '',
                xpath: getXPath(el),
                role: el.getAttribute('role') || el.tagName.toLowerCase(),
                html: (el.outerHTML || '').slice(0, 500),
            });
        }
    }
    return tabs;
}
"""

FIND_EVERNOTE_NAV_JS = r"""
() => {
    const skip = /TOGGLE|ACTION_MENU|EXPAND_COLLAPSE|PORTRAIT|NOTEBOOKS|TAGS|SPACES/i;
    const out = [];
    const seen = new Set();
    for (const el of document.querySelectorAll('[id^="qa-NAV_"]')) {
        const id = el.id;
        if (skip.test(id)) continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (seen.has(id)) continue;
        seen.add(id);
        const label = (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '')
            || (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60);
        out.push({ id, label });
    }
    return out;
}
"""


FIND_LIST_ITEMS_JS = r"""
() => {
    function getXPath(el) {
        if (!el || el.nodeType !== 1) return '';
        const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid');
        if (testId) {
            return "//*[@data-test-id='" + testId + "' or @data-testid='" + testId + "']";
        }
        const aria = el.getAttribute('aria-label');
        if (aria) {
            const safe = aria.replace(/'/g, "\\'");
            return "//" + el.tagName.toLowerCase() + "[@aria-label='" + safe + "']";
        }
        if (el.id) return '//*[@id="' + el.id.replace(/"/g, '\\"') + '"]';
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1) {
            let idx = 1;
            let sib = node.previousElementSibling;
            while (sib) {
                if (sib.tagName === node.tagName) idx++;
                sib = sib.previousElementSibling;
            }
            parts.unshift(node.tagName.toLowerCase() + '[' + idx + ']');
            node = node.parentElement;
        }
        return '/' + parts.join('/');
    }
    function visibleLabel(el) {
        const aria = el.getAttribute('aria-label') || '';
        const sr = el.querySelector('.sr-only, [class*="sr-only"]');
        const srText = sr ? (sr.innerText || sr.textContent || '').trim() : '';
        const text = (el.innerText || '').trim().replace(/\s+/g, ' ');
        return (aria || srText || text).trim().slice(0, 60);
    }
    const PRIMARY = /^(home|new chat|chats|projects|artifacts|library|search chats|apps|notes|tasks|files|calendar|code|customize|recents|gpts|codex|new note|shortcuts|spaces|open sidebar|close sidebar)$/i;
    const out = [];
    const seen = new Set();
    function consider(el) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        const href = (el.href || '').trim();
        const label = visibleLabel(el);
        if (!label || label.length < 2 || PRIMARY.test(label)) return;
        const inNav = el.closest('nav, aside, [class*="sidebar" i], [data-sidebar-item], [role="navigation"]');
        const chatUrl = /\/chat\/|\/c\/[0-9a-f-]{8,}|\/app\/[0-9a-f-]|conversation/i.test(href);
        const isRow = !!el.closest('[role="listitem"], li');
        if (!chatUrl && !(inNav && isRow)) return;
        const key = (href || label) + '|' + label.slice(0, 40);
        if (seen.has(key)) return;
        seen.add(key);
        out.push({
            testid: el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '',
            aria: el.getAttribute('aria-label') || '',
            text: label,
            label,
            href,
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            xpath: getXPath(el),
            html: (el.outerHTML || '').slice(0, 500),
        });
    }
    const sels = [
        'nav a[href]', 'aside a[href]',
        'a[href*="/chat/"]', 'a[href*="/c/"]',
        '[role="listitem"] a', '[role="listitem"] button',
        'li a[href]', '[data-testid*="conversation"]', '[data-testid*="chat-history"]',
    ];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            consider(el);
            if (out.length >= 15) return out;
        }
    }
    return out;
}
"""


def matches_any(text, patterns):
    t = (text or "").lower()
    return any(re.search(p, t) for p in patterns)


def classify(text):
    for category, pats in CATEGORY_PATTERNS:
        if matches_any(text, pats):
            return category
    return "Other"


def to_casb_activity(category):
    return CATEGORY_TO_CASB.get(category, "other")


def is_gen_ai_domain(host):
    """True when host belongs to a Gen-AI chat application."""
    return root_domain((host or "").lower()) in GEN_AI_ROOT_DOMAINS


def is_download_action_label(text):
    """True when label reads as a download/export control, not incidental chat text."""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    return any(pat.search(t) for pat in DOWNLOAD_ACTION_LABEL_RE)


def is_download_false_positive(el):
    """Sidebar/history rows whose title contains 'download' are not file-download controls."""
    html = (el.get("html") or "").lower()
    href = (el.get("href") or "")
    tid = (el.get("testid") or "").lower()
    aria = (el.get("aria") or "").strip()
    label = (el.get("label") or el.get("text") or "").strip()

    if "data-sidebar-item" in html or "__menu-item" in html:
        return True
    if "history-item" in tid or "conversation-options" in html:
        return True
    if re.search(r"/c/|/chat/", href):
        return True
    if re.match(r"^(pin|open conversation options for)\s", aria or label, re.I):
        return True
    # Long titles are chat thread names, not download buttons.
    if len(label) > 50 and re.search(r"\bdownload\b", label, re.I):
        return True
    return False


def is_genuine_download_control(el):
    """True only for real download/export UI controls."""
    if is_download_false_positive(el):
        return False

    html = (el.get("html") or "")
    tid = (el.get("testid") or "").lower()
    aria = (el.get("aria") or "").strip()
    label = (el.get("label") or el.get("text") or "").strip()
    primary = aria or label

    if el.get("has_download_attr") or el.get("download"):
        return True
    if re.search(r'download\s*=', html, re.I):
        return True
    if any(k in tid for k in ("download", "export-file", "save-as", "file-download")):
        return True

    if not re.search(r"\bdownload\b|\bexport\b|save as", primary, re.I):
        return False

    return is_download_action_label(primary)


async def first_visible_locator(page, selectors):
    """Return (locator, selector) for the first visible match on page or in iframes."""
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


async def wait_for_visible_locator(page, selectors, timeout_s=12):
    """Poll until one of the selectors matches a visible element."""
    deadline = timeout_s * 1000
    waited = 0
    while waited < deadline:
        loc, sel = await first_visible_locator(page, selectors)
        if loc is not None:
            return loc, sel
        await page.wait_for_timeout(500)
        waited += 500
    return None, None


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


def root_domain(host):
    """Best-effort registrable domain (evernote.com from www.evernote.com)."""
    host = (host or "").lower().split(":")[0]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def normalize_url(url):
    """Normalize URL for same-page comparison (ignore trailing slash, no fragment)."""
    p = urlparse(url or "")
    path = (p.path or "/").rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}"


def crawl_url_key(url):
    """Canonical crawl identity including hash fragment (for hash-router SPAs)."""
    p = urlparse(url or "")
    path = (p.path or "/").rstrip("/") or "/"
    base = f"{p.scheme}://{p.netloc.lower()}{path}"
    return f"{base}#{p.fragment}" if p.fragment else base


def hash_route_url(base_url, fragment):
    """Build a full hash-route URL from a base app URL and fragment (/tasks or #/tasks)."""
    root = (base_url or "").split("#")[0]
    frag = (fragment or "").strip()
    if not frag:
        return root
    if not frag.startswith("#"):
        frag = "#" + (frag if frag.startswith("/") else "/" + frag)
    return root + frag


def hash_route_seeds_for_host(host):
    return APP_HASH_ROUTE_SEEDS_BY_ROOT.get(root_domain(host), ())


def evernote_fragment_for_nav_id(nav_id):
    """Map Evernote qa-NAV_* button id to a hash fragment."""
    if nav_id in EVERNOTE_NAV_ID_TO_FRAGMENT:
        return EVERNOTE_NAV_ID_TO_FRAGMENT[nav_id]
    name = (nav_id or "").replace("qa-NAV_", "").lower()
    if name.endswith("_kingdom"):
        name = name[:-8]
    if name == "task":
        return "/tasks"
    if name:
        return "/" + name.replace("_", "-")
    return None


async def page_href(page):
    try:
        return await page.evaluate("() => window.location.href")
    except Exception:
        return page.url or ""


def same_app_host(url_host, allowed_host):
    """True when url_host is allowed_host or a subdomain of the same root."""
    url_host = (url_host or "").lower()
    allowed_host = (allowed_host or "").lower()
    if not url_host or not allowed_host:
        return False
    if url_host == allowed_host:
        return True
    if url_host.endswith("." + allowed_host):
        return True
    return root_domain(url_host) == root_domain(allowed_host)


def path_template(url):
    p = urlparse(url)
    segs = []
    for s in p.path.split("/"):
        if not s:
            continue
        if (re.fullmatch(r"[0-9a-fA-F]{10,}", s) or re.fullmatch(r"\d{5,}", s)
                or re.fullmatch(r"[0-9a-fA-F-]{20,}", s)
                or s.startswith("connector_") or s.startswith("asdk_app_")
                or len(s) >= 24 or (re.search(r"\d", s) and len(s) >= 12)):
            segs.append("*")
        else:
            segs.append(s)
    # Collapse /apps/<vendor>/<connector> -> /apps/*
    if len(segs) >= 2 and segs[0] == "apps":
        segs = ["apps", "*"]
    # Collapse /c/<chatId> -> /c/*
    if len(segs) >= 2 and segs[0] == "c":
        segs = ["c", "*"]
    frag = f"#{p.fragment}" if p.fragment else ""
    return f"{p.netloc}/{'/'.join(segs)}{frag}"


def should_skip_crawl_path(path):
    path = path or "/"
    return any(pat.match(path) for pat in SKIP_CRAWL_PATH_RE)


def build_selector(el):
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
        tag = el.get("role", "*")
        if tag not in ("button", "a"):
            tag = "*"
        return f"{tag}:has-text('{safe}')"
    xpath = el.get("xpath")
    if xpath:
        return f"xpath={xpath}"
    return None


def step_record(step_num, action, description, el, url, note=None):
    """Build one navigation step with xpath/selector/html."""
    rec = {
        "step": step_num,
        "action": action,
        "description": description,
        "xpath": el.get("xpath") or "",
        "selector": build_selector(el),
        "html": (el.get("html") or "")[:HTML_SNIPPET_MAX],
        "url": url,
    }
    if note:
        rec["note"] = note
    return rec


def events_from_steps(steps):
    """Human-readable Events column (like CASB tracker column C)."""
    lines = []
    for s in steps:
        act = s["action"]
        desc = s["description"]
        if act == "detect":
            lines.append(f"Verify '{desc}' is visible")
        elif act == "click":
            lines.append(f"Click '{desc}'")
        elif act == "click_tab":
            lines.append(f"Select tab '{desc}'")
        elif act == "hover":
            lines.append(f"Hover over '{desc}'")
        elif act == "type":
            lines.append(f"Type '{desc}'")
        elif act == "wait":
            lines.append(f"Wait for '{desc}'")
        else:
            lines.append(f"{act}: {desc}")
    return lines


async def get_links(page, base_host, path_prefix="", base_app_url=None):
    try:
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    except Exception:
        hrefs = []
    links = set()
    for h in hrefs:
        try:
            u = urlparse(h)
            if not u.scheme.startswith("http"):
                continue
            if u.netloc.lower() != base_host.lower():
                continue
            if path_prefix and not u.path.startswith(path_prefix):
                continue
            if should_skip_crawl_path(u.path):
                continue
            if u.fragment:
                links.add(crawl_url_key(h))
            else:
                clean = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/") or f"{u.scheme}://{u.netloc}{u.path}"
                links.add(clean)
        except Exception:
            continue
    if base_app_url and root_domain(base_host) in HASH_ROUTER_ROOTS:
        try:
            fragments = await page.evaluate(r"""() => {
                const out = new Set();
                for (const el of document.querySelectorAll('a[href*="#/"], [href*="#/"]')) {
                    const raw = el.getAttribute('href') || '';
                    const idx = raw.indexOf('#/');
                    if (idx >= 0) out.add(raw.slice(idx + 1));
                }
                return [...out];
            }""")
            for frag in fragments or []:
                links.add(hash_route_url(base_app_url, frag))
        except Exception:
            pass
    return links


def fetch_sitemap_urls(start_url, base_host, limit=40):
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
        if not same_app_host(u.netloc, base_host):
            continue
        clean = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)
        if len(urls) >= limit:
            break
    return urls


class CasbNavCrawler:
    def __init__(self, start_url, allowed_host=None):
        self.start_url = start_url
        parsed = urlparse(start_url)
        self.base_host = (allowed_host or parsed.netloc).lower()
        self.home_url = start_url
        # Only follow in-app links under this path prefix (e.g. /client for Evernote).
        rd = root_domain(self.base_host)
        if rd in APP_PATH_PREFIX_BY_ROOT:
            self.app_path_prefix = APP_PATH_PREFIX_BY_ROOT[rd]
        else:
            path_parts = [p for p in parsed.path.split("/") if p]
            if path_parts and path_parts[0].lower() not in SPA_ENTRY_SEGMENTS:
                self.app_path_prefix = "/" + path_parts[0]
            else:
                self.app_path_prefix = ""
        self.visited = set()
        self.template_counts = {}
        self.activities = []
        self.pages = []
        self._activity_keys = set()
        self._gen_ai_download_seeded = False
        self._gen_ai_download_prefix = []
        self._hash_routes_seeded = False
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def is_hash_router(self):
        return root_domain(self.base_host) in HASH_ROUTER_ROOTS

    def is_gen_ai(self):
        return is_gen_ai_domain(self.base_host)

    def gen_ai_chat_url(self):
        path = GEN_AI_CHAT_URLS.get(self.base_host, "/")
        parsed = urlparse(self.start_url)
        return f"{parsed.scheme}://{self.base_host}{path}"

    def should_register_download(self, el, page_url):
        if not is_genuine_download_control(el):
            return False
        if not self.is_gen_ai():
            return True
        path = urlparse(page_url or "").path or "/"
        if GEN_AI_DOWNLOAD_PAGE_RE.search(path):
            return True
        return self._gen_ai_download_seeded

    def same_host(self, url):
        try:
            return same_app_host(urlparse(url).netloc, self.base_host)
        except Exception:
            return False

    def is_external(self, url):
        return same_app_host_is_external(url, self.base_host)

    def crawlable_url(self, url):
        """In-app URLs eligible for BFS (exact host + optional path prefix)."""
        try:
            u = urlparse(url)
            if u.netloc.lower() != self.base_host:
                return False
            if self.app_path_prefix and not u.path.startswith(self.app_path_prefix):
                return False
            if should_skip_crawl_path(u.path):
                return False
            # Root-domain SPAs (ChatGPT, Gemini): skip marketing pages on same host.
            if not self.app_path_prefix and self.base_host in APP_ROUTE_SEEDS_BY_HOST:
                path = (u.path or "/").rstrip("/") or "/"
                if not ROOT_APP_ROUTES_RE.match(path):
                    return False
            return True
        except Exception:
            return False

    def is_destructive(self, el):
        hay = " ".join([
            el.get("aria", ""), el.get("text", ""),
            el.get("title", ""), el.get("testid", ""), el.get("label", ""),
        ])
        return matches_any(hay, DESTRUCTIVE)

    def register_activity(self, el, page_url, prefix_steps, via="page"):
        """Record a discovered activity with full step path."""
        label = el.get("label") or el.get("text") or el.get("testid")
        if label == el.get("testid") and el.get("text"):
            label = el.get("text")
        if not label:
            return
        hay = " ".join([el.get("aria", ""), el.get("text", ""), el.get("title", ""), el.get("testid", "")])
        category = classify(hay)
        casb = to_casb_activity(category)
        if casb == "download_file" and not self.should_register_download(el, page_url):
            return
        destructive = self.is_destructive(el)

        steps = list(prefix_steps)
        if casb == "download_file" and self._gen_ai_download_prefix:
            steps = list(self._gen_ai_download_prefix) + steps
        target_action = "detect" if destructive else "detect"
        steps.append(step_record(
            len(steps) + 1,
            target_action,
            label,
            el,
            page_url,
            note="destructive — detected only, not clicked" if destructive else None,
        ))
        for i, s in enumerate(steps, 1):
            s["step"] = i

        key = (casb, label, page_url, tuple(s["description"] for s in steps))
        if key in self._activity_keys:
            return
        self._activity_keys.add(key)

        entry = {
            "activity": casb,
            "label": label,
            "category": category,
            "page_url": page_url,
            "via": via,
            "destructive": destructive,
            "role": el.get("role"),
            "testid": el.get("testid") or None,
            "href": el.get("href") or None,
            "events": events_from_steps(steps),
            "steps": steps,
            "target_xpath": el.get("xpath") or "",
            "target_selector": build_selector(el),
            "target_html": (el.get("html") or "")[:HTML_SNIPPET_MAX],
        }
        self.activities.append(entry)

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

    async def close_offdomain_pages(self, context, keep_page):
        """Close any tab that navigated outside the app ecosystem."""
        for pg in list(context.pages):
            if pg is keep_page:
                continue
            try:
                url = pg.url or ""
                if url.startswith("http") and self.is_external(url):
                    await pg.close()
            except Exception:
                continue

    async def restore_app_page(self, page, fallback_url):
        """Return to app URL if current page left the app ecosystem."""
        try:
            if self.is_external(page.url):
                print(f"   off-domain -> restoring {fallback_url}")
                page = await safe_goto(page, fallback_url, page.context)
                await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
                return page
            if not same_app_host(urlparse(page.url).netloc, self.base_host):
                print(f"   SSO/subdomain -> restoring {fallback_url}")
                page = await safe_goto(page, fallback_url, page.context)
                await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
                return page
        except Exception as e:
            print(f"   restore failed: {str(e)[:80]}")
        return page

    async def wait_for_app_shell(self, page, fallback_url, timeout_s=20):
        """Wait after navigation/SSO bounce until we're on the app shell."""
        waited = 0
        while waited < timeout_s:
            if not self.is_external(page.url) and same_app_host(urlparse(page.url).netloc, self.base_host):
                return page, True
            await page.wait_for_timeout(2000)
            waited += 2
        if self.is_external(page.url):
            page = await self.restore_app_page(page, fallback_url)
        return page, not self.is_external(page.url)

    async def wait_for_interactive(self, page, min_count=MIN_INTERACTIVE_ELEMENTS,
                                   timeout_s=APP_SHELL_WAIT_SECONDS):
        """Poll until SPA renders interactive controls (Evernote needs ~15s after reload)."""
        waited = 0
        last_count = 0
        while waited < timeout_s:
            elements = await self.enumerate_elements(page)
            last_count = len(elements)
            if last_count >= min_count:
                await page.wait_for_timeout(1500)
                return last_count
            await page.wait_for_timeout(2000)
            waited += 2
            if waited in (10, 20, 30):
                print(f"   waiting for app shell… ({waited}s, {last_count} controls so far)")
        return last_count

    async def safe_click(self, page, selector, fallback_url):
        """Click element; close off-domain popups and restore app page if needed."""
        context = page.context
        pages_before = len(context.pages)
        url_before = page.url
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0 or not await loc.is_visible():
                return False
            await loc.click(timeout=8000)
            await page.wait_for_timeout(900)
        except Exception:
            return False

        await self.close_offdomain_pages(context, page)

        if not self.same_host(page.url):
            await self.restore_app_page(page, fallback_url)
            return True

        if len(context.pages) > pages_before:
            for pg in list(context.pages):
                if pg is not page:
                    try:
                        if self.is_external(pg.url):
                            await pg.close()
                    except Exception:
                        pass

        if page.url != url_before and self.is_external(page.url):
            await self.restore_app_page(page, fallback_url)

        return True

    async def enumerate_elements(self, page):
        try:
            return await page.evaluate(ENUMERATE_JS)
        except Exception:
            return []

    async def discover_list_items(self, page):
        try:
            return await page.evaluate(FIND_LIST_ITEMS_JS)
        except Exception:
            return []

    def is_overflow_trigger(self, el):
        hay = " ".join([el.get("aria", ""), el.get("text", ""), el.get("testid", ""), el.get("label", "")])
        if self.is_destructive(el):
            return False
        if matches_any(hay, ITEM_OVERFLOW_TRIGGER):
            return True
        tid = (el.get("testid") or "").lower()
        return any(k in tid for k in ("overflow", "options", "more", "menu", "organize", "ellipsis", "kebab"))

    def find_overflow_triggers(self, elements, before_labels):
        triggers = []
        for el in elements:
            if not self.is_overflow_trigger(el):
                continue
            label = el.get("label") or ""
            # Accept newly visible controls or explicit overflow labels.
            if label in before_labels and not matches_any(
                " ".join([el.get("aria", ""), el.get("testid", ""), label]),
                ITEM_OVERFLOW_TRIGGER,
            ):
                continue
            selector = build_selector(el)
            if selector:
                triggers.append((el, selector))
            if len(triggers) >= MAX_OVERFLOW_PER_ITEM:
                break
        return triggers

    async def safe_hover(self, page, el_dict):
        selector = build_selector(el_dict)
        if not selector:
            return False
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
            await loc.hover(timeout=5000)
        except Exception:
            try:
                row = loc.locator("xpath=ancestor::*[self::li or @role='listitem'][1]")
                if await row.count() > 0:
                    await row.hover(timeout=5000)
                else:
                    return False
            except Exception:
                return False
        await page.wait_for_timeout(800)
        return True

    async def expand_hover_item_menus(self, page, page_url, prefix_steps=None):
        """Hover chat/note rows to reveal three-dot menus (delete/share/edit/rename)."""
        prefix_steps = prefix_steps or []
        items = await self.discover_list_items(page)
        if not items:
            return []

        nested_results = []
        sample = items[:MAX_HOVER_ITEMS]
        print(f"   hover items: {len(sample)}")
        before_all = {e.get("label") for e in await self.enumerate_elements(page)}

        for item in sample:
            item_label = (item.get("label") or "item")[:50]
            item_step = step_record(len(prefix_steps) + 1, "hover", item_label, item, page_url)
            item_prefix = prefix_steps + [item_step]

            if not await self.safe_hover(page, item):
                continue

            after_hover = await self.enumerate_elements(page)
            triggers = self.find_overflow_triggers(after_hover, before_all)
            if not triggers:
                await page.mouse.move(10, 10)
                continue

            for trigger_el, selector in triggers:
                trigger_label = trigger_el.get("label") or trigger_el.get("testid") or "three dots"
                menu_step = step_record(
                    len(item_prefix) + 1, "click", trigger_label, trigger_el, page_url,
                )
                menu_prefix = item_prefix + [menu_step]

                if not await self.safe_click(page, selector, page_url):
                    continue
                await page.wait_for_timeout(600)

                menu_items = await self.enumerate_elements(page)
                for el in menu_items:
                    label = el.get("label") or ""
                    hay = " ".join([el.get("aria", ""), el.get("text", ""), el.get("testid", "")])
                    category = classify(hay)
                    role = (el.get("role") or "").lower()
                    is_casb = category in CASB_MENU_CATEGORIES
                    is_menu_row = role in ("menuitem", "option")
                    if not is_casb and not is_menu_row:
                        continue
                    if label == trigger_label or label == item_label:
                        continue
                    nested_results.append((el, menu_prefix, f"hover:{item_label}->menu"))

                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)

            await page.mouse.move(10, 10)
            await page.wait_for_timeout(200)

        return nested_results

    async def ensure_sidebar_open(self, page):
        """Click sidebar toggle when the app shell hides primary navigation."""
        for sel in SIDEBAR_OPEN_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click(timeout=5000)
                await page.wait_for_timeout(1200)
                print("   opened sidebar")
                return True
            except Exception:
                continue
        return False

    async def discover_tabs(self, page):
        try:
            tabs = await page.evaluate(FIND_TABS_JS)
            filtered = []
            skip = re.compile(
                r"\b(expand|collapse|more actions|menu|close sidebar|open sidebar|"
                r"close|dismiss|skip to|home)\b", re.I
            )
            for tab in tabs:
                label = tab.get("label") or ""
                href = tab.get("href") or ""
                if "/chat/" in href:
                    continue
                if skip.search(label):
                    continue
                if len(label) > 40:
                    continue
                # Prefer labeled primary nav (Claude sidebar, ChatGPT sidebar).
                if not tab.get("aria") and not tab.get("testid"):
                    nav_id = tab.get("id") or ""
                    if not self.is_hash_router() and "#/" not in href and not nav_id.startswith("qa-NAV_"):
                        continue
                filtered.append(tab)
            return filtered[:MAX_TABS_PER_PAGE]
        except Exception:
            return []

    async def traverse_home_tabs(self, page, page_url):
        """Click every tab on the home page and record activities with tab steps."""
        tabs = await self.discover_tabs(page)
        if not tabs:
            return

        print(f"   home tabs: {len(tabs)}")
        for tab in tabs:
            tab_label = tab.get("label") or "tab"
            tab_el = {
                "testid": tab.get("testid"),
                "aria": tab_label,
                "text": tab_label,
                "xpath": tab.get("xpath"),
                "html": tab.get("html"),
            }
            selector = build_selector(tab_el)
            if not selector:
                continue

            tab_step = step_record(1, "click_tab", tab_label, tab_el, page_url)
            prefix = [tab_step]

            clicked = await self.safe_click(page, selector, page_url)
            if not clicked:
                continue
            await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)

            if not self.same_host(page.url):
                await self.restore_app_page(page, page_url)
                continue

            elements = await self.enumerate_elements(page)
            for el in elements:
                if el.get("role") == "tab":
                    continue
                self.register_activity(el, page_url, prefix, via=f"tab:{tab_label}")

            nested = await self.expand_menus(page, page_url, elements, prefix_steps=prefix)
            for el, extra_prefix, via in nested:
                self.register_activity(el, page_url, extra_prefix, via=via)

            hover_nested = await self.expand_hover_item_menus(page, page_url, prefix_steps=prefix)
            for el, extra_prefix, via in hover_nested:
                self.register_activity(el, page_url, extra_prefix, via=via)

            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            # Only reload if navigation actually left the app view.
            if normalize_url(page.url) != normalize_url(page_url) or self.is_external(page.url):
                await self.restore_app_page(page, page_url)
                await self.wait_for_interactive(page, min_count=MIN_INTERACTIVE_ELEMENTS, timeout_s=20)

    async def discover_evernote_nav_hash_routes(self, page, base_url):
        """Read qa-NAV_* sidebar buttons and map to hash-route URLs."""
        routes = set()
        try:
            nav_items = await page.evaluate(FIND_EVERNOTE_NAV_JS)
        except Exception:
            nav_items = []
        for item in nav_items or []:
            frag = evernote_fragment_for_nav_id(item.get("id") or "")
            if frag:
                routes.add(crawl_url_key(hash_route_url(base_url, frag)))
        if routes:
            print(f"   hash routes: {len(routes)} from qa-NAV_* sidebar ids")
        return routes

    async def discover_sidebar_hash_routes(self, page, base_url):
        """Click sidebar nav items on hash-router SPAs; return discovered route URLs."""
        await self.ensure_sidebar_open(page)
        routes = await self.discover_evernote_nav_hash_routes(page, base_url)

        if crawl_url_key(await page_href(page)) != crawl_url_key(base_url):
            page = await safe_goto(page, base_url, page.context)
            await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)

        tabs = await self.discover_tabs(page)
        if not tabs:
            return routes

        print(f"   hash routes: probing {len(tabs)} sidebar items")
        home_key = crawl_url_key(base_url)
        nav_skip = re.compile(
            r"\b(sign out|log out|logout|help|support|upgrade|account|expand|collapse)\b", re.I
        )

        for tab in tabs:
            label = tab.get("label") or "nav"
            if nav_skip.search(label):
                continue
            tab_el = {
                "testid": tab.get("testid"),
                "aria": tab.get("aria") or label,
                "text": label,
                "xpath": tab.get("xpath"),
                "html": tab.get("html"),
                "nav_id": tab.get("id"),
            }
            selector = None
            if tab.get("id"):
                selector = f"#{tab['id']}"
            if not selector:
                selector = build_selector(tab_el)
            if not selector:
                continue

            url_before = await page_href(page)
            if not await self.safe_click(page, selector, base_url):
                continue
            try:
                await page.wait_for_function(
                    "(before) => window.location.href !== before",
                    url_before,
                    timeout=8000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
            await self.wait_for_interactive(page, min_count=2, timeout_s=20)
            current = crawl_url_key(await page_href(page))
            if current != crawl_url_key(url_before) and self.crawlable_url(current):
                routes.add(current)

            if current != home_key:
                page = await safe_goto(page, base_url, page.context)
                await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)

        print(f"   hash routes: discovered {len(routes)} total")
        return routes

    async def expand_menus(self, page, page_url, raw_elements, prefix_steps=None):
        """Open safe menus; return list of (element, steps_prefix, via)."""
        prefix_steps = prefix_steps or []
        nested_results = []
        triggers = []
        for el in raw_elements:
            hay = " ".join([el.get("aria", ""), el.get("text", ""), el.get("testid", "")])
            if matches_any(hay, MENU_TRIGGER) and not self.is_destructive(el):
                selector = build_selector(el)
                if selector:
                    triggers.append((el, selector))
            if len(triggers) >= MAX_MENUS_PER_PAGE:
                break

        before_labels = {e.get("label") for e in raw_elements}

        for trigger_el, selector in triggers:
            trigger_label = trigger_el.get("label") or trigger_el.get("testid") or "menu"
            menu_step = step_record(
                len(prefix_steps) + 1,
                "click",
                trigger_label,
                trigger_el,
                page_url,
            )
            menu_prefix = prefix_steps + [menu_step]

            if not await self.safe_click(page, selector, page_url):
                continue

            after = await self.enumerate_elements(page)
            for el in after:
                if el.get("label") in before_labels:
                    continue
                nested_results.append((el, menu_prefix, f"menu:{trigger_label}"))

            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            await self.restore_app_page(page, page_url)

        return nested_results

    async def wait_for_gen_ai_response(self, page):
        """Wait until the model finishes generating a chat response."""
        await page.wait_for_timeout(2000)
        waited = 0
        while waited < GEN_AI_RESPONSE_WAIT_SECONDS:
            loc, _ = await first_visible_locator(page, GENERATING_SELECTORS)
            if loc is None:
                await page.wait_for_timeout(2000)
                return True
            await page.wait_for_timeout(3000)
            waited += 3
        return False

    async def seed_gen_ai_file_download(self, page, page_url):
        """Prompt chat to generate a downloadable file; record steps for download_file paths."""
        if not self.is_gen_ai() or self._gen_ai_download_seeded:
            return

        chat_url = self.gen_ai_chat_url()
        steps = []
        step_num = 1

        print("   gen-ai: seeding downloadable file via chat prompt")
        try:
            if normalize_url(page.url) != normalize_url(chat_url):
                page = await safe_goto(page, chat_url, page.context)
                await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
                await self.wait_for_interactive(page, min_count=2, timeout_s=25)

            input_loc, input_sel = await wait_for_visible_locator(
                page, CHAT_INPUT_SELECTORS, timeout_s=20,
            )
            if input_loc is None:
                print("   gen-ai download seed: chat input not found")
                return page

            await input_loc.click()
            await page.keyboard.type(DOWNLOAD_FILE_PROMPT)
            await page.wait_for_timeout(500)

            input_el = {
                "label": "chat prompt input",
                "aria": "chat prompt input",
                "text": DOWNLOAD_FILE_PROMPT[:60],
                "xpath": "",
                "html": f"<textarea>{DOWNLOAD_FILE_PROMPT[:80]}</textarea>",
            }
            steps.append(step_record(
                step_num, "type", "Type prompt to generate downloadable file",
                input_el, page.url, note=f"selector: {input_sel}",
            ))
            step_num += 1

            send_loc, send_sel = await wait_for_visible_locator(
                page, SEND_BUTTON_SELECTORS, timeout_s=10,
            )
            if send_loc is None:
                print("   gen-ai download seed: send button not found")
                return page

            await send_loc.click()
            await page.wait_for_timeout(800)

            send_el = {
                "label": "Send",
                "aria": "Send",
                "text": "Send",
                "xpath": "",
                "html": "<button>Send</button>",
            }
            steps.append(step_record(
                step_num, "click", "Send prompt", send_el, page.url, note=f"selector: {send_sel}",
            ))
            step_num += 1

            responded = await self.wait_for_gen_ai_response(page)
            wait_el = {
                "label": "AI response with downloadable file",
                "aria": "AI response with downloadable file",
                "text": "AI response",
                "xpath": "",
                "html": "<div>assistant response</div>",
            }
            steps.append(step_record(
                step_num, "wait", "Wait for AI response with downloadable file",
                wait_el, page.url,
                note="completed" if responded else "timed out — scanning for download controls anyway",
            ))

            self._gen_ai_download_prefix = steps
            self._gen_ai_download_seeded = True
            print("   gen-ai download seed: prompt sent, scanning for download controls")
        except Exception as e:
            print(f"   gen-ai download seed failed: {str(e)[:80]}")
        return page

    async def discover_download_controls(self, page, page_url, via="gen-ai:chat-response"):
        """Register download_file controls visible on the current page."""
        for el in await self.enumerate_elements(page):
            hay = " ".join([el.get("aria", ""), el.get("text", ""), el.get("testid", "")])
            if to_casb_activity(classify(hay)) != "download_file":
                continue
            if not self.should_register_download(el, page_url):
                continue
            self.register_activity(el, page_url, [], via=via)

    async def discover_page_actions(self, page, page_url, is_home=False):
        """Enumerate all actions on a page (after optional home-tab pass)."""
        if is_home:
            await self.ensure_sidebar_open(page)

        if is_home and self.is_gen_ai():
            page = await self.seed_gen_ai_file_download(page, page_url)
            await self.discover_download_controls(page, page_url)

        if is_home and not self.is_hash_router():
            await self.traverse_home_tabs(page, page_url)

        elements = await self.enumerate_elements(page)
        for el in elements:
            self.register_activity(el, page_url, [], via="page")

        nested = await self.expand_menus(page, page_url, elements, prefix_steps=[])
        for el, prefix, via in nested:
            self.register_activity(el, page_url, prefix, via=via)

        hover_nested = await self.expand_hover_item_menus(page, page_url, prefix_steps=[])
        for el, prefix, via in hover_nested:
            self.register_activity(el, page_url, prefix, via=via)

    async def visit(self, page, url, is_home=False):
        url_key = crawl_url_key(url)
        if url_key in self.visited or len(self.visited) >= MAX_PAGES:
            return set(), page
        tmpl = path_template(url)
        if self.template_counts.get(tmpl, 0) >= MAX_PER_TEMPLATE:
            return set(), page

        self.visited.add(url_key)
        self.template_counts[tmpl] = self.template_counts.get(tmpl, 0) + 1
        tag = " [HOME]" if is_home else ""
        print(f"\n[page {len(self.visited)}/{MAX_PAGES}]{tag} {url}")

        try:
            if crawl_url_key(await page_href(page)) == url_key:
                print("   reusing open tab (no reload)")
            else:
                page = await safe_goto(page, url, page.context)
                await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
        except Exception as e:
            print(f"   could not load: {str(e)[:80]}")
            return set(), page

        # SSO may bounce through accounts.* subdomain — wait for app shell.
        page, shell_ok = await self.wait_for_app_shell(page, url)
        if not shell_ok:
            if self.is_external(page.url):
                print(f"   skipped: redirected outside app to {urlparse(page.url).netloc}")
            else:
                print(f"   skipped: not logged in or app shell did not load ({page.url})")
            self.visited.discard(url_key)
            return set(), page

        control_count = await self.wait_for_interactive(page)
        if control_count < MIN_INTERACTIVE_ELEMENTS:
            title = await self.safe_title(page)
            print(f"   skipped: no interactive controls found ({control_count}, title={title!r})")
            print("   Tip: finish logging in, wait for the app home screen, then re-run.")
            self.visited.discard(url_key)
            return set(), page

        try:
            title = await self.safe_title(page)
            frag = urlparse(url).fragment
            shot_name = (frag.replace("/", "_").strip("_") if frag else urlparse(url).path) or "home"
            await self.shot(page, shot_name)
            await self.discover_page_actions(page, url, is_home=is_home)

            by_casb = {}
            page_acts = [a for a in self.activities if a["page_url"] == url]
            for a in page_acts:
                by_casb.setdefault(a["activity"], 0)
                by_casb[a["activity"]] += 1
            summary = ", ".join(f"{k}:{v}" for k, v in sorted(by_casb.items()))
            print(f"   {len(page_acts)} activities  ({summary})")

            self.pages.append({"url": url, "title": title, "is_home": is_home,
                               "activity_count": len(page_acts)})
        except Exception as e:
            print(f"   error while reading page (skipped): {str(e)[:80]}")
            return set(), page

        links = await get_links(page, self.base_host, self.app_path_prefix,
                                base_app_url=self.start_url.split("#")[0])
        if is_home and self.is_hash_router() and not self._hash_routes_seeded:
            self._hash_routes_seeded = True
            links |= await self.discover_sidebar_hash_routes(page, self.start_url)
        return links, page

    async def run(self, page):
        parsed = urlparse(self.start_url)
        queue = [crawl_url_key(self.start_url)]
        # Seed known app sections for root-domain SPAs (ChatGPT, Gemini, etc.)
        if not self.app_path_prefix:
            for route in APP_ROUTE_SEEDS_BY_HOST.get(self.base_host, ()):
                seed = f"{parsed.scheme}://{self.base_host}{route}"
                if self.crawlable_url(seed) and seed not in queue:
                    queue.append(seed)
        if self.is_hash_router():
            for frag in hash_route_seeds_for_host(self.base_host):
                seed = hash_route_url(self.start_url, frag)
                if self.crawlable_url(seed) and seed not in queue:
                    queue.append(crawl_url_key(seed))
        elif not self.app_path_prefix:
            for u in fetch_sitemap_urls(self.start_url, self.base_host):
                if self.crawlable_url(u) and u not in queue:
                    queue.append(u)

        home_normalized = crawl_url_key(self.start_url)

        while queue and len(self.visited) < MAX_PAGES:
            url = queue.pop(0)
            if not self.crawlable_url(url):
                continue
            is_home = crawl_url_key(url) == home_normalized and not urlparse(url).fragment
            new_links, page = await self.visit(page, url, is_home=is_home)
            for link in new_links:
                link_key = crawl_url_key(link)
                if self.crawlable_url(link) and link_key not in self.visited and link_key not in queue:
                    queue.append(link_key)


def write_reports(start_url, crawler):
    host = host_slug(start_url)
    by_activity = {}
    for a in crawler.activities:
        by_activity.setdefault(a["activity"], []).append(a)

    report = {
        "generated_at": datetime.now().isoformat(),
        "application": f"{root_domain(crawler.base_host).split('.')[0]}(windows_browser)",
        "start_url": start_url,
        "app_domain": crawler.base_host,
        "pages_crawled": len(crawler.pages),
        "total_activities": len(crawler.activities),
        "casb_activity_summary": {k: len(v) for k, v in sorted(by_activity.items())},
        "pages": crawler.pages,
        "activities": crawler.activities,
    }

    json_path = f"{host}_casb_navigations.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    lines = [
        f"# CASB Navigation Map: {start_url}\n",
        f"_Generated: {report['generated_at']}_\n",
        f"**Pages: {report['pages_crawled']} | Activities: {report['total_activities']}**\n",
        f"App domain: `{crawler.base_host}`\n",
        "\n> Destructive actions are detected only (xpath recorded, never clicked).\n",
        "\n---\n",
    ]

    for casb_name in sorted(by_activity.keys()):
        items = by_activity[casb_name]
        lines.append(f"\n## Activity: `{casb_name}` ({len(items)})\n")
        for a in items[:50]:
            lines.append(f"### {a['label']}")
            lines.append(f"- **Page:** `{a['page_url']}`")
            lines.append(f"- **Via:** {a['via']}")
            if a["destructive"]:
                lines.append("- **Destructive:** yes (detect only)")
            lines.append(f"- **Target XPath:** `{a['target_xpath']}`")
            lines.append(f"- **Target selector:** `{a['target_selector']}`")
            lines.append("\n**Events (test steps):**")
            for i, ev in enumerate(a["events"], 1):
                lines.append(f"{i}. {ev}")
            lines.append("\n**Navigation steps:**")
            for s in a["steps"]:
                lines.append(
                    f"- Step {s['step']} [{s['action']}] {s['description']}\n"
                    f"  - xpath: `{s['xpath']}`\n"
                    f"  - selector: `{s.get('selector')}`"
                )
            lines.append("")
        if len(items) > 50:
            lines.append(f"_… and {len(items) - 50} more_\n")

    md_path = f"{host}_CASB_NAVIGATIONS.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return host, json_path, md_path


def get_url_arg(default=None):
    for a in sys.argv[1:]:
        if a.startswith("http://") or a.startswith("https://"):
            return a
    return default


def get_flag_value(name):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def detect_signed_out(activities):
    """Heuristic: prominent login control and no upload/create actions."""
    labels = " ".join(a["label"].lower() for a in activities)
    has_login = any(a["activity"] == "login" for a in activities)
    has_app_actions = any(a["activity"] in ("upload_file", "create", "post", "edit") for a in activities)
    return has_login and not has_app_actions and ("log in" in labels or "sign in" in labels)


def serve_mode(url):
    print("SERVE MODE - starting Chrome for manual login")
    print("=" * 60)
    chrome = find_chrome()
    if not chrome:
        print("ERROR: Chrome not found. Install Google Chrome and retry.")
        return
    profile = os.path.join(os.getcwd(), f"{host_slug(url)}_cdp_profile")
    os.makedirs(profile, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    creationflags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    subprocess.Popen(args, creationflags=creationflags) if creationflags else subprocess.Popen(args)
    print(f"Chrome started on debug port {CDP_PORT} with profile {profile}")
    print("\nNEXT STEPS:")
    print("  1. Sign in within the Chrome window that opened.")
    print("  2. Leave that Chrome window OPEN.")
    print(f"  3. Run:  python {os.path.basename(__file__)} {url}")


async def is_page_alive(page):
    """True when the CDP page handle is still usable."""
    try:
        if page.is_closed():
            return False
        _ = page.url
        return True
    except Exception:
        return False


async def safe_goto(page, url, context=None):
    """Navigate to url; on detached frame, open a fresh tab."""
    ctx = context or page.context
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        return page
    except Exception as e:
        msg = str(e).lower()
        if "detached" not in msg and "closed" not in msg:
            raise
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
        fresh = await ctx.new_page()
        await fresh.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        return fresh


def die_no_chrome(start_url):
    script = os.path.basename(__file__)
    print("\nChrome is not available on CDP (window closed or wrong profile).")
    print(f"Restart it:  python {script} --serve {start_url}")
    print("Sign in, leave the window open, then re-run the crawler.")
    raise SystemExit(1)


async def open_app_page(browser, start_url, base_host):
    """Reuse an existing logged-in app tab; prefer the start URL."""
    try:
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
    except Exception:
        die_no_chrome(start_url)
    start_norm = normalize_url(start_url)

    def on_app(url):
        try:
            host = urlparse(url).netloc
            return url.startswith("http") and same_app_host(host, base_host) and not same_app_host_is_external(url, base_host)
        except Exception:
            return False

    candidates = []
    for pg in context.pages:
        if await is_page_alive(pg):
            candidates.append(pg)

    for pg in candidates:
        try:
            if on_app(pg.url) and normalize_url(pg.url) == start_norm:
                return pg
        except Exception:
            continue

    for pg in candidates:
        try:
            if on_app(pg.url):
                return await safe_goto(pg, start_url, context)
        except Exception as e:
            if "closed" in str(e).lower():
                die_no_chrome(start_url)
            continue

    for pg in candidates:
        try:
            if await is_page_alive(pg):
                return await safe_goto(pg, start_url, context)
        except Exception as e:
            if "closed" in str(e).lower():
                die_no_chrome(start_url)
            continue

    try:
        page = await context.new_page()
        return await safe_goto(page, start_url, context)
    except Exception as e:
        if "closed" in str(e).lower() or "detached" in str(e).lower():
            die_no_chrome(start_url)
        raise


def same_app_host_is_external(url, base_host):
    """True when url is outside the app ecosystem."""
    try:
        host = urlparse(url).netloc.lower()
        if not host:
            return False
        return not same_app_host(host, base_host)
    except Exception:
        return True


async def crawl_mode(url, allowed_host=None, path_prefix=None):
    allowed_host = (allowed_host or urlparse(url).netloc).lower()
    print("CASB Navigation + XPath Crawler")
    print("=" * 50)
    print(f"Target: {url}")
    print(f"Locked to host: {allowed_host}")
    if path_prefix is not None:
        print(f"Path prefix: {path_prefix or '(none)'}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        except Exception:
            print(f"Could not connect to Chrome on port {CDP_PORT}.")
            print(f"Start it first:  python {os.path.basename(__file__)} --serve {url}")
            return

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await open_app_page(browser, url, allowed_host)

        crawler = CasbNavCrawler(url, allowed_host=allowed_host)
        if path_prefix is not None:
            crawler.app_path_prefix = path_prefix

        async def on_new_page(new_page):
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=8000)
                if new_page.url and crawler.is_external(new_page.url):
                    await new_page.close()
            except Exception:
                pass

        context.on("page", lambda pg: asyncio.create_task(on_new_page(pg)))

        try:
            await crawler.run(page)
            host, json_path, md_path = write_reports(url, crawler)
            print("\n" + "=" * 50)
            print("CRAWL COMPLETE")
            print("=" * 50)
            print(f"Pages crawled : {len(crawler.pages)}")
            print(f"Activities    : {len(crawler.activities)}")
            summary = {}
            for a in crawler.activities:
                summary[a["activity"]] = summary.get(a["activity"], 0) + 1
            print(f"By CASB type  : {json.dumps(dict(sorted(summary.items())), indent=2)}")
            print(f"Report (JSON) : {json_path}")
            print(f"Map (Markdown): {md_path}")
            print(f"Screenshots   : {SCREENSHOT_DIR}/")
            if detect_signed_out(crawler.activities):
                print("\nWARNING: Session looks signed out (login controls found, no app actions).")
                print(f"Run:  python {os.path.basename(__file__)} --serve {url}")
                print("Sign in, leave Chrome open, then re-run the crawler.")
        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            raise
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def main():
    url = get_url_arg()
    allowed_host = get_flag_value("--host")
    path_prefix = get_flag_value("--path-prefix")
    if "--serve" in sys.argv:
        if not url:
            print(f"Usage: python {os.path.basename(__file__)} --serve <app_url>")
            return
        serve_mode(url)
    else:
        if not url:
            print(f"Usage: python {os.path.basename(__file__)} <app_url> [--host <hostname>] [--path-prefix /client]")
            print(f"First time:  python {os.path.basename(__file__)} --serve <app_url>")
            return
        asyncio.run(crawl_mode(url, allowed_host=allowed_host, path_prefix=path_prefix))


if __name__ == "__main__":
    main()
