# MTG Scanner

A desktop app for scanning Magic: The Gathering card photos into a usable collection list with prices.

You point it at a folder of card images, it identifies cards with a vision model, resolves printings through Scryfall, pulls pricing, and lets you validate everything before saving.

> Note: the Windows EXE builder is currently not functional.

## What this app does

- Scans card images (`.jpg`, `.jpeg`, `.png`, `.webp`), including images with multiple cards
- Detects card name, set code, collector number, and finish (foil/nonfoil/unknown)
- Resolves cards through Scryfall with fallback matching
- Prices cards from MTGJSON (default) or Scryfall
- Shows live scan results in the GUI while scanning
- Lets you validate and correct printings before saving
- Saves collections as JSON files and supports editing/repricing later

## How it works (high level)

1. You launch `app.py`
2. The app loads `.env`
3. If local model autostart is enabled, it tries to start or detect a local OpenAI-compatible server
4. GUI opens (`PySide6`)
5. When you scan a folder:
   - `vision.py` identifies card candidates from each image
   - `scryfall.py` resolves candidates to exact card data (with cache + rate limiting)
   - `pricing.py` enriches cards with prices using MTGJSON or Scryfall
  - each image can contain one card or multiple cards
6. Results go to a validation view where you can adjust set/number/finish
7. You save to a named collection JSON in your local app data folder

## Main pieces in the project

- `app.py` - app entry point, env load, local server startup, GUI launch
- `gui_pyside.py` - desktop UI, scan workflow, collection management
- `scanner_engine.py` - scan pipeline for GUI callbacks
- `vision.py` - model/provider abstraction (Gemini or local OpenAI-compatible endpoint)
- `scryfall.py` - card lookups, caching, retry logic
- `pricing.py` - pricing source abstraction
- `mtgjson_prices.py` - MTGJSON download + SQLite price index
- `scan.py` - CLI scanning workflow (optional, separate from GUI)
- `local_server.py` - local inference server autodetect/autostart lifecycle

## Run the app

From the project root with your virtual environment active:

Windows (PowerShell):

```powershell
python app.py
```

macOS (Terminal):

```bash
python app.py
```

If that command does not work in your shell, use:

```bash
python3 app.py
```

## Local setup (Windows)

### 1) Requirements

- Python 3.10+ (3.11 recommended)
- Git
- Internet access for Scryfall and MTGJSON data
- One vision provider:
  - Gemini API key, or
  - local OpenAI-compatible server (Unsloth, llama.cpp server, or Ollama)

### 2) Create and activate a virtual environment

PowerShell:

