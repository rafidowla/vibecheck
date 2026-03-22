#!/usr/bin/env bash
# --------------------------------------------------------------------------
# build_mac.sh — Build VibeCheck.app and wrap it in a .dmg
#
# Purpose:
#     Automates the full macOS build pipeline: venv setup, dependency
#     installation, whisper-cli binary copy, PyInstaller bundling, and
#     DMG creation.
#
# Usage:
#     cd /path/to/VibeCheck
#     bash build/build_mac.sh
#
# Prerequisites:
#     - Python 3.11 or 3.12 (Homebrew recommended)
#     - whisper-cpp installed: brew install whisper-cpp
#     - Xcode Command Line Tools (for hdiutil)
#
# Side Effects:
#     - Creates/overwrites build/bin/, dist/, and build/ directories
#     - Creates dist/VibeCheck.dmg
#
# Determinism: Deterministic given the same source + deps.
# Idempotency: Yes — overwrites previous build output.
# --------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "╔══════════════════════════════════════════════╗"
echo "║       VibeCheck — macOS Build Pipeline       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Project root: $PROJECT_ROOT"

cd "$PROJECT_ROOT"

# ------------------------------------------------------------------
# 1. Virtual environment
# ------------------------------------------------------------------
echo ""
echo "── Step 1/5: Setting up virtual environment ──"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install pyinstaller -q

echo "   ✅ venv ready ($(python3 --version))"

# ------------------------------------------------------------------
# 2. Copy whisper-cli binary + shared libraries
# ------------------------------------------------------------------
echo ""
echo "── Step 2/5: Locating whisper-cli binary + dylibs ──"

mkdir -p build/bin

WHISPER_BIN=""
for candidate in /opt/homebrew/bin/whisper-cli /usr/local/bin/whisper-cli; do
    if [ -f "$candidate" ]; then
        WHISPER_BIN="$candidate"
        break
    fi
done

if [ -z "$WHISPER_BIN" ]; then
    WHISPER_BIN="$(which whisper-cli 2>/dev/null || true)"
fi

