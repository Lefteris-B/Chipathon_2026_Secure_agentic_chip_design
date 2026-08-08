# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for chip-agent.

Build with:
    uv run --with pyinstaller pyinstaller packaging/chip-agent.spec --noconfirm

Produces a *onedir* bundle at dist/chip-agent/. Onedir (not onefile) is
deliberate: litellm + langgraph + textual weigh in heavily, and onefile pays
a multi-second extract-to-temp cost on every single invocation of a CLI the
user runs dozens of times per session.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH is injected by PyInstaller

datas: list = []
binaries: list = []
hiddenimports: list = []

# --------------------------------------------------------------------------
# Dependencies that resolve modules or data files at runtime. Static analysis
# misses these, so pull them in wholesale.
# --------------------------------------------------------------------------
COLLECT_ALL = [
    "textual",            # .tcss stylesheets + dynamically-imported widgets
    "litellm",            # model_prices_and_context_window.json, tokenizers
    "langgraph",
    "langgraph_checkpoint",
    "langgraph_sdk",
    "pydantic",
    "pydantic_settings",
    "tiktoken",
    "tiktoken_ext",       # classic PyInstaller gotcha: encodings live here
    "vcd",                # pyvcd
    "certifi",
    "tokenizers",
    "jsonschema_specifications",
]

for pkg in COLLECT_ALL:
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    except Exception:  # optional / not installed on this platform
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# chip_agent itself dispatches agents, tools and graph nodes by name, so make
# sure every submodule is reachable in the frozen bundle.
hiddenimports += collect_submodules("chip_agent")
hiddenimports += ["sqlite3", "encodings.idna"]

# --------------------------------------------------------------------------
# Data the CLI reads from disk at runtime.
# --------------------------------------------------------------------------
for rel in ("configs", "specs"):
    src = ROOT / rel
    if src.is_dir():
        datas.append((str(src), rel))

# Prompts, schemas and stylesheets that live inside the package tree.
RESOURCE_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".txt", ".tcss", ".css", ".sql", ".j2"}
pkg_root = ROOT / "chip_agent"
if pkg_root.is_dir():
    for path in pkg_root.rglob("*"):
        if path.is_file() and path.suffix in RESOURCE_SUFFIXES:
            datas.append((str(path), str(path.relative_to(ROOT).parent)))

# --------------------------------------------------------------------------

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "pytest",
        "mypy",
        "ruff",
        "IPython",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chip-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX corrupts some macOS/arm64 dylibs — not worth it
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # native only; no cross-compilation
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="chip-agent",
)
