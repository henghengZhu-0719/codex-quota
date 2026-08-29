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
provider_icon_dir = project_dir / "assets" / "provider-icons"
provider_icons = [
    (str(provider_icon_dir / name), "assets/provider-icons")
    for name in ("codex.svg", "claude.svg")
]

a = Analysis(
    [str(project_dir / "codex_quota_window.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=provider_icons,
    hiddenimports=[
        "AppKit",
        "Foundation",
        "objc",
        "PyObjCTools.AppHelper",
        # 只有显式声明，PyInstaller 才会把 certifi 的 cacert.pem 打进包里；
        # 冻结后的 OpenSSL 自带的证书目录在 macOS 上并不存在。
        "certifi",
    ],
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
    # 不能开 UPX：压缩后的二进制在 macOS(尤其 Apple Silicon)上会破坏
    # code signature，导致 ad-hoc 签名失效、系统以「已损坏」拒绝启动。
    upx=False,
    console=False,
)

collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
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
