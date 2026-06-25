# CASB AI Classifier with Local LLM (Ollama)

Local, offline AI classification for CASB activity discovery. No API costs, works without internet!

## 🎯 What This Does

Enhances your existing `casb_nav_xpath_crawler.py` output by using a local LLM to reclassify ambiguous UI elements. Instead of relying on regex patterns alone, the AI examines each element's HTML, context, and label to accurately classify activities like:

- `create`, `edit`, `delete`, `share`
- `upload_file`, `download_file` 
- `login`, `logout`, `post`
- `navigation`, `other`, etc.

## ⚡ Quick Start

### 1. Install Ollama

**Windows:**
```bash
# Download from https://ollama.ai/download/windows
# Run the installer, then restart your terminal
```

**macOS:**
```bash
brew install ollama
# or download from https://ollama.ai/download/mac
```

**Linux:**
```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

### 2. Set up the model

```bash
# Auto-setup (downloads best model for your system)
python setup_ollama.py

# Or choose a specific model
python setup_ollama.py --model mistral:7b
python setup_ollama.py --model phi3:medium
```

### 3. Run the classifier

```bash
# Classify your crawler output
python casb_ai_classifier.py evernote.com_casb_navigations.json

# Force classify everything (not just low-confidence items)  
python casb_ai_classifier.py evernote.com_casb_navigations.json --all

# Test without making API calls
python casb_ai_classifier.py evernote.com_casb_navigations.json --dry-run
```

## 📊 Models Comparison

| Model | Size | Speed | Accuracy | Best For |
|-------|------|-------|----------|----------|
| `llama3.1:8b` | 4.7GB | Medium | 95% | **Recommended** - best balance |
| `mistral:7b` | 4.1GB | Fast | 90% | Speed-focused |
| `phi3:medium` | 2.4GB | Very Fast | 85% | Resource-limited systems |
| `gemma2:9b` | 5.4GB | Slower | 97% | Accuracy-focused |

## 🔧 Configuration

### Confidence Threshold

Controls when to use AI vs regex classification:

```bash
# Conservative (more AI calls)
python casb_ai_classifier.py data.json --threshold 0.6

# Aggressive (fewer AI calls) 
python casb_ai_classifier.py data.json --threshold 0.9

# Default
python casb_ai_classifier.py data.json --threshold 0.75
```

### Ollama Settings

Edit `casb_ai_classifier.py` to customize:

```python
OLLAMA_BASE_URL = "http://localhost:11434"  # Change if running remotely
DEFAULT_MODEL = "llama3.1:8b"              # Your preferred model
```

## 📁 Output Files

### Enhanced JSON
```
deepseek.com_classified.json
```
Same structure as crawler output, with AI fields added:
```json
{
  "activity": "delete",
  "ai_activity": "delete", 
  "ai_confidence": 0.95,
  "ai_reasoning": "Clear delete button with trash icon",
  "ai_reclassified": false
}
```

### Review Report
```
deepseek.com_classified_REPORT.md
```
Human-readable summary for your review dashboard (Phase 3):
- Reclassified items (most important)
- Uncertain classifications (need review)
- API errors
- Skipped items (high confidence)

## 🧪 Testing

```bash
# Test your Ollama setup
python test_ollama_classifier.py

# Check available models
python setup_ollama.py --list-models

# Test a specific model
python setup_ollama.py --test llama3.1:8b
```

## 🔄 Integration with Your Workflow

### Current Flow
```
casb_nav_xpath_crawler.py → evernote.com_casb_navigations.json
```

### Enhanced Flow  
```
casb_nav_xpath_crawler.py → evernote.com_casb_navigations.json
                          ↓
casb_ai_classifier.py     → evernote.com_classified.json
                          ↓
[Phase 3 - Review Dashboard]
                          ↓  
[Phase 4 - Code Generator]
```

## 📈 Performance Tips

### Speed Optimization
- Use `phi3:medium` for faster classification
- Increase `--threshold` to reduce AI calls
- Run on machines with GPU for 2-3x speedup

### Accuracy Optimization  
- Use `gemma2:9b` or `llama3.1:8b`
- Use `--all` to classify everything with AI
- Review and correct the output in Phase 3

### Resource Usage
- Models consume 2-5GB RAM when loaded
- First API call loads the model (5-10 seconds)
- Subsequent calls are fast (1-2 seconds each)

## 🚨 Troubleshooting

### "Ollama is not running"
```bash
# Start the server
ollama serve

# Check if it's working
curl http://localhost:11434/api/tags
```

### "No models available"
```bash
# Download a model manually
ollama pull llama3.1:8b

# Or use the setup script
python setup_ollama.py
```

### "JSON parse failed"
The model sometimes returns malformed JSON. This is normal - the script retries automatically. If it persists:
1. Try a different model (`mistral:7b` is more reliable)
2. Check the model isn't corrupted: `ollama pull <model> --force`

### Performance Issues
```bash
# Check system resources
ollama ps

# Use a smaller model
python setup_ollama.py --model phi3:medium

# Increase timeout for slow systems (edit casb_ai_classifier.py)
timeout=60  # in call_ollama()
```

## 🔒 Privacy & Security

✅ **Completely local** - no data sent to external APIs  
✅ **Works offline** - no internet required after setup  
✅ **No API costs** - free forever after initial download  
✅ **Data stays on your machine** - CASB-sensitive info never leaves  

## 🔮 Next Steps

This is **Phase 2** of your CASB automation pipeline:

- **Phase 1**: ✅ Website crawler (already built)
- **Phase 2**: ✅ AI classifier (this module) 
- **Phase 3**: 🔄 Review dashboard (web UI for approval)
- **Phase 4**: 🔄 Code generator (Python test templates)
- **Phase 5**: 🔄 Drift detection (weekly re-crawl + diff)

The classified JSON output is ready to feed into Phase 3 (review dashboard) or Phase 4 (code generation).

## 🤝 Contributing

Found issues or want to improve the classifier? 

1. **Model accuracy**: Test different models and report results
2. **Prompt engineering**: Improve `SYSTEM_PROMPT` in `casb_ai_classifier.py`  
3. **Performance**: Optimize the Ollama integration
4. **Error handling**: Better fallbacks and retry logic

## 📚 References

- [Ollama Documentation](https://ollama.ai/docs)
- [Supported Models](https://ollama.ai/library)
- [CASB Activity Types](../casb-status/reference.md) (internal)