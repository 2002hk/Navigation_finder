"""
CASB AI Activity Classifier — Phase 2 (Local LLM)
==================================================

Post-processes the JSON output from casb_nav_xpath_crawler.py and re-classifies
every activity using a local LLM via Ollama (llama3.1, mistral, phi3, etc.).

Why this exists
---------------
The crawler uses regex patterns to classify elements (e.g. anything containing
"delete" → delete activity). This works well for clear labels but fails on:
  - Ambiguous testids  (e.g. "btn-confirm-modal", "action-primary")
  - Icon-only buttons  (no visible label, just aria or testid)
  - App-specific names  (e.g. "Archive" on Gmail = delete-equivalent)
  - Multi-action menus  (a single "..." button that opens delete + share + rename)
  - Low-confidence "other" / "navigation" classifications that might actually be
    a real CASB activity

The AI classifier sends each low/medium confidence element to a local LLM with its
full HTML snippet, label, aria-label, testid, and surrounding step context, and
gets back a structured JSON decision. No API costs, works offline!

Prerequisites
-------------
    # Install and start Ollama:
    # Download from https://ollama.ai/
    ollama serve
    
    # The script will auto-download the best model (llama3.1:8b by default)

Usage
-----
    # Classify a single crawler output file:
    python casb_ai_classifier.py deepseek.com_casb_navigations.json

    # Classify + write enhanced output:
    python casb_ai_classifier.py deepseek.com_casb_navigations.json --out deepseek_classified.json

    # Force-reclassify EVERY activity (not just low-confidence):
    python casb_ai_classifier.py deepseek.com_casb_navigations.json --all

    # Dry run — print decisions without calling the LLM:
    python casb_ai_classifier.py deepseek.com_casb_navigations.json --dry-run

    # Set confidence threshold (0.0–1.0, default 0.75):
    python casb_ai_classifier.py deepseek.com_casb_navigations.json --threshold 0.6

Output
------
  <host>_classified.json       — enhanced JSON with ai_activity, ai_confidence,
                                  ai_reasoning fields added to every activity
  <host>_classified_REPORT.md  — human-readable summary for review dashboard

The enhanced JSON is a drop-in replacement for the crawler's raw JSON —
casb_automation's code generator (Phase 4) reads from it directly.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("ERROR: requests package not found.")
    print("Install it:  pip install requests")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────────

# Ollama configuration
OLLAMA_BASE_URL = "http://localhost:11434"
RECOMMENDED_MODELS = [
    "llama3.1:8b",     # Best balance - very good at classification
    "mistral:7b",      # Faster, still quite good  
    "phi3:medium",     # Smallest that works well
    "gemma2:9b",       # Google's model, good accuracy
]
DEFAULT_MODEL = "llama3.1:8b"
MAX_TOKENS = 512
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_S = 2.0

# Global model selection (will be set during startup)
SELECTED_MODEL = None

# Activities the CASB system cares about. "other" and "navigation" are
# low-value — we want to catch things that were mis-filed there.
CASB_ACTIVITY_TYPES = [
    "create",
    "edit",
    "delete",
    "logout",
    "share",
    "upload_file",
    "download_file",
    "post",
    "login",
    "rename",
    "search",
    "settings",
    "navigation",
    "other",
]

# These activity types from the crawler already have high regex confidence.
# Skip AI reclassification unless --all is passed.
HIGH_CONFIDENCE_ACTIVITIES = {
    "login",
    "logout",
    "upload_file",
    "download_file",
    "delete",
    "share",
    "post",
}

# Regex patterns that indicate high confidence (borrowed from crawler).
# If a label strongly matches, we trust the existing classification.
HIGH_CONFIDENCE_PATTERNS = [
    (re.compile(r"\bsign\s*out\b|\blog\s*out\b", re.I), "logout"),
    (re.compile(r"\bsign\s*in\b|\blog\s*in\b", re.I), "login"),
    (re.compile(r"\bupload\b|\battach\b|\bimport\b", re.I), "upload_file"),
    (re.compile(r"\bdownload\b|\bexport\b", re.I), "download_file"),
    (re.compile(r"\bdelete\b|\bremove\b|\btrash\b", re.I), "delete"),
    (re.compile(r"\brename\b", re.I), "rename"),
    (re.compile(r"\bshare\b|\binvite\b|\bpublish\b", re.I), "share"),
    (re.compile(r"\bedit\b|\bmodify\b", re.I), "edit"),
    (re.compile(r"\bcreate\b|\bnew\b|\bcompose\b", re.I), "create"),
    (re.compile(r"\bsend\b|\bpost\b|\bsubmit\b|\breply\b", re.I), "post"),
]

HTML_SNIPPET_MAX = 400  # chars to send to AI (keep prompt small)


# ── Confidence scoring (heuristic before AI call) ────────────────────────────

def regex_confidence(activity: str, entry: dict) -> float:
    """
    Returns a 0.0–1.0 heuristic confidence for the crawler's existing
    classification, based on how strongly the label/aria matches known patterns.
    0.0 = no matching pattern found (definitely send to AI)
    1.0 = perfect strong-pattern match (skip AI unless --all)
    """
    label = (entry.get("label") or "").strip()
    aria = (entry.get("steps", [{}])[-1].get("description") if entry.get("steps") else "") or ""
    testid = (entry.get("testid") or "").strip()
    hay = f"{label} {aria} {testid}".lower()

    if not hay.strip():
        return 0.0

    for pat, mapped_activity in HIGH_CONFIDENCE_PATTERNS:
        if pat.search(hay):
            # Pattern matches and crawler's activity agrees → high confidence
            if activity == mapped_activity:
                return 0.95
            # Pattern matches but crawler filed it differently → low confidence
            return 0.3

    # Vague activities with no strong signal
    if activity in ("other", "navigation", "settings"):
        return 0.4

    # Has a label but doesn't match any strong pattern
    if label and len(label) > 2:
        return 0.55

    # Icon-only (no useful label)
    return 0.2


# ── Prompt builder ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a CASB (Cloud Access Security Broker) activity classifier.
Your job is to classify web UI elements into the activity type they trigger when a user interacts with them.

Activity types and their meanings:
- create: Creates a new object (note, file, project, chat, document)
- edit: Modifies an existing object's content
- rename: Changes the name/title of an existing object
- delete: Permanently removes an object (or moves to trash)
- logout: Signs the user out of the application
- share: Shares an object with others, or generates a share link
- upload_file: Uploads a local file to the application
- download_file: Downloads a file from the application to local storage
- post: Sends a message, prompt, comment, or form submission
- login: Signs the user into the application
- search: Searches or filters content
- settings: Opens settings, preferences, or profile configuration
- navigation: Navigates to a different section/page (no data action)
- other: Does not fit any of the above categories

You MUST respond with ONLY a valid JSON object — no preamble, no explanation, no markdown fences.
Format:
{
  "activity": "<one of the 14 activity types above>",
  "confidence": <float 0.0 to 1.0>,
  "reasoning": "<one sentence explaining your decision>"
}"""


