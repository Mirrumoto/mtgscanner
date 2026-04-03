"""
build_exe.py — Build MTG Scanner as a standalone Windows EXE using PyInstaller.

Usage:
    python build_exe.py

This will create a dist/mtg_scanner/ folder with the executable inside.
"""

import PyInstaller.__main__
import sys
from pathlib import Path

def build_exe():
    """Build the MTG Scanner EXE with PyInstaller."""
    project_dir = Path(__file__).parent
    
    # PyInstaller arguments
    args = [
        str(project_dir / "app.py"),
        "--name=MTGScanner",
        "--onefile",  # Single EXE file
        "--icon=NONE",  # No custom icon (use default)
        "--windowed",  # No console window
        "--hidden-import=tkinter",
        "--hidden-import=PIL",
        "--hidden-import=PIL.Image",
        "--hidden-import=google.generativeai",
        "--hidden-import=openai",
        "--collect-all=google",  # Ensure Google libs are bundled
        "--collect-all=openai",  # Ensure OpenAI libs are bundled
        f"--distpath={project_dir}/dist",
        f"--workpath={project_dir}/build",
        f"--specpath={project_dir}",
    ]
    
    print("Building MTG Scanner EXE...")
    print(f"Output will be in: {project_dir}/dist/")
    print()
    
    try:
        PyInstaller.__main__.run(args)
        print()
        print("✓ Build complete!")
        print(f"✓ EXE located at: {project_dir}/dist/MTGScanner.exe")
        print()
        print("Note: Ensure your .env file is in the same directory as the EXE for API keys.")
    except Exception as e:
        print(f"✗ Build failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    build_exe()
