#!/usr/bin/env python3
"""Native macOS menu-bar popover for Codex quota (NSStatusItem + NSPopover)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

import objc

try:
    from AppKit import (
        NSAppearance,
        NSAppearanceNameAqua,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApp,
        NSBezierPath,
        NSButton,
        NSColor,
        NSEventMaskLeftMouseUp,
        NSEventMaskRightMouseUp,
        NSEventTypeRightMouseUp,
        NSFont,
        NSFontWeightBold,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSImage,
        NSLineCapStyleRound,
        NSMenu,
        NSMenuItem,
        NSMinYEdge,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSStatusBar,
        NSTextAlignmentCenter,
        NSTextAlignmentRight,
        NSTextField,
        NSVariableStatusItemLength,
        NSView,
        NSViewController,
        NSViewHeightSizable,
        NSViewWidthSizable,
        NSVisualEffectBlendingModeBehindWindow,
        NSVisualEffectMaterialPopover,
        NSVisualEffectStateActive,
        NSVisualEffectView,
    )
    from Foundation import NSMakeRect, NSMakeSize, NSObject, NSTimer
    from PyObjCTools import AppHelper
except ImportError as exc:  # pragma: no cover - depends on the desktop environment
    raise SystemExit(
        "缺少 PyObjC。请运行 `python3 -m pip install pyobjc-framework-Cocoa` 后重试。"
    ) from exc

from codex_quota import QuotaError, _snapshots, fetch_quota


class ClaudeStatusError(RuntimeError):
    """A user-facing Claude usage query error."""


def _read_claude_oauth() -> dict[str, Any]:
    """Read the local Claude Code OAuth credential (access token + plan info).

    Prefers the macOS Keychain entry Claude Code itself writes
    ("Claude Code-credentials"), falling back to the on-disk
    ~/.claude/.credentials.json file it maintains as a secondary copy.
    Read-only: this never writes, logs, or sends the token anywhere except
    in the Authorization header of the one HTTPS request that follows.
    """
    raw: Optional[str] = None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-a",
                os.environ.get("USER", ""),
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        raw = None

    if not raw:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
        creds_path = os.path.expanduser(os.path.join(config_dir, ".credentials.json"))
        try:
            with open(creds_path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            raw = None

    if not raw:
        raise ClaudeStatusError("未找到 Claude 登录凭据，请先运行 `claude` 登录")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeStatusError("Claude 登录凭据格式无效") from exc

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise ClaudeStatusError("未找到 Claude 登录凭据，请先运行 `claude` 登录")
    return oauth


def fetch_claude_usage(timeout: float) -> dict[str, Any]:
    """Query Anthropic's (undocumented) `/api/oauth/usage` endpoint for the
    account's real Session (5h) / Weekly (7d) rate-limit percentages — the
    same kind of data Codex exposes officially via `account/rateLimits/read`,
    but which Claude Code has no public API for. Reuses the OAuth access
    token Claude Code itself stores locally; read-only, and no token refresh
    is attempted, so an expired token surfaces as an error asking the user
    to re-run `claude` rather than being silently renewed.
    """
    oauth = _read_claude_oauth()
    token = oauth.get("accessToken")

    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.69",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ClaudeStatusError("Claude 登录已过期，请重新运行 `claude` 登录") from exc
        if exc.code == 429:
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            suffix = f"，约 {retry_after} 秒后重试" if retry_after else ""
            raise ClaudeStatusError(f"Claude 用量查询被限流{suffix}") from exc
        raise ClaudeStatusError(f"Claude 用量查询失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise ClaudeStatusError(f"无法连接 Claude 用量接口：{exc.reason}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ClaudeStatusError("Claude 返回了无效的 JSON") from exc
    if not isinstance(data, dict):
        raise ClaudeStatusError("Claude 返回了意料之外的数据格式")

    data["_plan"] = oauth.get("subscriptionType")
    return data


def _parse_claude_reset(value: Any) -> Optional[int]:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if abs(value) >= 1e10 else int(value)
    return None


def _rgba(r: int, g: int, b: int, a: float = 1.0) -> "NSColor":
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)


ACCENT = _rgba(0x9B, 0x7B, 0xFF)
WARN = _rgba(0xF5, 0xC4, 0x51)
DANGER = _rgba(0xFF, 0x62, 0x59)
ERROR_TEXT = _rgba(0xFF, 0x8A, 0x80)
PRIMARY_TEXT = NSColor.blackColor()
SECONDARY_TEXT = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.85)
SUBTLE_TEXT = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.7)
MUTED_TEXT = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.48)
TRACK_COLOR = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.14)
SEPARATOR_COLOR = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.1)
BAR_TRACK_COLOR = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.12)

CARD_WIDTH = 320
MARGIN_X = 22
TOP_PAD = 18
BOTTOM_PAD = 20
HEADER_HEIGHT = 26
ICON_SIZE = 20.0
ICON_RING_WIDTH = 3.2


def _remaining(window: dict[str, Any]) -> int:
    try:
        used = int(window.get("usedPercent", 0))
    except (TypeError, ValueError):
        used = 0
    return max(0, min(100, 100 - used))


def _short_reset(timestamp: Any) -> str:
    if not isinstance(timestamp, int):
        return "Reset time unavailable"
    now = datetime.now().astimezone()
    reset = datetime.fromtimestamp(timestamp).astimezone()
    seconds = max(0, timestamp - int(now.timestamp()))
    if seconds < 60:
        return "Reset in < 1m"
    elif seconds < 3600:
        return f"Reset in {seconds // 60}m"
    elif reset.date() == now.date():
        return f"Reset in {seconds // 3600}h {seconds % 3600 // 60}m"
    elif reset.date() == now.date() + timedelta(days=1):
        return f"Reset tomorrow at {reset:%H:%M}"
    return f"Reset {reset:%a at %H:%M}"


def _tray_image(percent: Optional[int] = None, error: bool = False) -> "NSImage":
    """Draw a battery-style ring gauge for the status item — icon only, no text."""
    size = ICON_SIZE
    ring_width = ICON_RING_WIDTH
    rect = NSMakeRect(ring_width / 2, ring_width / 2, size - ring_width, size - ring_width)

    def draw(_rect) -> bool:
        track = NSBezierPath.bezierPath()
        track.appendBezierPathWithOvalInRect_(rect)
        track.setLineWidth_(ring_width)
        TRACK_COLOR.setStroke()
        track.stroke()

        if error:
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithOvalInRect_(rect)
            arc.setLineWidth_(ring_width)
            DANGER.setStroke()
            arc.stroke()
        elif percent is not None:
            remaining = max(0, min(100, percent))
            if remaining:
                color = ACCENT if remaining > 50 else (WARN if remaining > 20 else DANGER)
                center = (size / 2, size / 2)
                radius = (size - ring_width) / 2
                # 0 deg = 3 o'clock, angles grow counter-clockwise; start at
                # 12 o'clock and sweep clockwise by the remaining fraction.
                start_angle = 90.0
                end_angle = 90.0 - (remaining / 100.0) * 360.0
                arc = NSBezierPath.bezierPath()
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    center, radius, start_angle, end_angle, True
                )
                arc.setLineWidth_(ring_width)
                arc.setLineCapStyle_(NSLineCapStyleRound)
                color.setStroke()
                arc.stroke()
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(size, size), False, draw)
    image.setTemplate_(False)
    return image


class _FlippedView(NSView):
    """A plain NSView with a top-left origin so children lay out top-down."""

    def isFlipped(self) -> bool:
        return True


class _EscapeClosingEffectView(NSVisualEffectView):
    """The popover's background view; Escape closes it like the old widget did."""

    def isFlipped(self) -> bool:
        return True

    def acceptsFirstResponder(self) -> bool:
        return True

    def keyDown_(self, event) -> None:
        if event.keyCode() == 53:  # Escape
            popover = getattr(self, "owner_popover", None)
            if popover is not None:
                popover.close()
            return
        objc.super(_EscapeClosingEffectView, self).keyDown_(event)


