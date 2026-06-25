#!/usr/bin/env python3
"""
Ollama Setup Helper for CASB AI Classifier
==========================================

Helps set up Ollama with the best models for CASB activity classification.
Run this once before using casb_ai_classifier.py.

Usage:
    python setup_ollama.py
    python setup_ollama.py --model mistral:7b
    python setup_ollama.py --list-models
"""

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import requests

# Configuration
OLLAMA_BASE_URL = "http://localhost:11434"
RECOMMENDED_MODELS = [
    ("llama3.1:8b", "Best overall - good accuracy and speed (4.7GB)"),
    ("mistral:7b", "Fast and efficient (4.1GB)"),
    ("phi3:medium", "Smallest good model (2.4GB)"),
    ("gemma2:9b", "Google's model, very accurate (5.4GB)"),
]

def check_ollama_installed():
    """Check if Ollama CLI is available."""
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

def start_ollama_server():
    """Attempt to start Ollama server."""
    print("Starting Ollama server...")
    try:
        if platform.system() == "Windows":
            # On Windows, try to start the service
            subprocess.Popen(["ollama", "serve"], 
                           creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # On Unix-like systems
            subprocess.Popen(["ollama", "serve"])
        
        # Wait a moment for server to start
        for i in range(10):
            if check_ollama_running():
                print("  ✓ Ollama server started successfully")
                return True
            time.sleep(1)
            print(f"  Waiting for server... ({i+1}/10)")
        
        return False
    except Exception as e:
        print(f"  ERROR: Failed to start server: {e}")
        return False

def list_available_models():
    """List currently available models."""
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        if response.status_code == 200:
            data = response.json()
            models = data.get("models", [])
            if models:
                print("\nCurrently available models:")
                for model in models:
                    name = model["name"]
                    size = model.get("size", 0)
                    size_gb = size / (1024**3) if size > 0 else 0
                    modified = model.get("modified_at", "")[:10]  # Just the date
                    print(f"  {name:<20} {size_gb:>6.1f}GB  {modified}")
            else:
                print("\nNo models currently available.")
            return [m["name"] for m in models]
        else:
            print("ERROR: Cannot connect to Ollama server")
            return []
    except Exception as e:
        print(f"ERROR: {e}")
        return []

def pull_model(model_name: str):
    """Download a model."""
    print(f"\nDownloading model: {model_name}")
    print("This may take several minutes depending on your internet connection...")
    
    try:
        # Use subprocess to show real-time progress
        result = subprocess.run(["ollama", "pull", model_name], 
                               text=True, capture_output=False)
        return result.returncode == 0
    except Exception as e:
        print(f"ERROR downloading model: {e}")
        return False

def test_model(model_name: str):
    """Test that a model works for classification."""
    print(f"\nTesting model: {model_name}")
    
    test_prompt = '''You are a CASB activity classifier. Classify this UI element:
  label: "Delete note"
  role: "button"
  
Respond with ONLY valid JSON:
{
  "activity": "delete",
  "confidence": 0.95,
  "reasoning": "Clear delete action button"
}'''
    
    try:
        payload = {
            "model": model_name,
            "prompt": test_prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 100}
        }
        
        response = requests.post(f"{OLLAMA_BASE_URL}/api/generate", 
                               json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            result = data.get("response", "").strip()
            print(f"  ✓ Model responded: {result[:100]}...")
            return True
        else:
            print(f"  ✗ API error: {response.status_code}")
            return False
    except Exception as e:
        print(f"  ✗ Test failed: {e}")
        return False

def install_instructions():
    """Show Ollama installation instructions."""
    system = platform.system()
    
    print("\n" + "="*60)
    print("Ollama Installation Instructions")
    print("="*60)
    
    if system == "Windows":
        print("1. Download Ollama for Windows:")
        print("   https://ollama.ai/download/windows")
        print("\n2. Run the installer")
        print("\n3. Restart your terminal/command prompt")
        
    elif system == "Darwin":  # macOS
        print("1. Download Ollama for macOS:")
        print("   https://ollama.ai/download/mac")
        print("\n2. Or install via Homebrew:")
        print("   brew install ollama")
        
    else:  # Linux
        print("1. Install via curl:")
        print("   curl -fsSL https://ollama.ai/install.sh | sh")
        print("\n2. Or download from:")
        print("   https://ollama.ai/download/linux")
    
    print("\n4. After installation, run this script again:")
    print("   python setup_ollama.py")

def main():
    parser = argparse.ArgumentParser(description="Setup Ollama for CASB AI Classifier")
    parser.add_argument("--model", default="", 
                       help="Specific model to download (e.g., llama3.1:8b)")
    parser.add_argument("--list-models", action="store_true",
                       help="List currently available models and exit")
    parser.add_argument("--test", default="",
                       help="Test a specific model")
    
    args = parser.parse_args()
    
    print("CASB Ollama Setup")
    print("="*50)
    
    # Check if Ollama is installed
    if not check_ollama_installed():
        print("❌ Ollama is not installed.")
        install_instructions()
        sys.exit(1)
    
    print("✅ Ollama CLI found")
    
    # Check if server is running
    if not check_ollama_running():
        print("⚠️  Ollama server is not running")
        if not start_ollama_server():
            print("\nManually start Ollama server with:")
            print("  ollama serve")
            sys.exit(1)
    else:
        print("✅ Ollama server is running")
    
    # Handle list models request
    if args.list_models:
        list_available_models()
        return
    
    # Handle test request
    if args.test:
        if test_model(args.test):
            print(f"✅ Model {args.test} is working correctly")
        else:
            print(f"❌ Model {args.test} failed test")
        return
    
    # Get available models
    available = list_available_models()
    
    # Determine which model to install
    if args.model:
        target_model = args.model
        print(f"\nTarget model: {target_model} (specified)")
    else:
        # Check if any recommended model is available
        for model_name, desc in RECOMMENDED_MODELS:
            if model_name in available:
                print(f"\n✅ Recommended model already available: {model_name}")
                if test_model(model_name):
                    print(f"\n🎉 Setup complete! You can now use casb_ai_classifier.py")
                    return
        
        # None available, suggest the best one
        target_model, desc = RECOMMENDED_MODELS[0]
        print(f"\nRecommended model: {target_model}")
        print(f"Description: {desc}")
        
        confirm = input(f"\nDownload {target_model}? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("\nSetup cancelled. Available models:")
            for model_name, desc in RECOMMENDED_MODELS:
                print(f"  python setup_ollama.py --model {model_name}")
            return
    
    # Download the model
    if pull_model(target_model):
        print(f"\n✅ Successfully downloaded: {target_model}")
        if test_model(target_model):
            print(f"\n🎉 Setup complete! You can now run:")
            print(f"    python casb_ai_classifier.py your_file.json")
        else:
            print(f"\n⚠️  Model downloaded but test failed. It might still work.")
    else:
        print(f"\n❌ Failed to download: {target_model}")
        print("\nTry a different model:")
        for model_name, desc in RECOMMENDED_MODELS[1:]:
            print(f"  python setup_ollama.py --model {model_name}")

if __name__ == "__main__":
    main()