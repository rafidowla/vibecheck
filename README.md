# 🎯 VibeCheck

> **Record your screen. Speak your feedback. Get AI-ready task lists.**

VibeCheck captures your screen review sessions — audio + annotated screenshots — and uses a vision-language AI model to produce structured, actionable task lists that you can paste directly into AI coding agents like **Claude Code**, **Antigravity**, or **Cursor**.

## ✨ What It Does

1. **Record** — Pick a screen, start recording. Click anywhere to capture annotated screenshots (red crosshairs mark your clicks).
2. **Speak** — Talk through the issues you see while clicking through your app. Pause/resume anytime.
3. **Generate** — Stop recording and pick a Whisper model. The app transcribes your audio, sends the transcript + screenshots to an AI model, and generates:
   - **HTML** — dark-themed report with inline screenshots
   - **DOCX** — Word document with tasks and inline screenshots
   - **MD** — Markdown file ready to paste into an AI coding assistant
4. **Fix** — Each task includes implementation steps, likely file paths, and acceptance criteria — structured for AI agents to execute directly.

## 📂 Output Structure

```
insurance-tracking-ui/
├── insurance-tracking-ui.html    ← Interactive HTML report
├── insurance-tracking-ui.docx    ← Word document
├── insurance-tracking-ui.md      ← Markdown for AI agents
├── cost.txt                      ← AI usage cost summary
└── img/
    ├── click_0000.png            ← Annotated screenshots
    ├── click_0001.png
    └── ...
```

## 🚀 Quick Start

### Prerequisites

| Tool | macOS | Windows |
|------|-------|---------|
| Python 3.12+ | `brew install python@3.12` | `winget install Python.Python.3.12` |
| FFmpeg | `brew install ffmpeg` | `winget install Gyan.FFmpeg` |
| whisper.cpp | `brew install whisper-cpp` | [Download release](https://github.com/ggerganov/whisper.cpp/releases) |

> **macOS (Apple Silicon):** Make sure whisper-cpp is installed via the ARM Homebrew (`/opt/homebrew/bin/brew`) for Metal GPU acceleration. The Intel Homebrew version runs ~50× slower via Rosetta.

### Install

```bash
git clone git@bitbucket.org:rafidowla/vibecheck.git
cd VibeCheck
python -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate          # Windows

pip install -r requirements.txt
```

### Configure

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

```env
# Required: OpenRouter API key (get one at https://openrouter.ai)
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# AI model for report generation (default: Qwen VL 72B)
OPENROUTER_MODEL=qwen/qwen2.5-vl-72b-instruct

# Default Whisper model (can also be changed per-session in the UI)
WHISPER_MODEL=medium-q5

# Output directory (default: ~/Downloads/vibecheck-output)
# OUTPUT_DIR=~/Downloads/vibecheck-output
```

### Run

```bash
python -m audit_tool.main
```

## 🎙️ Whisper Model Options

Choose per-session when you click **Stop & Generate**:

| Model | RAM | Speed (1 min audio) | Best for |
|-------|-----|---------------------|----------|
| `base` | ~500 MB | ~6 seconds | Quick English-only sessions |
| `small` | ~900 MB | ~12 seconds | Basic multilingual |
| `medium-q5` ⭐ | ~1 GB | ~20 seconds | Multilingual, low memory |
| `medium` | ~2.5 GB | ~25 seconds | Multilingual, full quality |
| `large-v3-q5` | ~2 GB | ~40 seconds | Best quality, low memory |
| `large-v3` | ~4.5 GB | ~50 seconds | Highest accuracy |

Models auto-download on first use.

## 🖥️ Platform Support

| Feature | macOS | Windows |
|---------|-------|---------|
| Screen capture | ✅ mss | ✅ mss |
| Audio recording | ✅ sounddevice | ✅ sounddevice |
| Mouse tracking | ✅ pynput | ✅ pynput |
| Transcription | ✅ whisper.cpp (Metal GPU) | ✅ whisper.cpp (CUDA GPU) |
| GUI | ✅ tkinter | ✅ tkinter |
| Report generation | ✅ | ✅ |

## 🧱 Architecture

```
VibeCheck/
├── audit_tool/
│   ├── main.py              ← Tkinter GUI + orchestration
│   ├── audio_recorder.py    ← Microphone → WAV (sounddevice)
│   ├── mouse_tracker.py     ← Click capture + annotated screenshots
│   ├── transcriber.py       ← WAV → text (whisper.cpp subprocess)
│   ├── report_generator.py  ← Transcript + screenshots → HTML/DOCX/MD
│   └── config.py            ← Environment variables + session dirs
├── models/                  ← Local Whisper model binaries (auto-populated)
│   └── ggml-<model>.bin     ← Downloaded on first use per selected model
├── .env.example             ← Template for required environment variables
└── requirements.txt         ← Python dependencies
```

**Processing pipeline:**
```
Record → Stop → [Pick Whisper Model]
  → Transcribe audio (whisper.cpp subprocess)
  → Move screenshots to img/
  → Send transcript + images to AI (OpenRouter API)
  → Generate HTML, DOCX, MD reports
  → Delete recording.wav
  → Rename folder to AI-generated slug
  → Open HTML report
```

## 💰 Cost

- **Whisper transcription:** Free (runs locally)
- **AI report generation:** ~$0.002–0.01 per session via OpenRouter (Qwen VL 72B)
- Cost per session is logged in `cost.txt`

## 📄 License

[MIT](LICENSE)