def build_user_prompt(entry: dict) -> str:
    label = (entry.get("label") or "").strip()
    category = (entry.get("category") or "").strip()
    current_activity = (entry.get("activity") or "").strip()
    role = (entry.get("role") or "").strip()
    testid = (entry.get("testid") or "").strip()
    href = (entry.get("href") or "").strip()
    html = (entry.get("target_html") or "")[:HTML_SNIPPET_MAX]
    page_url = (entry.get("page_url") or "").strip()

    # Build step path context (what the user had to do to reach this element)
    steps = entry.get("steps", [])
    step_path = ""
    if steps:
        path_parts = []
        for s in steps[:-1]:  # exclude the target element itself
            action = s.get("action", "")
            desc = s.get("description", "")
            if action and desc:
                path_parts.append(f"{action}: {desc}")
        if path_parts:
            step_path = " → ".join(path_parts)

    parts = [f"Classify this web UI element:"]
    parts.append(f"  label:            {label!r}")
    if testid:
        parts.append(f"  data-testid:      {testid!r}")
    if role:
        parts.append(f"  role/tag:         {role!r}")
    if href:
        parts.append(f"  href:             {href!r}")
    if step_path:
        parts.append(f"  navigation path:  {step_path}")
    if page_url:
        # Show just the path, not full URL (reduce noise)
        try:
            parsed = urlparse(page_url)
            parts.append(f"  page:             {parsed.path or '/'}")
        except Exception:
            pass
    if html:
        parts.append(f"  HTML snippet:     {html!r}")
    parts.append(f"\nCurrent classification: activity={current_activity!r}, category={category!r}")
    parts.append("Is this classification correct? If not, provide the better one.")

    return "\n".join(parts)