class _RingBar(_FlippedView):
    """A small rounded progress bar, custom-drawn to mirror the card style."""

    def drawRect_(self, rect) -> None:
        bounds = self.bounds()
        radius = bounds.size.height / 2
        track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, radius, radius)
        BAR_TRACK_COLOR.setFill()
        track.fill()

        fraction = getattr(self, "fraction", 0.0)
        color = getattr(self, "bar_color", ACCENT)
        if fraction > 0:
            fill_width = max(bounds.size.height, bounds.size.width * fraction)
            fill_rect = NSMakeRect(0, 0, fill_width, bounds.size.height)
            fill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                fill_rect, radius, radius
            )
            color.setFill()
            fill_path.fill()


class _Separator(_FlippedView):
    def drawRect_(self, rect) -> None:
        SEPARATOR_COLOR.setFill()
        NSBezierPath.fillRect_(self.bounds())


def _make_label(
    text: str,
    size: float,
    weight: float = NSFontWeightRegular,
    color: "NSColor" = SECONDARY_TEXT,
    alignment=None,
) -> "NSTextField":
    label = NSTextField.labelWithString_(text)
    label.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    label.setTextColor_(color)
    if alignment is not None:
        label.setAlignment_(alignment)
    return label


def _make_icon_button(title: str, target: "NSObject", action: str) -> "NSButton":
    button = NSButton.buttonWithTitle_target_action_(title, target, action)
    button.setBordered_(False)
    button.setFont_(NSFont.systemFontOfSize_(15))
    button.setContentTintColor_(SUBTLE_TEXT)
    return button


