#!/usr/bin/env sh
# Install chip-agent from an extracted release bundle.
#
#   ./install.sh                 # -> ~/.local/share/chip-agent, link in ~/.local/bin
#   PREFIX=/usr/local ./install.sh
#
# Uninstall: rm -rf "$LIBDIR" "$BINDIR/chip-agent"

set -eu

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -n "${PREFIX-}" ]; then
    LIBDIR="${PREFIX}/lib/chip-agent"
    BINDIR="${PREFIX}/bin"
else
    LIBDIR="${XDG_DATA_HOME:-$HOME/.local/share}/chip-agent"
    BINDIR="$HOME/.local/bin"
fi

if [ ! -x "${SOURCE_DIR}/chip-agent" ]; then
    echo "error: chip-agent executable not found next to this script." >&2
    echo "       Run install.sh from inside the extracted bundle directory." >&2
    exit 1
fi

echo "Installing chip-agent"
echo "  bundle -> ${LIBDIR}"
echo "  launcher -> ${BINDIR}/chip-agent"

mkdir -p "${LIBDIR}" "${BINDIR}"
rm -rf "${LIBDIR:?}/"*
# -L dereferences; keeps the copy self-contained if the bundle was moved around.
cp -RL "${SOURCE_DIR}/." "${LIBDIR}/"
rm -f "${LIBDIR}/install.sh"
chmod +x "${LIBDIR}/chip-agent"

# macOS: release binaries are unsigned, so Gatekeeper quarantines anything
# downloaded through a browser. Strip the flag rather than making the user
# right-click-open ~200 nested dylibs.
if [ "$(uname -s)" = "Darwin" ]; then
    xattr -dr com.apple.quarantine "${LIBDIR}" 2>/dev/null || true
fi

ln -sf "${LIBDIR}/chip-agent" "${BINDIR}/chip-agent"

echo
if command -v chip-agent >/dev/null 2>&1; then
    echo "Done. Try: chip-agent --help"
else
    echo "Done, but ${BINDIR} is not on your PATH. Add it:"
    echo
    echo "  echo 'export PATH=\"${BINDIR}:\$PATH\"' >> ~/.profile"
    echo
    echo "Or run it directly: ${BINDIR}/chip-agent --help"
fi

echo
echo "Note: real tool execution additionally needs Docker with the pinned"
echo "IIC-OSIC-TOOLS image. Tool-backed steps auto-skip when it is unavailable."
