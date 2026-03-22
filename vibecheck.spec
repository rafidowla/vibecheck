# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for VibeCheck.

Purpose:
    Configures how PyInstaller bundles the VibeCheck application into a
    native distributable.  Used by both macOS and Windows build scripts.

Usage:
    pyinstaller vibecheck.spec

Side Effects:
    Produces ``dist/VibeCheck.app`` (macOS) or ``dist/VibeCheck/`` (Windows).

Determinism: Deterministic given the same source tree.
Idempotency: Yes — overwrites previous build output.
"""

import platform
import sys
from pathlib import Path

block_cipher = None

_PROJECT_ROOT = Path(SPECPATH)

# ---------------------------------------------------------------------------
# Data files to bundle alongside the Python code
# ---------------------------------------------------------------------------
# prompts/ — editable AI prompt templates
# .env.example — shipped as a reference; user copies to .env on first run
_datas = [
    (str(_PROJECT_ROOT / "prompts"), "prompts"),
    (str(_PROJECT_ROOT / ".env.example"), "."),
]

# ---------------------------------------------------------------------------
# External binaries to bundle
# ---------------------------------------------------------------------------
# whisper-cli + its dylib dependencies (libwhisper, libggml, etc.)
# Build scripts copy and rpath-fix all files into build/bin/
_binaries = []
_bin_dir = _PROJECT_ROOT / "build" / "bin"

if _bin_dir.exists():
    for bin_file in _bin_dir.iterdir():
        if bin_file.is_file():
            _binaries.append((str(bin_file), "bin"))

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [str(_PROJECT_ROOT / "audit_tool" / "main.py")],
    pathex=[str(_PROJECT_ROOT)],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=[
        "audit_tool",
        "audit_tool.config",
        "audit_tool.audio_recorder",
        "audit_tool.mouse_tracker",
        "audit_tool.transcriber",
        "audit_tool.report_generator",
        "audit_tool.jira_client",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        "PIL.ImageFilter",
        "pynput",
        "pynput.mouse",
        "pynput.keyboard",
        "mss",
        "sounddevice",
        "soundfile",
        "Quartz",
        # HTTP / networking — needed for OpenRouter API calls
        "httpx",
        "httpx._transports",
        "httpx._transports.default",
        "httpcore",
        "httpcore._sync",
        "httpcore._async",
        "certifi",
        "h11",
        "anyio",
        "anyio._core",
        "sniffio",
        "idna",
        # Word document generation
        "docx",
        "docx.oxml",
        "docx.shared",
        "docx.enum",
        "docx.enum.text",
    ],

    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "pytest",
        "IPython",
        "matplotlib",
        "scipy",
        "pandas",
        "notebook",
        "jupyterlab",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# Executable
# ---------------------------------------------------------------------------
_icon_mac = str(_PROJECT_ROOT / "assets" / "icon.icns")
_icon_win = str(_PROJECT_ROOT / "assets" / "icon.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VibeCheck",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No terminal window
    icon=_icon_mac if platform.system() == "Darwin" else _icon_win,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="VibeCheck",
)

# ---------------------------------------------------------------------------
# macOS .app bundle
# ---------------------------------------------------------------------------
if platform.system() == "Darwin":
    app = BUNDLE(
        coll,
        name="VibeCheck.app",
        icon=_icon_mac,
        bundle_identifier="com.vibecheck.app",
        info_plist={
            "CFBundleName": "VibeCheck",
            "CFBundleDisplayName": "VibeCheck",
            "CFBundleVersion": "1.0.0",
            "CFBundleShortVersionString": "1.0.0",
            "NSMicrophoneUsageDescription": (
                "VibeCheck records audio to transcribe your spoken observations."
            ),
            "NSAppleEventsUsageDescription": (
                "VibeCheck needs accessibility permissions to track mouse clicks."
            ),
            "NSScreenCaptureUsageDescription": (
                "VibeCheck captures screenshots of the selected monitor "
                "to annotate with your click positions."
            ),
        },
    )
