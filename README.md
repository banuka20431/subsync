Here is a complete, professional, and visually structured `README.md` for your repository. It includes all standard open-source sections, tailored perfectly to the features we just built into your script.

---

# SubSync 🎬

**An intelligent, zero-friction CLI for fetching, syncing, and transcribing video subtitles locally with AI.**

SubSync is a lightweight, single-file toolkit engineered to handle the everyday hassles of video subtitles. Whether your audio is 2 seconds out of sync, the local `.srt` file is locked in an obscure character encoding, or no subtitles exist online at all, SubSync handles the entire pipeline locally.

## ✨ Core Features

* 🔍 **Intelligent Discovery:** Queries OpenSubtitles, Podnapisi, Subscene, and Addic7ed using exact 64-bit video checksums and smart filename heuristics.
* ⏱️ **Precision Synchronization:** Shift timestamps globally or adjust for progressive frame-rate drift (e.g., `23.976 fps` to `25 fps`) with strict, overlap-safe index parsing.
* 🤖 **Local AI Speech-to-Text:** Generates `.srt` files straight from your video using `faster-whisper` (CTranslate2 INT8 quantization). Optimized for multi-core CPUs so you don't need an expensive GPU.
* 🧹 **Auto-Cleaner:** Automatically sanitizes `[hearing-impaired]` commentary, musical notes `♪`, and HTML formatting artifacts.
* 🔤 **Encoding Fixer:** Detects obscure encodings (like ISO-8859-1 or Windows-1252) and forces all output into clean, media-player-safe UTF-8 without double carriage return bugs.
* 📦 **Zero Bloat:** The entire architecture is contained within a single `subsync.py` file.

---

## 🛠️ Prerequisites

* **Python 3.10** or higher.
* **FFmpeg** (Required *only* for the AI `generate` command). Ensure it is installed and added to your system's `PATH`.

## 🚀 Installation

1. **Clone the repository:**
```bash
git clone https://github.com/yourusername/subsync.git
cd subsync

```


2. **Create a virtual environment (Recommended):**
```bash
python -m venv .venv

# Windows
.\.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

```


3. **Install dependencies:**
```bash
pip install requests beautifulsoup4 chardet rarfile faster-whisper tqdm

```



---

## 💻 Usage

SubSync operates using three primary commands: `download`, `sync`, and `generate`. You can pass a single file or an entire directory for batch processing.

### 1. Download Subtitles

Searches the web for matching subtitles. It prioritizes exact file hashes before falling back to filename scraping.

```bash
# Download English subtitles for a specific video and clean tags
python subsync.py download "D:/Movies/Inception.mkv" -l en --clean

# Batch download subtitles for an entire directory
python subsync.py download "D:/TV_Shows/The_Office" -l en

```

### 2. Sync & Shift

Fix subtitle delays, advance them, or correct frame-rate desynchronization.

```bash
# Shift subtitles earlier by 2 seconds (video audio happens before subtitle text)
python subsync.py sync "movie.srt" --shift -2.0 --clean

# Delay subtitles by 1.5 seconds (video audio happens after subtitle text)
python subsync.py sync "movie.srt" --shift 1.5

# Convert framerate to fix progressive drift
python subsync.py sync "movie.srt" --from-fps 23.976 --to-fps 25

```

### 3. Generate (AI Transcription)

No subtitles online? Generate them locally. SubSync extracts the audio and processes it through an optimized Whisper model, showing a real-time ETA progress bar.

```bash
# Generate subtitles using the default 'base' model
python subsync.py generate "video.mp4"

# Specify a larger model for better accuracy (requires more RAM)
python subsync.py generate "video.mkv" --model small -l en

```

*(Available models: `tiny`, `base`, `small`, `medium`, `large`)*

---

## ⚙️ Configuration

**OpenSubtitles API:**
To avoid rate limits while scraping OpenSubtitles, it is highly recommended to provide your own API key. Set it as an environment variable before running the script:

* **Windows (Command Prompt):** `set OPENSUBTITLES_API_KEY=your_api_key_here`
* **Windows (PowerShell):** `$env:OPENSUBTITLES_API_KEY="your_api_key_here"`
* **macOS/Linux:** `export OPENSUBTITLES_API_KEY="your_api_key_here"`

---

## 🤝 Contributing

Contributions are welcome! If you find a bug, have an idea for a new provider, or want to improve the regex cleaning logic:

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
