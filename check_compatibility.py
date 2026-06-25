#!/usr/bin/env python
"""
Python Compatibility Checker for CASB Ollama Classifier
=======================================================

Checks if your Python version and dependencies are compatible.
Run this before using the Ollama classifier.

Usage:
    python check_compatibility.py
"""

import sys
import subprocess

def check_python_version():
    """Check if Python version is supported."""
    version = sys.version_info
    print(f"Python version: {version.major}.{version.minor}.{version.micro}")
    
    if version.major < 3:
        print("❌ Python 3.6+ required. You have Python 2.")
        return False
    elif version.major == 3 and version.minor < 6:
        print("❌ Python 3.6+ required. Please upgrade.")
        return False
    else:
        print("✅ Python version is supported")
        return True

def check_dependency(package, import_name=None):
    """Check if a package is available."""
    if import_name is None:
        import_name = package
    
    try:
        __import__(import_name)
        print(f"✅ {package}")
        return True
    except ImportError:
        print(f"❌ {package} - install with: pip install {package}")
        return False

def install_missing_deps():
    """Try to install missing dependencies."""
    print("\nAttempting to install missing dependencies...")
    
    deps = ["requests"]
    if sys.version_info < (3, 7):
        deps.append("pathlib2")
    
    for dep in deps:
        try:
            print(f"Installing {dep}...")
            result = subprocess.run([sys.executable, "-m", "pip", "install", dep],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                print(f"  ✅ {dep} installed")
            else:
                print(f"  ❌ {dep} failed to install")
        except Exception as e:
            print(f"  ❌ {dep} error: {e}")

def main():
    print("CASB Ollama Classifier Compatibility Check")
    print("=" * 50)
    
    # Check Python version
    python_ok = check_python_version()
    if not python_ok:
        sys.exit(1)
    
    print("\nChecking dependencies:")
    
    # Check core dependencies
    deps_ok = True
    deps_ok &= check_dependency("requests")
    
    # Python 3.6 needs pathlib2
    if sys.version_info < (3, 7):
        deps_ok &= check_dependency("pathlib2")
    
    # Check optional dependencies
    print("\nOptional dependencies:")
    check_dependency("playwright")
    
    if not deps_ok:
        print("\n⚠️  Some required dependencies are missing.")
        try_install = input("Try to install them automatically? [y/N]: ").strip().lower()
        if try_install in ("y", "yes"):
            install_missing_deps()
            print("\nRe-checking dependencies:")
            check_dependency("requests")
            if sys.version_info < (3, 7):
                check_dependency("pathlib2")
    
    print("\n" + "=" * 50)
    
    if python_ok and deps_ok:
        print("🎉 Your system is ready for the CASB Ollama Classifier!")
        print("\nNext steps:")
        print("1. Install Ollama: https://ollama.ai/")
        print("2. Run: python setup_ollama.py")
        print("3. Test: python casb_ai_classifier.py your_file.json")
    else:
        print("⚠️  Please fix the issues above before proceeding.")
        print("\nFor manual installation:")
        print("pip install requests")
        if sys.version_info < (3, 7):
            print("pip install pathlib2")

if __name__ == "__main__":
    main()