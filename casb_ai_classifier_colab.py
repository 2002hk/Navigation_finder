#!/usr/bin/env python3
"""
CASB AI Activity Classifier — Phase 2 (Google Colab Version)
============================================================

Google Colab optimized version that handles Ollama installation and server
management automatically within the notebook environment.

Usage in Colab:
    !python casb_ai_classifier_colab.py www.evernote.com_casb_navigations.json
"""

import argparse
import json
import os
import re
import sys
import time
import subprocess
import signal
from datetime import datetime
from urllib.parse import urlparse

try:
    from pathlib import Path
except ImportError:
    try:
        from pathlib2 import Path
    except ImportError:
        print("ERROR: pathlib not available. Installing pathlib2...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pathlib2"])
        from pathlib2 import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    subprocess.run([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# ── Configuration ────────────────────────────────────────────────────────────

# Ollama configuration for Colab
OLLAMA_BASE_URL = "http://localhost:11434"
RECOMMENDED_MODELS = [
    "llama3.1:8b",     # Best balance - very good at classification
    "mistral:7b",      # Faster, still quite good  
    "phi3:medium",     # Smallest that works well
    "gemma2:9b",       # Google's model, good accuracy
]
DEFAULT_MODEL = "llama3.1:8b"  # Best balance for classification accuracy
MAX_TOKENS = 512
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_S = 2.0

# Global variables for Colab
SELECTED_MODEL = None
OLLAMA_PROCESS = None

# Activities the CASB system cares about
CASB_ACTIVITY_TYPES = [
    "create", "edit", "delete", "logout", "share", "upload_file",
    "download_file", "post", "login", "rename", "search", 
    "settings", "navigation", "other",
]

# High confidence activities (skip AI unless --all)
HIGH_CONFIDENCE_ACTIVITIES = {
    "login", "logout", "upload_file", "download_file", 
    "delete", "share", "post",
}

# High confidence patterns
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

HTML_SNIPPET_MAX = 400

# ── Colab-specific Ollama Management ─────────────────────────────────────────

def install_ollama_colab():
    """Install Ollama in Google Colab environment."""
    print("Installing Ollama in Colab...")
    try:
        # Install Ollama
        result = subprocess.run([
            "bash", "-c", 
            "curl -fsSL https://ollama.ai/install.sh | sh"
        ], capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            print(f"Installation error: {result.stderr}")
            return False
        
        print("✅ Ollama installed successfully")
        return True
    except Exception as e:
        print(f"❌ Installation failed: {e}")
        return False

def start_ollama_server_colab():
    """Start Ollama server in background for Colab."""
    global OLLAMA_PROCESS
    
    print("Starting Ollama server in background...")
    try:
        # Start server in background
        OLLAMA_PROCESS = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid if hasattr(os, 'setsid') else None
        )
        
        # Wait for server to start
        for i in range(20):
            try:
                response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=2)
                if response.status_code == 200:
                    print(f"✅ Ollama server started (attempt {i+1})")
                    return True
            except:
                pass
            time.sleep(1)
            print(f"  Waiting for server... ({i+1}/20)")
        
        print("❌ Server failed to start within 20 seconds")
        return False
        
    except Exception as e:
        print(f"❌ Failed to start server: {e}")
        return False

def check_ollama_available():
    """Check if Ollama command is available."""
    try:
        result = subprocess.run(["ollama", "--version"], 
                              capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False

def check_ollama_running():
    """Check if Ollama server is running."""
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False

def pull_model_colab(model_name: str):
    """Download a model in Colab."""
    print(f"Downloading model: {model_name}")
    print("This may take several minutes...")
    
    try:
        # Use subprocess with real-time output
        process = subprocess.Popen(
            ["ollama", "pull", model_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Show progress
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                print(output.strip())
        
        return process.returncode == 0
    except Exception as e:
        print(f"ERROR downloading model: {e}")
        return False

def setup_ollama_colab():
    """Complete Ollama setup for Colab."""
    global SELECTED_MODEL
    
    print("=" * 60)
    print("Setting up Ollama for Google Colab")
    print("=" * 60)
    
    # Check if Ollama is installed
    if not check_ollama_available():
        print("Ollama not found. Installing...")
        if not install_ollama_colab():
            raise RuntimeError("Failed to install Ollama")
    else:
        print("✅ Ollama is installed")
    
    # Start server if not running
    if not check_ollama_running():
        print("Starting Ollama server...")
        if not start_ollama_server_colab():
            raise RuntimeError("Failed to start Ollama server")
    else:
        print("✅ Ollama server is running")
    
    # Check available models
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        if response.status_code == 200:
            data = response.json()
            available_models = [m["name"] for m in data.get("models", [])]
            print(f"Available models: {available_models}")
        else:
            available_models = []
    except:
        available_models = []
    
    # Select or download model
    for model in RECOMMENDED_MODELS:
        if model in available_models:
            SELECTED_MODEL = model
            print(f"✅ Using existing model: {model}")
            break
    
    if not SELECTED_MODEL:
        print(f"Downloading default model: {DEFAULT_MODEL}")
        if pull_model_colab(DEFAULT_MODEL):
            SELECTED_MODEL = DEFAULT_MODEL
            print(f"✅ Downloaded: {DEFAULT_MODEL}")
        else:
            raise RuntimeError("Failed to download model")
    
    print("=" * 60)
    print(f"🎉 Setup complete! Using model: {SELECTED_MODEL}")
    print("=" * 60)
    
    return SELECTED_MODEL

def cleanup_ollama():
    """Clean up Ollama process on exit."""
    global OLLAMA_PROCESS
    if OLLAMA_PROCESS:
        try:
            # Kill the process group
            if hasattr(os, 'killpg'):
                os.killpg(os.getpgid(OLLAMA_PROCESS.pid), signal.SIGTERM)
            else:
                OLLAMA_PROCESS.terminate()
            OLLAMA_PROCESS.wait(timeout=5)
        except:
            pass

# Register cleanup
import atexit
atexit.register(cleanup_ollama)

# ── Confidence scoring ───────────────────────────────────────────────────────

def regex_confidence(activity: str, entry: dict) -> float:
    """Heuristic confidence for existing classification."""
    label = (entry.get("label") or "").strip()
    aria = (entry.get("steps", [{}])[-1].get("description") if entry.get("steps") else "") or ""
    testid = (entry.get("testid") or "").strip()
    hay = f"{label} {aria} {testid}".lower()

    if not hay.strip():
        return 0.0

    for pat, mapped_activity in HIGH_CONFIDENCE_PATTERNS:
        if pat.search(hay):
            if activity == mapped_activity:
                return 0.95
            return 0.3

    if activity in ("other", "navigation", "settings"):
        return 0.4

    if label and len(label) > 2:
        return 0.55

    return 0.2

# ── Prompt system ────────────────────────────────────────────────────────────

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
    """Build classification prompt for an entry."""
    label = (entry.get("label") or "").strip()
    category = (entry.get("category") or "").strip()
    current_activity = (entry.get("activity") or "").strip()
    role = (entry.get("role") or "").strip()
    testid = (entry.get("testid") or "").strip()
    href = (entry.get("href") or "").strip()
    html = (entry.get("target_html") or "")[:HTML_SNIPPET_MAX]
    page_url = (entry.get("page_url") or "").strip()

    steps = entry.get("steps", [])
    step_path = ""
    if steps:
        path_parts = []
        for s in steps[:-1]:
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

# ── Ollama API call ──────────────────────────────────────────────────────────

def call_ollama(model: str, entry: dict) -> dict:
    """Send entry to Ollama for classification."""
    user_prompt = build_user_prompt(entry)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            payload = {
                "model": model,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,  # Low for consistent classification
                    "top_p": 0.9,
                    "num_predict": MAX_TOKENS,
                    "num_ctx": 2048,     # Optimized context for llama3.1:8b
                    "num_thread": 8,     # Use multiple threads for speed
                }
            }
            
            response = requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json=payload,
                timeout=60,  # Longer timeout for Colab
            )
            
            if response.status_code != 200:
                raise Exception(f"Ollama API returned status {response.status_code}")
            
            data = response.json()
            raw = data.get("response", "").strip()

            # Clean up response
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

            result = json.loads(raw)

            # Validate
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

# ── Classification logic ─────────────────────────────────────────────────────

def classify_activities(activities, model, threshold=0.75, force_all=False, 
                       dry_run=False, verbose=True):
    """Classify activities using local LLM."""
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

        entry = dict(entry)
        entry["original_activity"] = original

        skip = False
        if not force_all and conf >= threshold:
            skip = True

        if skip:
            entry["ai_activity"] = original
            entry["ai_confidence"] = conf
            entry["ai_reasoning"] = "Skipped — regex confidence above threshold"
            entry["ai_reclassified"] = False
            entry["ai_skipped"] = True
            stats["skipped"] += 1
            enhanced.append(entry)
            continue

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

        # Light rate limiting - reduce since you're getting fast processing
        if not dry_run and stats["sent_to_ai"] % 20 == 0:
            time.sleep(0.2)  # Reduced delay since llama3.1:8b is fast

    if verbose:
        print(f"\n  Summary: total={stats['total']} | "
              f"sent_to_ai={stats['sent_to_ai']} | "
              f"reclassified={stats['reclassified']} | "
              f"skipped={stats['skipped']} | "
              f"errors={stats['errors']}")

    return enhanced, stats

# ── Output writers ───────────────────────────────────────────────────────────

def write_enhanced_json(original_report, enhanced_activities, out_path, model_name):
    """Write enhanced JSON output."""
    report = dict(original_report)
    report["ai_classification"] = {
        "model": model_name,
        "model_type": "ollama_local_colab",
        "classified_at": datetime.now().isoformat(),
        "threshold": original_report.get("_classifier_threshold", 0.75),
    }

    # Rebuild activity summary
    by_activity = {}
    for a in enhanced_activities:
        act = a.get("ai_activity") or a.get("activity", "other")
        by_activity[act] = by_activity.get(act, 0) + 1
    report["casb_activity_summary_ai"] = dict(sorted(by_activity.items()))

    report["activities"] = enhanced_activities

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  Enhanced JSON → {out_path}")

def write_review_report(enhanced_activities, out_path, app_domain):
    """Write Markdown review report."""
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

    # Add sections (simplified for Colab)
    if reclassified:
        lines.append(f"\n## 🔄 Reclassified ({len(reclassified)})\n")
        for i, a in enumerate(reclassified[:10]):  # Show first 10
            orig = a.get("original_activity", "?")
            ai = a.get("ai_activity", "?")
            conf = a.get("ai_confidence", 0)
            reason = a.get("ai_reasoning", "")
            label = a.get("label", "")
            lines.append(f"### {i+1}. `{label}`")
            lines.append(f"- **Original:** `{orig}` → **AI:** `{ai}` (confidence: {conf:.0%})")
            if reason:
                lines.append(f"- **Reasoning:** {reason}")
            lines.append("")
        if len(reclassified) > 10:
            lines.append(f"... and {len(reclassified) - 10} more entries")

    lines.append(f"\n## ✅ Summary\n")
    by_type = {}
    for a in enhanced_activities:
        act = a.get("ai_activity", "other")
        by_type[act] = by_type.get(act, 0) + 1
    for act, count in sorted(by_type.items()):
        lines.append(f"- `{act}`: {count}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Review report  → {out_path}")

# ── Main function ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CASB AI Classifier for Google Colab"
    )
    parser.add_argument("input_json", 
                       help="Path to <host>_casb_navigations.json from crawler")
    parser.add_argument("--out", default=None,
                       help="Output JSON path (default: <host>_classified.json)")
    parser.add_argument("--threshold", type=float, default=0.75,
                       help="Regex confidence threshold (default: 0.75)")
    parser.add_argument("--all", dest="force_all", action="store_true",
                       help="Force AI classification on every entry")
    parser.add_argument("--dry-run", action="store_true",
                       help="Print what would be sent without calling AI")
    parser.add_argument("--quiet", action="store_true",
                       help="Suppress progress output")
    
    args = parser.parse_args()

    # Load input
    input_path = Path(args.input_json)
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    print(f"\nCASB AI Classifier — Phase 2 (Colab)")
    print("=" * 50)
    print(f"Input:     {input_path}")

    with open(input_path, encoding="utf-8") as f:
        report = json.load(f)

    activities = report.get("activities", [])
    app_domain = report.get("app_domain", "unknown")

    if not activities:
        print("No activities found in input JSON. Nothing to classify.")
        sys.exit(0)

    # Setup Ollama
    model = None
    if not args.dry_run:
        model = setup_ollama_colab()
    else:
        model = DEFAULT_MODEL

    print(f"Model:     {model} (local Ollama in Colab)")
    print(f"Threshold: {args.threshold}  (entries below this → AI, above → skip)")
    print(f"Mode:      {'force-all' if args.force_all else 'selective'}"
          f"{' [dry-run]' if args.dry_run else ''}")
    print()

    print(f"App:        {app_domain}")
    print(f"Activities: {len(activities)}")
    print()

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

    # Generate output paths
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
    print(f"Enhanced JSON: {out_json}")

if __name__ == "__main__":
    main()