class _Layout:
    """Tracks a running top-down y offset and stamps frames onto views."""

    def __init__(self, x: float, width: float, y: float) -> None:
        self.x = x
        self.width = width
        self.y = y
        self.views: list[Any] = []

    def add(self, view: Any, height: float) -> None:
        view.setFrame_(NSMakeRect(self.x, self.y, self.width, height))
        self.views.append(view)
        self.y += height

    def space(self, height: float) -> None:
        self.y += height


class QuotaMenuBarApp(NSObject):
    def initWithOptions_(self, options: dict[str, float]) -> "QuotaMenuBarApp":
        self = objc.super(QuotaMenuBarApp, self).init()
        if self is None:
            return None
        self.refresh_seconds = options["refresh"]
        self.timeout = options["timeout"]
        self.status_item = None
        self.popover = None
        self.effect_view = None
        self.header_refresh_button = None
        self._fetching = False
        self._refresh_timer = None
        self._last_close_time: Optional[float] = None
        return self

    # -- App lifecycle --------------------------------------------------
    def applicationDidFinishLaunching_(self, notification) -> None:
        self._build_status_item()
        self._build_popover()
        self._render_loading()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, "refreshTimerFired:", None, False
        )

    def _build_status_item(self) -> None:
        status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = status_item.button()
        button.setImage_(_tray_image())
        button.setToolTip_("Codex Quota")
        button.setTarget_(self)
        button.setAction_("statusItemClicked:")
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        self.status_item = status_item

    def _build_popover(self) -> None:
        effect_view = _EscapeClosingEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, CARD_WIDTH, 200)
        )
        # "Popover" material is the one designed to fill an NSPopover's
        # content edge-to-edge with no rounded shape of its own — HUD
        # material bakes in its own standalone rounded panel, which
        # doubled up with the popover's native rounded/arrow chrome and
        # showed as two overlapping outlines. Forcing dark appearance
        # (not the material choice) is what keeps it solidly dark
        # regardless of the system's light/dark setting or the desktop
        # content behind the window.
        effect_view.setMaterial_(NSVisualEffectMaterialPopover)
        effect_view.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        effect_view.setState_(NSVisualEffectStateActive)
        effect_view.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameAqua))
        # Apple-sanctioned vibrancy boost (no shape/layer of its own, so it
        # can't reintroduce the double-rounded-corner issue).
        effect_view.setEmphasized_(True)
        # The popover resizes its content view when setContentSize_ grows the
        # card (loading state -> full quota list); without this the backdrop
        # keeps its old frame and the newly exposed strip renders as the
        # popover's plain window background instead of the vibrancy material.
        effect_view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        dot = _make_label("●", 17, NSFontWeightRegular, ACCENT)
        dot.setFrame_(NSMakeRect(MARGIN_X, TOP_PAD, 16, HEADER_HEIGHT))
        effect_view.addSubview_(dot)

        title = _make_label("Codex", 15, NSFontWeightSemibold, PRIMARY_TEXT)
        title.setFrame_(NSMakeRect(MARGIN_X + 18, TOP_PAD, 120, HEADER_HEIGHT))
        effect_view.addSubview_(title)

        close_button = _make_icon_button("×", self, "closeClicked:")
        close_button.setFrame_(
            NSMakeRect(CARD_WIDTH - MARGIN_X - 24, TOP_PAD - 2, 24, HEADER_HEIGHT)
        )
        effect_view.addSubview_(close_button)

        refresh_button = _make_icon_button("↻", self, "refreshClicked:")
        refresh_button.setFrame_(
            NSMakeRect(CARD_WIDTH - MARGIN_X - 54, TOP_PAD - 2, 24, HEADER_HEIGHT)
        )
        effect_view.addSubview_(refresh_button)
        self.header_refresh_button = refresh_button

        body = _FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, TOP_PAD + HEADER_HEIGHT + 12, CARD_WIDTH, 10)
        )
        effect_view.addSubview_(body)
        self.body_view = body

        view_controller = NSViewController.alloc().init()
        view_controller.setView_(effect_view)

        popover = NSPopover.alloc().init()
        popover.setContentSize_(NSMakeSize(CARD_WIDTH, 160))
        popover.setBehavior_(NSPopoverBehaviorTransient)
        # Programmatic show/close (as opposed to a raw AppKit-internal
        # user click) can leave the close animation stuck mid-flight in
        # some launch contexts, wedging isShown() as True forever;
        # disabling animation makes open/close state changes immediate
        # and reliable.
        popover.setAnimates_(False)
        popover.setContentViewController_(view_controller)
        popover.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameAqua))
        popover.setDelegate_(self)
        effect_view.owner_popover = popover

        self.effect_view = effect_view
        self.popover = popover

        menu = NSMenu.alloc().init()
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "quitClicked:", ""
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)
        self.context_menu = menu

    # -- Status item interaction -----------------------------------------
    def statusItemClicked_(self, sender) -> None:
        event = NSApp.currentEvent()
        if event is not None and event.type() == NSEventTypeRightMouseUp:
            self.status_item.popUpStatusItemMenu_(self.context_menu)
            return
        self._toggle_popover()

    def _toggle_popover(self) -> None:
        if self.popover.isShown():
            self.popover.close()
            return
        # Clicking the status item button while a Transient popover is open
        # is itself treated as an "outside click" by AppKit, which closes
        # the popover a moment before this handler runs; skip the immediate
        # re-open so the click reads as a plain toggle-closed.
        if self._last_close_time is not None and time.monotonic() - self._last_close_time < 0.25:
            return
        button = self.status_item.button()
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            button.bounds(), button, NSMinYEdge
        )
        window = self.effect_view.window()
        if window is not None:
            window.makeFirstResponder_(self.effect_view)

    def popoverDidClose_(self, notification) -> None:
        self._last_close_time = time.monotonic()

    def closeClicked_(self, sender) -> None:
        self.popover.close()

    def refreshClicked_(self, sender) -> None:
        self.refresh()

    def quitClicked_(self, sender) -> None:
        NSApp.terminate_(None)

    # -- Data refresh -----------------------------------------------------
    def refreshTimerFired_(self, timer) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        self.header_refresh_button.setEnabled_(False)
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        codex_payload: Optional[dict[str, Any]] = None
        codex_error: Optional[str] = None
        try:
            codex_payload = fetch_quota(self.timeout)
        except (QuotaError, OSError) as exc:
            codex_error = str(exc)

        claude_usage: Optional[dict[str, Any]] = None
        claude_error: Optional[str] = None
        try:
            claude_usage = fetch_claude_usage(self.timeout)
        except (ClaudeStatusError, OSError) as exc:
            claude_error = str(exc)

        AppHelper.callAfter(
            self._on_fetch_done, codex_payload, codex_error, claude_usage, claude_error
        )

    def _on_fetch_done(
        self,
        codex_payload: Optional[dict[str, Any]],
        codex_error: Optional[str],
        claude_usage: Optional[dict[str, Any]],
        claude_error: Optional[str],
    ) -> None:
        self._render_status(codex_payload, codex_error, claude_usage, claude_error)
        self._finish_refresh()

    def _finish_refresh(self) -> None:
        self._fetching = False
        self.header_refresh_button.setEnabled_(True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self.refresh_seconds, self, "refreshTimerFired:", None, False
        )

    # -- Rendering ----------------------------------------------------------
    def _clear_body(self) -> None:
        for view in list(self.body_view.subviews()):
            view.removeFromSuperview()

    def _finish_layout(self, layout: "_Layout") -> None:
        for view in layout.views:
            self.body_view.addSubview_(view)
        body_height = max(layout.y, 40)
        total_height = TOP_PAD + HEADER_HEIGHT + 12 + body_height + BOTTOM_PAD
        # Only drive the popover's content size — never the effect view's own
        # frame. The popover positions its content view inside a frame view
        # that also carries the arrow and rounded chrome, so that frame has a
        # non-zero origin; pinning it to (0, 0) shifts the whole card down-left
        # and leaves an uncovered strip showing the bare window background.
        # The autoresizing mask set in _build_popover keeps the backdrop
        # filling whatever frame the popover hands it.
        self.popover.setContentSize_(NSMakeSize(CARD_WIDTH, total_height))
        self.body_view.setFrame_(
            NSMakeRect(0, TOP_PAD + HEADER_HEIGHT + 12, CARD_WIDTH, body_height)
        )
        self.effect_view.setNeedsDisplay_(True)

    def _render_loading(self) -> None:
        self._clear_body()
        layout = _Layout(MARGIN_X, CARD_WIDTH - 2 * MARGIN_X, 0)
        label = _make_label(
            "正在查询 Codex 剩余额度…", 12, NSFontWeightRegular, MUTED_TEXT
        )
        label.setAlignment_(NSTextAlignmentCenter)
        layout.add(label, 60)
        self._finish_layout(layout)

    def _render_status(
        self,
        codex_payload: Optional[dict[str, Any]],
        codex_error: Optional[str],
        claude_usage: Optional[dict[str, Any]],
        claude_error: Optional[str],
    ) -> None:
        layout = _Layout(MARGIN_X, CARD_WIDTH - 2 * MARGIN_X, 0)

        if codex_error:
            tray_percent: Optional[int] = None
            label = _make_label(
                f"查询失败\n\n{codex_error}\n\n请运行 codex login status 检查登录状态。",
                12,
                NSFontWeightRegular,
                ERROR_TEXT,
            )
            label.setAlignment_(NSTextAlignmentCenter)
            label.setPreferredMaxLayoutWidth_(CARD_WIDTH - 2 * MARGIN_X)
            label.cell().setWraps_(True)
            layout.add(label, 90)
        else:
            tray_percent = self._layout_codex_payload(layout, codex_payload)

        layout.space(18)
        layout.add(_Separator.alloc().init(), 1)
        layout.space(16)
        self._layout_claude_usage(layout, claude_usage, claude_error)

        layout.space(18)
        updated = _make_label(
            f"Updated {datetime.now().astimezone():%H:%M}",
            11,
            NSFontWeightRegular,
            MUTED_TEXT,
        )
        updated.setAlignment_(NSTextAlignmentRight)
        layout.add(updated, 16)

        self._clear_body()
        self._finish_layout(layout)

        if codex_error:
            self.status_item.button().setImage_(_tray_image(error=True))
            self.status_item.button().setToolTip_("Codex Quota · 查询失败")
        else:
            suffix = f" · {tray_percent}% remaining" if tray_percent is not None else ""
            self.status_item.button().setImage_(_tray_image(tray_percent))
            self.status_item.button().setToolTip_(f"Codex Quota{suffix}")

    def _layout_codex_payload(
        self, layout: "_Layout", payload: dict[str, Any]
    ) -> Optional[int]:
        snapshots = _snapshots(payload)
        if not snapshots:
            label = _make_label(
                "未返回限额信息。\n请确认当前使用 ChatGPT 账户登录 Codex。",
                12,
                NSFontWeightRegular,
                MUTED_TEXT,
            )
            label.setAlignment_(NSTextAlignmentCenter)
            layout.add(label, 40)
            return None

        tray_percent: Optional[int] = None
        for bucket_index, (bucket_id, snapshot) in enumerate(snapshots):
            if bucket_index:
                layout.space(16)
                layout.add(_Separator.alloc().init(), 1)
                layout.space(16)

            name = str(snapshot.get("limitName") or bucket_id)
            plan = snapshot.get("planType")
            heading = name + (f"  ·  {plan}" if plan else "")
            layout.add(_make_label(heading, 13, NSFontWeightSemibold, SECONDARY_TEXT), 18)

            windows = [
                ("Short Window", snapshot.get("primary")),
                ("Long Window", snapshot.get("secondary")),
            ]
            rendered = 0
            for label_text, window in windows:
                if not isinstance(window, dict):
                    continue
                layout.space(20)
                if rendered:
                    layout.add(_Separator.alloc().init(), 1)
                    layout.space(20)
                remaining = _remaining(window)
                if tray_percent is None:
                    tray_percent = remaining
                self._layout_window(layout, label_text, window, remaining)
                rendered += 1

            if not rendered:
                layout.space(8)
                layout.add(
                    _make_label("No active quota windows", 11, NSFontWeightRegular, MUTED_TEXT),
                    16,
                )

        return tray_percent

    def _layout_claude_usage(
        self,
        layout: "_Layout",
        usage: Optional[dict[str, Any]],
        error: Optional[str],
    ) -> None:
        if error:
            layout.add(_make_label("Claude", 13, NSFontWeightSemibold, SECONDARY_TEXT), 18)
            layout.space(6)
            label = _make_label(error, 11, NSFontWeightRegular, ERROR_TEXT)
            label.setPreferredMaxLayoutWidth_(layout.width)
            label.cell().setWraps_(True)
            layout.add(label, 32)
            return
        if usage is None:
            layout.add(_make_label("Claude", 13, NSFontWeightSemibold, SECONDARY_TEXT), 18)
            layout.space(6)
            layout.add(_make_label("正在查询…", 11, NSFontWeightRegular, MUTED_TEXT), 16)
            return

        plan = usage.get("_plan")
        heading = "Claude" + (f"  ·  {plan.capitalize()}" if plan else "")
        layout.add(_make_label(heading, 13, NSFontWeightSemibold, SECONDARY_TEXT), 18)

        windows = [
            ("Session (5h)", usage.get("five_hour")),
            ("Weekly", usage.get("seven_day")),
        ]
        rendered = 0
        for label_text, window_data in windows:
            if not isinstance(window_data, dict) or "utilization" not in window_data:
                continue
            layout.space(20)
            if rendered:
                layout.add(_Separator.alloc().init(), 1)
                layout.space(20)
            window = {
                "usedPercent": window_data.get("utilization", 0),
                "resetsAt": _parse_claude_reset(window_data.get("resets_at")),
            }
            remaining = _remaining(window)
            self._layout_window(layout, label_text, window, remaining)
            rendered += 1

        if not rendered:
            layout.space(8)
            layout.add(
                _make_label("No active usage windows", 11, NSFontWeightRegular, MUTED_TEXT),
                16,
            )

    def _layout_window(
        self, layout: "_Layout", label_text: str, window: dict[str, Any], remaining: int
    ) -> None:
        layout.add(_make_label(label_text, 12, NSFontWeightSemibold, SUBTLE_TEXT), 16)
        layout.space(8)

        percent = _make_label(f"{remaining}%", 36, NSFontWeightBold, PRIMARY_TEXT)
        percent.setAlignment_(NSTextAlignmentCenter)
        layout.add(percent, 44)
        layout.space(9)

        bar = _RingBar.alloc().init()
        bar.fraction = remaining / 100.0
        bar.bar_color = ACCENT if remaining > 50 else (WARN if remaining > 20 else DANGER)
        layout.add(bar, 7)
        layout.space(8)

        layout.add(
            _make_label(_short_reset(window.get("resetsAt")), 11, NSFontWeightRegular, MUTED_TEXT),
            16,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="以 macOS 菜单栏原生 popover 显示 Codex 剩余额度")
    parser.add_argument("--refresh", type=float, default=30, help="刷新间隔秒数（默认 30）")
    parser.add_argument("--timeout", type=float, default=15, help="查询超时秒数（默认 15）")
    args = parser.parse_args()
    if args.refresh < 5:
        parser.error("--refresh 必须大于或等于 5 秒")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def main() -> int:
    args = parse_args()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = QuotaMenuBarApp.alloc().initWithOptions_(
        {"refresh": args.refresh, "timeout": args.timeout}
    )
    app.setDelegate_(delegate)
    global _delegate_ref
    _delegate_ref = delegate  # keep a strong reference alive for the app's lifetime
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