# ── Ollama Setup and Health Check ─────────────────────────────────────────────

def check_ollama_health() -> bool:
    """Check if Ollama is running and accessible."""
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def list_available_models() -> list[str]:
    """Get list of models currently available in Ollama."""
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        if response.status_code == 200:
            data = response.json()
            return [model["name"] for model in data.get("models", [])]
        return []
    except Exception:
        return []


def pull_model(model_name: str) -> bool:
    """Download/pull a model if not already available."""
    print(f"  Downloading model '{model_name}'... (this may take a few minutes)")
    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/pull",
            json={"name": model_name},
            timeout=600,  # 10 minute timeout for downloads
        )
        return response.status_code == 200
    except Exception as e:
        print(f"  ERROR downloading model: {e}")
        return False


def select_best_model() -> str:
    """Select the best available model, downloading if needed."""
    available = list_available_models()
    
    # Check if any recommended model is already available
    for model in RECOMMENDED_MODELS:
        if model in available:
            print(f"  Using available model: {model}")
            return model
    
    # Try to download the default model
    print(f"  No recommended models found locally.")
    if pull_model(DEFAULT_MODEL):
        print(f"  Successfully downloaded: {DEFAULT_MODEL}")
        return DEFAULT_MODEL
    
    # Fallback to any available model
    if available:
        model = available[0]
        print(f"  Using fallback model: {model}")
        return model
    
    raise RuntimeError("No models available and unable to download default model")


def setup_ollama() -> str:
    """Initialize Ollama and return the selected model name."""
    global SELECTED_MODEL
    
    print("Setting up Ollama local LLM...")
    
    if not check_ollama_health():
        print("ERROR: Ollama is not running.")
        print("Start it with:  ollama serve")
        print("Or install from: https://ollama.ai/")
        sys.exit(1)
    
    print("  ✓ Ollama is running")
    
    SELECTED_MODEL = select_best_model()
    print(f"  ✓ Selected model: {SELECTED_MODEL}")
    
    return SELECTED_MODEL


# ── Ollama API call ───────────────────────────────────────────────────────────

def call_ollama(model: str, entry: dict) -> dict:
    """
    Send one entry to Ollama for classification.
    Returns dict with keys: activity, confidence, reasoning, api_error (optional).
    """
    user_prompt = build_user_prompt(entry)
    
    # Construct full prompt (Ollama doesn't separate system/user like Anthropic)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            payload = {
                "model": model,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,  # Low temperature for consistent classification
                    "top_p": 0.9,
                    "num_predict": MAX_TOKENS,
                }
            }
            
            response = requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json=payload,
                timeout=30,
            )
            
            if response.status_code != 200:
                raise Exception(f"Ollama API returned status {response.status_code}")
            
            data = response.json()
            raw = data.get("response", "").strip()

            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

            result = json.loads(raw)

            # Validate response shape
            if "activity" not in result:
                raise ValueError("Missing 'activity' key")
            if result["activity"] not in CASB_ACTIVITY_TYPES:
                raise ValueError(f"Unknown activity: {result['activity']!r}")
            result.setdefault("confidence", 0.8)
            result.setdefault("reasoning", "")
            return result

        except json.JSONDecodeError as e:
            if attempt < API_RETRY_ATTEMPTS - 1:
                time.sleep(API_RETRY_DELAY_S)
                continue
            return {
                "activity": entry.get("activity", "other"),
                "confidence": 0.0,
                "reasoning": "",
                "api_error": f"JSON parse failed: {e}",
            }
        except Exception as e:
            if attempt < API_RETRY_ATTEMPTS - 1:
                time.sleep(API_RETRY_DELAY_S)
                continue
            return {
                "activity": entry.get("activity", "other"),
                "confidence": 0.0,
                "reasoning": "",
                "api_error": str(e),
            }


