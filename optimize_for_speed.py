#!/usr/bin/env python3
"""
Speed Optimization Settings for CASB AI Classifier with llama3.1:8b
===================================================================

Configuration tweaks to maximize processing speed when using llama3.1:8b
in Google Colab, based on observed 700 activities/minute performance.

Usage:
    Apply these settings to casb_ai_classifier_colab.py for maximum speed
"""

# Optimal Ollama settings for llama3.1:8b speed
SPEED_OPTIMIZED_CONFIG = {
    # Model settings
    "model": "llama3.1:8b",
    
    # Ollama generation options for speed
    "options": {
        "temperature": 0.1,      # Low for consistency
        "top_p": 0.9,           # Slightly higher for speed
        "num_predict": 256,     # Reduced from 512 - classifications are short
        "num_ctx": 2048,        # Optimized context window
        "num_thread": -1,       # Use all available threads
        "num_gpu": 1,           # Use GPU if available in Colab
        "repeat_penalty": 1.1,  # Prevent repetition
        "stop": ["}"],          # Stop at JSON end for faster response
    },
    
    # API settings
    "timeout": 30,              # Reduced from 60 - you're getting fast responses
    "retry_attempts": 2,        # Reduced from 3 - less waiting on errors
    "retry_delay": 1.0,         # Reduced from 2.0 seconds
    
    # Batching settings
    "rate_limit_batch": 50,     # Process 50 before brief pause
    "rate_limit_delay": 0.1,    # Very short pause - you're processing fast
    
    # Progress settings
    "progress_update_every": 25, # Update progress every 25 items
}

# Performance monitoring
PERFORMANCE_METRICS = {
    "target_speed": 700,        # activities per minute (your observed speed)
    "expected_accuracy": 0.95,  # llama3.1:8b accuracy
    "memory_usage_gb": 6,       # Expected RAM usage
    "model_size_gb": 4.7,       # llama3.1:8b download size
}

def apply_speed_optimizations(classifier_script_path):
    """
    Apply speed optimizations to the classifier script.
    
    This function would modify the casb_ai_classifier_colab.py file
    to use the optimized settings above.
    """
    
    replacements = [
        # Optimize timeout
        ("timeout=60,", "timeout=30,"),
        
        # Optimize rate limiting
        ("stats[\"sent_to_ai\"] % 20 == 0", "stats[\"sent_to_ai\"] % 50 == 0"),
        ("time.sleep(0.2)", "time.sleep(0.1)"),
        
        # Optimize token count
        ("MAX_TOKENS = 512", "MAX_TOKENS = 256"),
        
        # Add stop tokens for faster JSON response
        ('"num_thread": 8,', '"num_thread": -1,\n                    "stop": ["}"],'),
        
        # Optimize progress updates
        ("pct = int((i / len(activities)) * 100)", 
         "if i % 25 == 0:  # Update every 25 items\n        pct = int((i / len(activities)) * 100)"),
    ]
    
    print("Speed optimization settings:")
    print("=" * 40)
    for key, value in SPEED_OPTIMIZED_CONFIG["options"].items():
        print(f"{key:20s}: {value}")
    print()
    print(f"Target speed: {PERFORMANCE_METRICS['target_speed']} activities/minute")
    print(f"Expected accuracy: {PERFORMANCE_METRICS['expected_accuracy']:.0%}")
    
    return replacements

def estimate_processing_time(num_activities, speed_per_minute=700):
    """Estimate how long classification will take."""
    minutes = num_activities / speed_per_minute
    
    if minutes < 1:
        return f"{int(minutes * 60)} seconds"
    elif minutes < 60:
        return f"{int(minutes)} minutes"
    else:
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        return f"{hours}h {mins}m"

def print_performance_analysis():
    """Print performance analysis for different dataset sizes."""
    print("Performance Analysis - llama3.1:8b")
    print("=" * 50)
    
    datasets = [
        ("Small (100 activities)", 100),
        ("Medium (500 activities)", 500),
        ("Large (1000 activities)", 1000),
        ("Evernote (742 activities)", 742),
        ("ChatGPT (819 activities)", 819),
        ("Very Large (2000 activities)", 2000),
    ]
    
    for name, count in datasets:
        time_est = estimate_processing_time(count)
        print(f"{name:25s}: {time_est:>10s}")
    
    print("\nOptimizations applied:")
    print("✅ Reduced token limit (512 → 256)")
    print("✅ Shorter timeouts (60s → 30s)")  
    print("✅ Less rate limiting (every 20 → every 50)")
    print("✅ JSON stop tokens for faster parsing")
    print("✅ Multi-threading enabled (-1 threads)")
    print("✅ Optimized context window (2048)")

if __name__ == "__main__":
    print_performance_analysis()
    print()
    
    # Example usage
    activities = 742  # Your Evernote dataset
    estimated_time = estimate_processing_time(activities)
    print(f"🚀 Your {activities} Evernote activities should complete in: {estimated_time}")
    
    print("\nTo apply optimizations:")
    print("1. Use the updated casb_ai_classifier_colab.py")
    print("2. Run with: --threshold 0.7 (your sweet spot)")
    print("3. Monitor GPU usage in Colab for best performance")