```powershell
cd C:\path\to\mtgscanner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

Optional for packaging:

```powershell
pip install -r requirements-dev.txt
```

### 4) Configure environment variables

Copy `.env.example` to `.env` and edit values.

PowerShell quick copy:

```powershell
Copy-Item .env.example .env
```

Minimum setup for Gemini:

```dotenv
GEMINI_API_KEY=your-real-key
```

If you want to use local inference instead of Gemini, set your local endpoint variables in `.env`.
The app can auto-detect or auto-start supported local servers.

## Local setup (macOS)

### 1) Requirements

- Python 3.10+ (3.11 recommended)
- Git
- Internet access for Scryfall and MTGJSON data
- One vision provider:
  - Gemini API key, or
  - local OpenAI-compatible server (Unsloth, llama.cpp server, or Ollama)

If Python is not installed yet, easiest path is Homebrew:

```bash
brew install python
```

### 2) Create and activate a virtual environment

Terminal (zsh):

```bash
cd /path/to/mtgscanner
python3 -m venv .venv
source .venv/bin/activate
```

### 3) Install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Optional for packaging:

```bash
pip install -r requirements-dev.txt
```

### 4) Configure environment variables

```bash
cp .env.example .env
```

Minimum setup for Gemini:

```dotenv
GEMINI_API_KEY=your-real-key
```

If you want local inference instead of Gemini, set the local endpoint variables in `.env`.

## Unsloth + Ollama autodetect behavior

- With `UNSLOTH_AUTOSTART=1` (default), the app attempts to auto-detect a healthy local Ollama endpoint at startup.
- With `UNSLOTH_AUTOSTART=0`, startup autodetect is skipped, so set `UNSLOTH_BASE_URL` explicitly.

## Unsloth + Ollama setup (Windows)

Use this if you want local scanning with the app's `unsloth` provider while Ollama is the backend server.

### 1) Install Ollama

- Install from https://ollama.com/download/windows
- After install, open a new PowerShell window

### 2) Start Ollama server

Only do this if Ollama is not already running on your machine.

```powershell
ollama serve
```

Quick check before starting it manually:

```powershell
ollama list
```

If that command works and returns model info, Ollama is already running.

### 3) Pull Gemma 4 model(s)

Pick at least one model used by this app:

```powershell
ollama pull gemma4:e2b
ollama pull gemma4:e4b
ollama pull gemma4:26b
ollama pull gemma4:31b
```

If you only want one to start, `gemma4:e4b` is a good default balance.

### 4) Configure `.env`

```dotenv
UNSLOTH_BASE_URL=http://127.0.0.1:11434/v1
UNSLOTH_API_KEY=unsloth-local
UNSLOTH_AUTOSTART=0
```

### 5) Run app and select provider/model

- Start the app: `python app.py`
- In the app, set provider to `unsloth`
- Pick a model that matches what you pulled (for example `E4B (gemma4:e4b)`)

## Unsloth + Ollama setup (macOS)

### 1) Install Ollama

```bash
brew install --cask ollama
```

### 2) Start Ollama server

Only do this if Ollama is not already running.

```bash
ollama serve
```

Quick check before starting it manually:

```bash
ollama list
```

If that command works and returns model info, Ollama is already running.

### 3) Pull Gemma 4 model(s)

```bash
ollama pull gemma4:e2b
ollama pull gemma4:e4b
ollama pull gemma4:26b
ollama pull gemma4:31b
```

### 4) Configure `.env`

```dotenv
UNSLOTH_BASE_URL=http://127.0.0.1:11434/v1
UNSLOTH_API_KEY=unsloth-local
UNSLOTH_AUTOSTART=0
```

### 5) Run app and select provider/model

- Start the app: `python app.py` (or `python3 app.py`)
- Set provider to `unsloth`
- Pick a model that matches what you pulled

## Gemini notes (applies to Windows and macOS)

- Free mode is fine for testing, but it is 2.5-flash only and is rate-limited and quota-limited.
- In free mode, scans can slow down, fail with 429/rate-limit errors, or pause when daily quota is used up.
- For larger scans, paid usage is usually more reliable and consistent.

## First run checklist

- Pick your card image folder in the app
- Choose provider and model
- Click Start Scan
- Review cards in the validation view
- Save to a collection name

## Example test images

- This repo includes `examples.zip` in the project root.
- Extract it and point the scanner to the extracted folder when you want quick sample input images.

## Image best practices

- One image can contain one card or multiple cards.
- There is no hard card-per-image limit, but accuracy usually drops once an image has more than about 6 cards.
- Why accuracy drops: each card takes up fewer pixels, bottom text (set code and collector number) gets harder to read, and glare/blur/compression or overlap creates more ambiguous detections.
- For better results, keep cards flat, in focus, evenly lit, and avoid strong glare on lower text lines.

Collections are stored in:

- `%LOCALAPPDATA%\MTGBinderScanner\collections`

Other useful local files:

- Settings: `%LOCALAPPDATA%\MTGBinderScanner\settings.json`
- Logs: `%LOCALAPPDATA%\MTGBinderScanner\scanner.log`
- Thumbnails: `%LOCALAPPDATA%\MTGBinderScanner\thumb_cache`
- MTGJSON cache/index: `%LOCALAPPDATA%\MTGBinderScanner\mtgjson`
- Scryfall cache: `%LOCALAPPDATA%\MTGBinderScanner\scryfall_cache.json`

The app now defaults Scryfall cache to `MTGBinderScanner` for consistency with other app files.

If you want a custom location, set this in `.env`:

```dotenv
SCRYFALL_CACHE_PATH=%LOCALAPPDATA%\MTGBinderScanner\scryfall_cache.json
```

## Optional: Run CLI scan

You can also run the scanner without the GUI:

```powershell
python scan.py "C:\path\to\card-images" --output "my_collection.json"
```

## Build a Windows EXE (optional)

If you want a standalone executable:

```powershell
python build_exe.py
```

See `BUILD_EXE.md` for full packaging notes.

## Troubleshooting

### App opens but scan fails immediately

- Check `%LOCALAPPDATA%\MTGBinderScanner\scanner.log`
- Make sure your image folder exists and has supported file types

### Gemini errors

- Confirm `GEMINI_API_KEY` is set in `.env`
- Restart the app after changing `.env`

### Local model errors

- If autostart is enabled, confirm the backend is installed
- Or manually run your local server and set `UNSLOTH_BASE_URL`
- If you do not want autostart, set `UNSLOTH_AUTOSTART=0`
- `UNSLOTH_AUTOSTART=0` disables startup autodetect, so `UNSLOTH_BASE_URL` must be set

### Pricing looks missing or stale

- MTGJSON data refreshes daily
- If MTGJSON misses a card, fallback to Scryfall can still provide pricing
- Repricing can be triggered from the Collection tab pricing controls

## Notes

- The app keeps scan results in a temporary preview until you explicitly save.
- Existing collections can be repriced and edited from the Collection tab.
- Card images and API responses are cached locally to speed up repeat use.