# ── Batch classifier ──────────────────────────────────────────────────────────

def classify_activities(
    activities: list[dict],
    model: str,
    threshold: float = 0.75,
    force_all: bool = False,
    dry_run: bool = False,
    verbose: bool = True,
) -> list[dict]:
    """
    For each activity entry, decide whether to reclassify via AI,
    then annotate with ai_activity, ai_confidence, ai_reasoning.

    Fields added to each entry:
      ai_activity    — Claude's classification (may match original)
      ai_confidence  — 0.0–1.0
      ai_reasoning   — one-sentence explanation
      ai_reclassified — True if ai_activity differs from original activity
      ai_skipped      — True if entry was skipped (high confidence + not --all)
      original_activity — always set to the crawler's original value
    """
    enhanced = []
    stats = {
        "total": len(activities),
        "sent_to_ai": 0,
        "reclassified": 0,
        "skipped": 0,
        "errors": 0,
    }

    for i, entry in enumerate(activities):
        original = entry.get("activity", "other")
        label = (entry.get("label") or "").strip()
        conf = regex_confidence(original, entry)

        entry = dict(entry)  # shallow copy
        entry["original_activity"] = original

        skip = False
        if not force_all:
            # Skip if regex confidence is already high
            if conf >= threshold:
                skip = True
            # Skip if activity is one of the clearly-labeled high-confidence types
            # AND the label strongly matches (conf check above covers this,
            # but be explicit for transparency)

        if skip:
            entry["ai_activity"] = original
            entry["ai_confidence"] = conf
            entry["ai_reasoning"] = "Skipped — regex confidence above threshold"
            entry["ai_reclassified"] = False
            entry["ai_skipped"] = True
            stats["skipped"] += 1
            enhanced.append(entry)
            continue

        # Send to AI
        stats["sent_to_ai"] += 1
        if verbose:
            pct = int((i / len(activities)) * 100)
            print(f"  [{pct:3d}%] AI classify ({i+1}/{len(activities)}): "
                  f"{original!r:20s} → label={label[:40]!r}")

        if dry_run:
            entry["ai_activity"] = original
            entry["ai_confidence"] = 0.0
            entry["ai_reasoning"] = "dry-run — not sent to API"
            entry["ai_reclassified"] = False
            entry["ai_skipped"] = False
            enhanced.append(entry)
            continue

        result = call_ollama(model, entry)

        if "api_error" in result:
            stats["errors"] += 1
            if verbose:
                print(f"         ERROR: {result['api_error']}")

        ai_activity = result.get("activity", original)
        ai_confidence = float(result.get("confidence", 0.5))
        ai_reasoning = result.get("reasoning", "")
        reclassified = ai_activity != original

        if reclassified:
            stats["reclassified"] += 1
            if verbose:
                print(f"         ↳ RECLASSIFIED: {original!r} → {ai_activity!r} "
                      f"(conf={ai_confidence:.2f}) — {ai_reasoning}")

        entry["ai_activity"] = ai_activity
        entry["ai_confidence"] = ai_confidence
        entry["ai_reasoning"] = ai_reasoning
        entry["ai_reclassified"] = reclassified
        entry["ai_skipped"] = False
        if "api_error" in result:
            entry["ai_error"] = result["api_error"]

        enhanced.append(entry)

        # Light rate-limiting — the API handles bursting but be polite
        if not dry_run and stats["sent_to_ai"] % 10 == 0:
            time.sleep(0.5)

    if verbose:
        print(f"\n  Summary: total={stats['total']} | "
              f"sent_to_ai={stats['sent_to_ai']} | "
              f"reclassified={stats['reclassified']} | "
              f"skipped={stats['skipped']} | "
              f"errors={stats['errors']}")

    return enhanced, stats


