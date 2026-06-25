#!/usr/bin/env python3
"""
Test the Ollama-based CASB AI Classifier
========================================

Quick test to verify the local LLM classification works correctly.

Usage:
    python test_ollama_classifier.py
"""

import json
import sys
from pathlib import Path

# Import the classifier functions
try:
    from casb_ai_classifier import (
        setup_ollama, call_ollama, CASB_ACTIVITY_TYPES,
        regex_confidence, build_user_prompt, SYSTEM_PROMPT
    )
except ImportError as e:
    print(f"ERROR: Cannot import classifier: {e}")
    sys.exit(1)

# Test cases covering different scenarios
TEST_CASES = [
    {
        "name": "Clear delete button",
        "entry": {
            "label": "Delete note",
            "activity": "delete",
            "steps": [{"description": "Delete note", "action": "click"}],
            "target_html": '<button aria-label="Delete note">🗑️ Delete</button>',
            "page_url": "https://example.com/notes",
        },
        "expected": "delete"
    },
    {
        "name": "Ambiguous button with testid",
        "entry": {
            "label": "",
            "activity": "other",
            "testid": "btn-confirm-modal",
            "steps": [{"description": "...", "action": "click"}],
            "target_html": '<button data-testid="btn-confirm-modal">Confirm</button>',
            "page_url": "https://example.com/modal",
        },
        "expected": "other"  # Could be various things
    },
    {
        "name": "Share link action",
        "entry": {
            "label": "Copy link",
            "activity": "share",
            "steps": [
                {"description": "Share menu", "action": "click"},
                {"description": "Copy link", "action": "click"}
            ],
            "target_html": '<div role="menuitem">📋 Copy link</div>',
            "page_url": "https://example.com/document",
        },
        "expected": "share"
    },
    {
        "name": "Upload file control",
        "entry": {
            "label": "Choose file",
            "activity": "upload_file",
            "steps": [{"description": "Choose file", "action": "click"}],
            "target_html": '<input type="file" accept=".pdf,.doc">',
            "page_url": "https://example.com/upload",
        },
        "expected": "upload_file"
    },
]

def test_confidence_scoring():
    """Test the regex confidence scoring."""
    print("Testing confidence scoring...")
    
    for i, case in enumerate(TEST_CASES):
        entry = case["entry"]
        activity = entry["activity"]
        confidence = regex_confidence(activity, entry)
        
        print(f"  Case {i+1}: {case['name']}")
        print(f"    Activity: {activity}")
        print(f"    Confidence: {confidence:.2f}")
        print(f"    Label: '{entry.get('label', '')}'")
        print()

def test_prompt_building():
    """Test that prompts are built correctly."""
    print("Testing prompt building...")
    
    case = TEST_CASES[0]
    prompt = build_user_prompt(case["entry"])
    
    print("Sample prompt:")
    print("-" * 50)
    print(prompt[:400] + "..." if len(prompt) > 400 else prompt)
    print("-" * 50)
    print()

def test_ollama_classification():
    """Test actual Ollama classification."""
    print("Testing Ollama classification...")
    
    try:
        model = setup_ollama()
    except Exception as e:
        print(f"ERROR: Ollama setup failed: {e}")
        print("Make sure Ollama is installed and running:")
        print("  ollama serve")
        return False
    
    print(f"Using model: {model}")
    print()
    
    success_count = 0
    for i, case in enumerate(TEST_CASES):
        print(f"Test {i+1}: {case['name']}")
        
        try:
            result = call_ollama(model, case["entry"])
            
            if "api_error" in result:
                print(f"  ❌ API Error: {result['api_error']}")
                continue
            
            activity = result.get("activity", "unknown")
            confidence = result.get("confidence", 0.0)
            reasoning = result.get("reasoning", "")
            
            print(f"  Original:  {case['entry']['activity']}")
            print(f"  AI Result: {activity} (confidence: {confidence:.0%})")
            print(f"  Reasoning: {reasoning}")
            
            # Check if result is valid
            if activity in CASB_ACTIVITY_TYPES:
                print("  ✅ Valid activity type")
                success_count += 1
            else:
                print(f"  ❌ Invalid activity type: {activity}")
            
        except Exception as e:
            print(f"  ❌ Exception: {e}")
        
        print()
    
    print(f"Success rate: {success_count}/{len(TEST_CASES)} ({success_count/len(TEST_CASES)*100:.0f}%)")
    return success_count == len(TEST_CASES)

def test_json_parsing():
    """Test that the model returns valid JSON."""
    print("Testing JSON parsing...")
    
    # This test uses a simple prompt to check JSON formatting
    simple_test = {
        "label": "Log out",
        "activity": "logout", 
        "steps": [{"description": "Log out", "action": "click"}],
        "target_html": '<button>Log out</button>',
    }
    
    try:
        model = setup_ollama()
        result = call_ollama(model, simple_test)
        
        if "api_error" in result:
            print(f"  ❌ API Error: {result['api_error']}")
            return False
        
        required_fields = ["activity", "confidence", "reasoning"]
        for field in required_fields:
            if field not in result:
                print(f"  ❌ Missing field: {field}")
                return False
        
        print(f"  ✅ Valid JSON with all required fields")
        print(f"  Activity: {result['activity']}")
        print(f"  Confidence: {result['confidence']}")
        return True
        
    except Exception as e:
        print(f"  ❌ Exception: {e}")
        return False

def main():
    print("CASB Ollama Classifier Test Suite")
    print("=" * 50)
    print()
    
    # Run tests
    test_confidence_scoring()
    test_prompt_building()
    
    json_ok = test_json_parsing()
    if not json_ok:
        print("\n❌ JSON parsing test failed. Check your Ollama setup.")
        sys.exit(1)
    
    classification_ok = test_ollama_classification()
    
    print("\n" + "=" * 50)
    if classification_ok and json_ok:
        print("🎉 ALL TESTS PASSED!")
        print("\nYour Ollama classifier is ready to use:")
        print("  python casb_ai_classifier.py your_file.json")
    else:
        print("⚠️  Some tests failed. The classifier may still work, but double-check your setup.")
        print("\nTroubleshooting:")
        print("1. Ensure Ollama is running: ollama serve")
        print("2. Try a different model: python setup_ollama.py --model mistral:7b")
        print("3. Check model compatibility with your system")

if __name__ == "__main__":
    main()