if [ -n "$WHISPER_BIN" ]; then
    cp "$WHISPER_BIN" build/bin/whisper-cli
    chmod +x build/bin/whisper-cli
    echo "   ✅ Copied: $WHISPER_BIN → build/bin/whisper-cli"

    # Copy all dylib dependencies that use @rpath
    # Search multiple Homebrew paths: standard lib dirs + Cellar libexec
    LIB_SEARCH_DIRS=()
    for libdir in /opt/homebrew/lib /usr/local/lib; do
        [ -d "$libdir" ] && LIB_SEARCH_DIRS+=("$libdir")
    done
    # Also search the whisper-cpp Cellar libexec (where libggml* live)
    for cellar_lib in /opt/homebrew/Cellar/whisper-cpp/*/libexec/lib; do
        [ -d "$cellar_lib" ] && LIB_SEARCH_DIRS+=("$cellar_lib")
    done

    DYLIB_COUNT=0
    for dylib_name in $(otool -L build/bin/whisper-cli | grep '@rpath' | awk '{print $1}' | sed 's|@rpath/||'); do
        FOUND=""
        for search_dir in "${LIB_SEARCH_DIRS[@]}"; do
            src="$search_dir/$dylib_name"
            if [ -f "$src" ] || [ -L "$src" ]; then
                FOUND="$src"
                break
            fi
        done
        if [ -n "$FOUND" ]; then
            real_src="$(readlink -f "$FOUND" 2>/dev/null || realpath "$FOUND")"
            cp "$real_src" "build/bin/$dylib_name"
            chmod 755 "build/bin/$dylib_name"
            DYLIB_COUNT=$((DYLIB_COUNT + 1))
        else
            echo "   ⚠️  Could not find: $dylib_name"
        fi
    done
    echo "   ✅ Copied $DYLIB_COUNT shared libraries"

    # Rewrite @rpath references to @loader_path (same dir as the binary)
    for dylib_ref in $(otool -L build/bin/whisper-cli | grep '@rpath' | awk '{print $1}'); do
        install_name_tool -change "$dylib_ref" "@loader_path/$(basename $dylib_ref)" build/bin/whisper-cli 2>/dev/null || true
    done

    # Also fix dylib cross-references (dylibs that depend on each other)
    for dylib_file in build/bin/lib*.dylib; do
        [ -f "$dylib_file" ] || continue
        for ref in $(otool -L "$dylib_file" | grep '@rpath' | awk '{print $1}'); do
            install_name_tool -change "$ref" "@loader_path/$(basename $ref)" "$dylib_file" 2>/dev/null || true
        done
    done
    echo "   ✅ Fixed @rpath → @loader_path for all binaries"
else
    echo "   ⚠️  whisper-cli not found. Install it with: brew install whisper-cpp"
    echo "   The app will still build but transcription won't work without it."
fi

# ------------------------------------------------------------------
# 3. Generate icons (if source PNG exists but .icns doesn't)
# ------------------------------------------------------------------
echo ""
echo "── Step 3/5: Checking app icons ──"

if [ -f "assets/icon.png" ] && [ ! -f "assets/icon.icns" ]; then
    echo "   Generating icon.icns from icon.png..."
    ICONSET_DIR="build/icon.iconset"
    mkdir -p "$ICONSET_DIR"
    sips -z 16 16     assets/icon.png --out "$ICONSET_DIR/icon_16x16.png"    > /dev/null 2>&1
    sips -z 32 32     assets/icon.png --out "$ICONSET_DIR/icon_16x16@2x.png" > /dev/null 2>&1
    sips -z 32 32     assets/icon.png --out "$ICONSET_DIR/icon_32x32.png"    > /dev/null 2>&1
    sips -z 64 64     assets/icon.png --out "$ICONSET_DIR/icon_32x32@2x.png" > /dev/null 2>&1
    sips -z 128 128   assets/icon.png --out "$ICONSET_DIR/icon_128x128.png"  > /dev/null 2>&1
    sips -z 256 256   assets/icon.png --out "$ICONSET_DIR/icon_128x128@2x.png" > /dev/null 2>&1
    sips -z 256 256   assets/icon.png --out "$ICONSET_DIR/icon_256x256.png"  > /dev/null 2>&1
    sips -z 512 512   assets/icon.png --out "$ICONSET_DIR/icon_256x256@2x.png" > /dev/null 2>&1
    sips -z 512 512   assets/icon.png --out "$ICONSET_DIR/icon_512x512.png"  > /dev/null 2>&1
    sips -z 1024 1024 assets/icon.png --out "$ICONSET_DIR/icon_512x512@2x.png" > /dev/null 2>&1
    iconutil -c icns "$ICONSET_DIR" -o assets/icon.icns
    echo "   ✅ Generated assets/icon.icns"
else
    echo "   ✅ Icon already exists or no source PNG"
fi

# ------------------------------------------------------------------
# 4. Run PyInstaller
# ------------------------------------------------------------------
echo ""
echo "── Step 4/5: Running PyInstaller ──"

pyinstaller vibecheck.spec --noconfirm --clean 2>&1 | tail -5

echo "   ✅ Build complete → dist/VibeCheck.app"

# ------------------------------------------------------------------
# 5. Create DMG
# ------------------------------------------------------------------
echo ""
echo "── Step 5/5: Creating DMG ──"

DMG_NAME="VibeCheck.dmg"
DMG_PATH="dist/$DMG_NAME"

# Remove old DMG if present
rm -f "$DMG_PATH"

# Create a temporary directory for DMG contents
DMG_STAGING="build/dmg_staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "dist/VibeCheck.app" "$DMG_STAGING/"

# Add a symlink to /Applications for drag-install
ln -s /Applications "$DMG_STAGING/Applications"

# Create DMG using hdiutil
hdiutil create \
    -volname "VibeCheck" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDZO \
    "$DMG_PATH" \
    > /dev/null 2>&1

echo "   ✅ Created: $DMG_PATH"

# Cleanup
rm -rf "$DMG_STAGING"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           Build Complete! 🎉                 ║"
echo "║                                              ║"
echo "║   Install:  open dist/VibeCheck.dmg          ║"
echo "║   Run:      open /Applications/VibeCheck.app ║"
echo "╚══════════════════════════════════════════════╝"