# ── Report writer ─────────────────────────────────────────────────────────────

def write_enhanced_json(original_report: dict, enhanced_activities: list[dict],
                        out_path: str, model_name: str):
    """Write the enhanced JSON — same structure as crawler output + AI fields."""
    report = dict(original_report)
    report["ai_classification"] = {
        "model": model_name,
        "model_type": "ollama_local",
        "classified_at": datetime.now().isoformat(),
        "threshold": original_report.get("_classifier_threshold", 0.75),
    }

    # Rebuild casb_activity_summary using AI-classified activities
    by_activity = {}
    for a in enhanced_activities:
        act = a.get("ai_activity") or a.get("activity", "other")
        by_activity[act] = by_activity.get(act, 0) + 1
    report["casb_activity_summary_ai"] = dict(sorted(by_activity.items()))

    report["activities"] = enhanced_activities

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  Enhanced JSON → {out_path}")


def write_review_report(enhanced_activities: list[dict], out_path: str,
                        app_domain: str):
    """
    Write a Markdown report for Phase 3 (human review dashboard).
    Groups by reclassified vs skipped, sorted by confidence ascending
    so reviewers see the most uncertain items first.
    """
    reclassified = [a for a in enhanced_activities if a.get("ai_reclassified")]
    uncertain = [a for a in enhanced_activities
                 if not a.get("ai_skipped") and not a.get("ai_reclassified")
                 and a.get("ai_confidence", 1.0) < 0.75]
    skipped = [a for a in enhanced_activities if a.get("ai_skipped")]
    errors = [a for a in enhanced_activities if a.get("ai_error")]

    lines = [
        f"# CASB AI Classification Report: {app_domain}",
        f"_Generated: {datetime.now().isoformat()}_",
        "",
        f"| Stat | Count |",
        f"|------|-------|",
        f"| Total activities | {len(enhanced_activities)} |",
        f"| Reclassified by AI | {len(reclassified)} |",
        f"| Uncertain (review needed) | {len(uncertain)} |",
        f"| Skipped (high regex confidence) | {len(skipped)} |",
        f"| API errors | {len(errors)} |",
        "",
        "> **Action required:** Review the Reclassified and Uncertain sections below.",
        "> Approve or override before feeding to code generator (Phase 4).",
        "",
        "---",
    ]

    def activity_row(a, idx):
        orig = a.get("original_activity", "?")
        ai = a.get("ai_activity", "?")
        conf = a.get("ai_confidence", 0)
        reason = a.get("ai_reasoning", "")
        label = a.get("label", "")
        page = a.get("page_url", "")
        xpath = a.get("target_xpath", "")
        html = (a.get("target_html") or "")[:200]
        steps = a.get("steps", [])
        nav_path = " → ".join(
            s.get("description", "") for s in steps[:-1]
        ) if len(steps) > 1 else ""

        block = [
            f"### {idx}. `{label}`",
            f"- **Original:** `{orig}` → **AI:** `{ai}` (confidence: {conf:.0%})",
        ]
        if reason:
            block.append(f"- **Reasoning:** {reason}")
        if nav_path:
            block.append(f"- **Path:** {nav_path}")
        block.append(f"- **Page:** `{page}`")
        if xpath:
            block.append(f"- **XPath:** `{xpath}`")
        if html:
            block.append(f"- **HTML:** `{html}`")
        block.append("")
        return block

    if reclassified:
        lines.append(f"\n## 🔄 Reclassified ({len(reclassified)})\n")
        lines.append("These were changed by the AI from the crawler's original classification.\n")
        for i, a in enumerate(
            sorted(reclassified, key=lambda x: x.get("ai_confidence", 0)), 1
        ):
            lines.extend(activity_row(a, i))

    if uncertain:
        lines.append(f"\n## ⚠️ Uncertain — review needed ({len(uncertain)})\n")
        lines.append("AI agreed with original but with low confidence.\n")
        for i, a in enumerate(
            sorted(uncertain, key=lambda x: x.get("ai_confidence", 0)), 1
        ):
            lines.extend(activity_row(a, i))

    if errors:
        lines.append(f"\n## ❌ API Errors ({len(errors)})\n")
        for i, a in enumerate(errors, 1):
            lines.append(f"- **{a.get('label', '?')}**: {a.get('ai_error', '?')}")

    lines.append(f"\n---\n\n## ✅ Skipped ({len(skipped)})\n")
    lines.append("High regex-confidence classifications — no AI call made.\n")
    by_type: dict[str, int] = {}
    for a in skipped:
        act = a.get("ai_activity", "other")
        by_type[act] = by_type.get(act, 0) + 1
    for act, count in sorted(by_type.items()):
        lines.append(f"- `{act}`: {count}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Review report  → {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="CASB AI Activity Classifier — Phase 2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input_json",
                   help="Path to <host>_casb_navigations.json from the crawler")
    p.add_argument("--out", default=None,
                   help="Output JSON path (default: <host>_classified.json)")
    p.add_argument("--threshold", type=float, default=0.75,
                   help="Regex confidence threshold — entries ABOVE this are skipped "
                        "(default: 0.75). Lower = more AI calls. Range: 0.0–1.0")
    p.add_argument("--all", dest="force_all", action="store_true",
                   help="Force AI classification on every entry, ignoring threshold")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be sent to AI without actually calling the API")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-item progress output")
    return p.parse_args()


