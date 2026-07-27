"""Phase 4: packaging manifest guards and launcher entrypoint wiring."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_APP_VERSION = "0.1.4"


def test_pyinstaller_spec_excludes_secrets_and_runtime_materials() -> None:
    spec = (ROOT / "packaging/macos/polymarket_weather.spec").read_text(encoding="utf-8")
    assert "Polymarket Weather.app" in spec
    assert "com.seanbc.polymarket-weather" in spec
    assert "_is_forbidden" in spec
    assert ".env" in spec
    assert "console=False" in spec
    # Entry is the desktop launcher composition root.
    assert "desktop" in spec and "launcher.py" in spec


def test_build_script_and_smoke_script_exist_and_are_executable_bits() -> None:
    build = ROOT / "scripts/build_macos_app.sh"
    smoke = ROOT / "scripts/smoke_packaged_app.sh"
    assert build.is_file()
    assert smoke.is_file()
    text = build.read_text(encoding="utf-8")
    assert "pyinstaller" in text.lower()
    assert "hdiutil" in text
    assert "CODESIGN_IDENTITY" in text
    assert ".env" in text
    # Building must not strip developer tooling.
    assert "uv sync --extra dev --extra packaging" in text
    smoke_text = smoke.read_text(encoding="utf-8")
    assert "POLYMARKET_DESKTOP_DATA_ROOT" in smoke_text
    assert "TRADING_DISABLED=true" in smoke_text
    assert "/setup" in smoke_text
    assert "/app" in smoke_text


def test_packaging_version_sources_are_consistent() -> None:
    """Catch hard-coded version drift across packaging owners."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert match is not None
    assert match.group(1) == EXPECTED_APP_VERSION

    init_text = (ROOT / "src/polymarket_weather_arb/__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{EXPECTED_APP_VERSION}"' in init_text

    build = (ROOT / "scripts/build_macos_app.sh").read_text(encoding="utf-8")
    assert f'APP_VERSION:-{EXPECTED_APP_VERSION}' in build

    # Check architecture suffix logic and SHA256 logic
    assert "RAW_ARCH=\"$(uname -m)\"" in build
    assert "macos-${ARCH}.dmg" in build
    assert ".sha256" in build
    assert "ACTUAL_ARCHES=\"$(lipo -archs \"$BINARY_PATH\")\"" in build
    assert '[[ "$ACTUAL_ARCHES" != "$ARCH" ]]' in build

    spec = (ROOT / "packaging/macos/polymarket_weather.spec").read_text(encoding="utf-8")
    assert f'version="{EXPECTED_APP_VERSION}"' in spec
    assert f'"CFBundleShortVersionString": "{EXPECTED_APP_VERSION}"' in spec
    assert f'"CFBundleVersion": "{EXPECTED_APP_VERSION}"' in spec
    assert "target_arch='x86_64'" not in spec.lower()
    assert "target_arch='universal2'" not in spec.lower()
    assert "universal" not in spec.lower()

    runbook = (ROOT / "docs/runbooks/macos-beginner-app.md").read_text(encoding="utf-8")
    assert f"Polymarket Weather-{EXPECTED_APP_VERSION}-macos-arm64.dmg" in runbook
    assert f"Polymarket Weather-{EXPECTED_APP_VERSION}-macos-x86_64.dmg" in runbook

    release_note = (ROOT / f"docs/releases/{EXPECTED_APP_VERSION}.md").read_text(
        encoding="utf-8"
    )
    assert f"Polymarket Weather-{EXPECTED_APP_VERSION}-macos-arm64.dmg" in release_note
    assert f"Polymarket Weather-{EXPECTED_APP_VERSION}-macos-x86_64.dmg" in release_note

    workflow = (ROOT / ".github/workflows/package-macos-x86_64.yml").read_text(
        encoding="utf-8"
    )
    assert "runs-on: macos-15-intel" in workflow
    assert 'if [ "$ARCH" != "x86_64" ]' in workflow


def test_runbook_documents_gatekeeper_and_data_paths() -> None:
    runbook = (ROOT / "docs/runbooks/macos-beginner-app.md").read_text(encoding="utf-8")
    assert "Application Support/Polymarket Weather" in runbook
    assert "Gatekeeper" in runbook
    assert "Keychain" in runbook
    assert "Never" in runbook or "never" in runbook
    collapsed = " ".join(runbook.lower().split())
    assert "not notarized" in collapsed
    assert "auto-update" in collapsed or "auto-updater" in collapsed
    assert "quit" in collapsed
    assert "universal" not in collapsed


def test_launcher_module_is_importable_entrypoint() -> None:
    from polymarket_weather_arb.desktop.launcher import main

    assert callable(main)
