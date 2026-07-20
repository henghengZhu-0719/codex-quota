# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the macOS menu-bar application."""

from pathlib import Path


project_dir = Path(SPECPATH)
icon_file = next(
    (
        candidate
        for candidate in (
            project_dir / "assets" / "codex.icns",
            project_dir / "codex.icns",
        )
        if candidate.exists()
    ),
    None,
)
icon = str(icon_file) if icon_file is not None else None

a = Analysis(
    [str(project_dir / "codex_quota_window.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[],
    hiddenimports=["AppKit", "Foundation", "objc", "PyObjCTools.AppHelper"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodexQuota",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="CodexQuota",
)

app = BUNDLE(
    collection,
    name="CodexQuota.app",
    icon=icon,
    bundle_identifier="com.codexquota.menubar",
    info_plist={
        # A menu-bar utility should not add a second icon to the Dock.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleDisplayName": "Codex Quota",
    },
)