def main():
    args = parse_args()

    # Load input
    input_path = Path(args.input_json)
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    print(f"\nCASB AI Classifier — Phase 2")
    print("=" * 50)
    print(f"Input:     {input_path}")
    print(f"Model:     {model} (local Ollama)")
    print(f"Threshold: {args.threshold}  (entries below this → AI, above → skip)")
    print(f"Mode:      {'force-all' if args.force_all else 'selective'}"
          f"{' [dry-run]' if args.dry_run else ''}")
    print()

    with open(input_path, encoding="utf-8") as f:
        report = json.load(f)

    activities = report.get("activities", [])
    app_domain = report.get("app_domain", "unknown")

    if not activities:
        print("No activities found in input JSON. Nothing to classify.")
        sys.exit(0)

    print(f"App:        {app_domain}")
    print(f"Activities: {len(activities)}")
    print()

    # Set up Ollama
    model = None
    if not args.dry_run:
        model = setup_ollama()
    else:
        model = DEFAULT_MODEL  # For dry run display purposes

    # Run classification
    report["_classifier_threshold"] = args.threshold
    enhanced, stats = classify_activities(
        activities=activities,
        model=model,
        threshold=args.threshold,
        force_all=args.force_all,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )

    # Derive output paths
    stem = input_path.stem.replace("_casb_navigations", "")
    out_json = args.out or str(input_path.parent / f"{stem}_classified.json")
    out_md = out_json.replace(".json", "_REPORT.md")

    # Write outputs
    print()
    write_enhanced_json(report, enhanced, out_json, model)
    write_review_report(enhanced, out_md, app_domain)

    print()
    print("=" * 50)
    print("CLASSIFICATION COMPLETE")
    print("=" * 50)
    print(f"Activities processed : {stats['total']}")
    print(f"Sent to AI           : {stats['sent_to_ai']}")
    print(f"Reclassified         : {stats['reclassified']}")
    print(f"Errors               : {stats['errors']}")
    print()
    print("Next step → Phase 3 (review dashboard) or Phase 4 (code generator)")
    print(f"Feed this into codegen:  {out_json}")


if __name__ == "__main__":
    main()
