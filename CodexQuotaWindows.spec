# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the Windows command-line application."""

from pathlib import Path


project_dir = Path(SPECPATH)

a = Analysis(
    [str(project_dir / "codex_quota.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name="codex-quota",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
