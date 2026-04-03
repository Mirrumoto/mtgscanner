# Building the MTG Scanner EXE

This guide explains how to convert the MTG Scanner app into a standalone Windows EXE file.

## Prerequisites

1. Python 3.8 or higher installed
2. All dependencies from `requirements.txt` (plus PyInstaller)

## Step-by-Step Build Instructions

### 1. Install PyInstaller

If you haven't already, install PyInstaller:

```powershell
pip install pyinstaller
```

Or update your `requirements-dev.txt` with:

```
pyinstaller>=6.0.0
```

### 2. Set Up Your Environment Variables

Before building, ensure your `.env` file is in the project root with your API keys and local server config:

```
GEMINI_API_KEY=your-key-here
UNSLOTH_AUTOSTART=1
# Optional; leave blank to auto-detect installed backends (unsloth, llama_cpp, ollama)
UNSLOTH_SERVER_COMMAND=
```

Notes:
- `UNSLOTH_SERVER_COMMAND` is optional; set it only if you want to force a specific backend command.
- The app picks a free local port at launch and sets `UNSLOTH_BASE_URL` dynamically.
- If you already use a fixed hosted/local URL, set `UNSLOTH_BASE_URL=...` and keep `UNSLOTH_AUTOSTART_OVERRIDE_BASE_URL=0`.

### 3. Build the EXE

Run the build script from the project directory:

```powershell
python build_exe.py
```

The script will:
- Compile all Python files and dependencies
- Bundle everything into a single EXE file
- Create the executable at `dist/MTGScanner.exe`
- Generate build artifacts in the `build/` folder

The build process may take 1-3 minutes depending on your system.

### 4. Deploy and Run

Once built, you can:

**Option A: Run from the dist folder**
```powershell
./dist/MTGScanner.exe
```

**Option B: Copy the EXE to another location**
```powershell
# Copy the EXE
Copy-Item dist/MTGScanner.exe "C:\path\to\your\folder\MTGScanner.exe"

# Copy your .env file to the same folder (for API keys)
Copy-Item .env "C:\path\to\your\folder\.env"
```

The EXE must be able to find your `.env` file in the same directory.
When `UNSLOTH_AUTOSTART=1`, the app starts your local inference server at launch and stops it on exit.

### 5. Troubleshooting

**"API key not found" error:**
- Ensure `.env` is in the same directory as `MTGScanner.exe`
- Make sure `GEMINI_API_KEY` is set (if using Gemini)

**"Could not start local inference server" error:**
- Ensure `UNSLOTH_SERVER_COMMAND` is valid and runs successfully from PowerShell.
- Ensure the server command launches an OpenAI-compatible API with a `/v1` base URL.
- If running a fixed endpoint manually, set `UNSLOTH_BASE_URL` and disable autostart (`UNSLOTH_AUTOSTART=0`).

**"Module not found" error:**
- Rebuild with: `python build_exe.py`
- Delete `build/` and `dist/` folders before rebuilding if issues persist

**File size too large:**
- The EXE is typically 100-200 MB due to bundled dependencies (Google, Pillow, etc.)
- This is normal and expected

## Creating a Shortcut

To create a desktop shortcut to the EXE:

1. Right-click on `MTGScanner.exe`
2. Select "Create Shortcut" or drag while holding Alt
3. Move the shortcut to your Desktop or Start Menu

You can optionally set a working directory for the shortcut to ensure `.env` is found.

## Rebuilding After Code Changes

After modifying the code:

1. Rebuild: `python build_exe.py`
2. The new EXE will be in `dist/MTGScanner.exe`

## Backup and Cleanup

Before rebuilding, you may want to archive the old EXE:

```powershell
# Backup current EXE
if (Test-Path dist/MTGScanner.exe) {
    Copy-Item dist/MTGScanner.exe "dist/MTGScanner.exe.backup"
}
```

To clean up build artifacts:

```powershell
Remove-Item -Recurse build/
Remove-Item -Recurse dist/
Remove-Item *.spec
```

---

**Questions?** Check the build output for detailed errors, or refer to [PyInstaller Documentation](https://pyinstaller.org/).
