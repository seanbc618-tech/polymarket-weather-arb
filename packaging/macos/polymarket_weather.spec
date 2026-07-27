# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Polymarket Weather macOS .app (Scheme A).

Build via scripts/build_macos_app.sh. Does not embed .env, databases, logs,
credentials, or local user fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
ENTRY = SRC / "polymarket_weather_arb" / "desktop" / "launcher.py"

# Collect package modules that are imported dynamically.
hiddenimports = []
for package in (
    "polymarket_weather_arb",
    "polymarket",
    "eth_account",
    "eth_keys",
    "eth_utils",
    "eth_hash",
    "eth_typing",
    "hexbytes",
    "cytoolz",
    "toolz",
    "pydantic",
    "pydantic_settings",
    "httpx",
    "anyio",
    "httpcore",
    "certifi",
    "h11",
    "idna",
    "sniffio",
    # Status menu (AppKit). Tk remains as stdlib fallback when AppKit missing.
    "AppKit",
    "Foundation",
    "objc",
    "PyObjCTools",
):
    try:
        hiddenimports.extend(collect_submodules(package))
    except Exception:
        hiddenimports.append(package)

datas = []
noaa_json = SRC / "polymarket_weather_arb" / "noaa_station_mapping.json"
if noaa_json.is_file():
    datas.append((str(noaa_json), "polymarket_weather_arb"))
brand_mark = (
    SRC / "polymarket_weather_arb" / "dashboard_ui" / "assets" / "brand-mark.png"
)
if brand_mark.is_file():
    datas.append(
        (str(brand_mark), "polymarket_weather_arb/dashboard_ui/assets")
    )

stream_css = SRC / "polymarket_weather_arb" / "dashboard_ui" / "stream_panel.css"
if stream_css.is_file():
    datas.append(
        (str(stream_css), "polymarket_weather_arb/dashboard_ui")
    )

# Explicit excludes: never ship secrets, runtime DBs, or developer junk.
excludes = [
    "pytest",
    "pytest_httpx",
    "ruff",
    "mypy",
    # Keep tkinter for status-menu fallback when AppKit is unavailable.
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Defense-in-depth: drop any accidentally collected env/db/log paths.
def _is_forbidden(path: str) -> bool:
    lower = path.lower().replace("\\", "/")
    forbidden_names = (
        "/.env",
        "config.env",
        ".db",
        ".sqlite",
        "/logs/",
        "private_key",
        "id_rsa",
        ".pem",
    )
    return any(token in lower for token in forbidden_names)


a.datas = [item for item in a.datas if not _is_forbidden(str(item[0]))]
a.binaries = [item for item in a.binaries if not _is_forbidden(str(item[0]))]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Polymarket Weather",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app; logs go to Application Support
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,  # native host arch
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Polymarket Weather",
)

app = BUNDLE(
    coll,
    name="Polymarket Weather.app",
    icon=None,
    bundle_identifier="com.seanbc.polymarket-weather",
    version="0.1.4",
    info_plist={
        "CFBundleName": "Polymarket Weather",
        "CFBundleDisplayName": "Polymarket Weather",
        "CFBundleShortVersionString": "0.1.4",
        "CFBundleVersion": "0.1.4",
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "LSUIElement": False,
        "NSHumanReadableCopyright": "Local research/trading tool. Not financial advice.",
    },
)
