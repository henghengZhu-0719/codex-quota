#!/usr/bin/env python3
"""Native macOS menu-bar popover for Codex quota (NSStatusItem + NSPopover)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import objc

try:
    from AppKit import (
        NSAppearance,
        NSAppearanceNameAqua,
        NSAlert,
        NSAlertFirstButtonReturn,
        NSAlertThirdButtonReturn,
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
        NSFontAttributeName,
        NSFontWeightBold,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSImage,
        NSImageView,
        NSMenu,
        NSMenuItem,
        NSMinYEdge,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSForegroundColorAttributeName,
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
    from Foundation import (
        NSMakePoint,
        NSMakeRect,
        NSMakeSize,
        NSObject,
        NSString,
        NSTimer,
        NSUserDefaults,
    )
    from PyObjCTools import AppHelper
except ImportError as exc:  # pragma: no cover - depends on the desktop environment
    raise SystemExit(
        "缺少 PyObjC。请运行 `python3 -m pip install pyobjc-framework-Cocoa` 后重试。"
    ) from exc

from codex_quota import QuotaError, _snapshots, fetch_quota, normalize_proxy_url
from codex_usage import ssl_context
from token_usage import DailyTokenUsage, TokenTotals, scan_token_usage


def _resource_path(*parts: str) -> Path:
    """Resolve a resource in source checkouts and PyInstaller bundles."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root.joinpath(*parts)


_PROVIDER_ICON_CACHE: dict[str, Optional["NSImage"]] = {}


def _provider_icon(name: str) -> Optional["NSImage"]:
    cached = _PROVIDER_ICON_CACHE.get(name)
    if cached is not None:
        return cached
    path = _resource_path("assets", "provider-icons", f"{name}.svg")
    image = NSImage.alloc().initWithContentsOfFile_(str(path)) if path.is_file() else None
    if image is not None:
        image.setTemplate_(True)
    _PROVIDER_ICON_CACHE[name] = image
    return image


class ClaudeStatusError(RuntimeError):
    """A user-facing Claude usage query error."""


def _parse_claude_oauth(raw: str) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return oauth if isinstance(token, str) and token.strip() else None


def _claude_keychain_services() -> list[str]:
    """Return the service names Claude Code uses for this config directory."""
    base = "Claude Code-credentials"
    literal = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not literal:
        return [base]
    normalized = unicodedata.normalize("NFC", literal)
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return [f"{base}-{suffix}", base]


def _read_claude_oauth_candidates() -> list[dict[str, Any]]:
    """Read Claude Code logins in OpenUsage's keychain-before-file order.

    More than one candidate is intentionally retained: recent Claude Code
    versions treat Keychain as the source of truth but can leave a stale file
    behind, while a fresh external login can occasionally land in the other
    source. The caller can therefore fall through after an auth rejection.
    """
    candidates: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()

    def append(raw: Optional[str]) -> None:
        if not raw:
            return
        oauth = _parse_claude_oauth(raw)
        if oauth is None:
            return
        token = str(oauth["accessToken"]).strip()
        if token not in seen_tokens:
            seen_tokens.add(token)
            candidates.append(oauth)

    user = os.environ.get("USER", "").strip()
    for service in _claude_keychain_services():
        commands = []
        if user:
            commands.append(
                ["security", "find-generic-password", "-s", service, "-a", user, "-w"]
            )
        commands.append(["security", "find-generic-password", "-s", service, "-w"])
        for command in commands:
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=5
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                append(result.stdout.strip())

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
    creds_path = os.path.expanduser(os.path.join(config_dir, ".credentials.json"))
    try:
        with open(creds_path, "r", encoding="utf-8") as handle:
            append(handle.read())
    except OSError:
        pass

    if not candidates:
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
            raise ClaudeStatusError(
                "CLAUDE_CODE_OAUTH_TOKEN 不能读取订阅限额，请运行 `claude` 完成账户登录"
            )
        raise ClaudeStatusError("未找到 Claude 登录凭据，请先运行 `claude` 登录")
    return candidates


def fetch_claude_usage(
    timeout: float, proxy_url: Optional[str] = None
) -> dict[str, Any]:
    """Query Anthropic's (undocumented) `/api/oauth/usage` endpoint for the
    account's real Session (5h) / Weekly (7d) rate-limit percentages — the
    same kind of data Codex exposes officially via `account/rateLimits/read`,
    but which Claude Code has no public API for. Reuses the OAuth access
    token Claude Code itself stores locally; read-only, and no token refresh
    is attempted, so an expired token surfaces as an error asking the user
    to re-run `claude` rather than being silently renewed.
    """
    candidates = _read_claude_oauth_candidates()
    proxy = normalize_proxy_url(proxy_url)
    handlers: list[Any] = [urllib.request.HTTPSHandler(context=ssl_context())]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    for index, oauth in enumerate(candidates):
        token = str(oauth["accessToken"]).strip()
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
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and index + 1 < len(candidates):
                continue
            if exc.code in (401, 403):
                raise ClaudeStatusError(
                    "Claude 登录已过期或无权读取限额，请重新运行 `claude` 登录"
                ) from exc
            if exc.code == 429:
                retry_after = exc.headers.get("retry-after") if exc.headers else None
                suffix = f"，约 {retry_after} 秒后重试" if retry_after else ""
                raise ClaudeStatusError(f"Claude 用量查询被限流{suffix}") from exc
            raise ClaudeStatusError(f"Claude 用量查询失败（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ClaudeStatusError(f"无法连接 Claude 用量接口：{reason}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClaudeStatusError("Claude 返回了无效的 JSON") from exc
        if not isinstance(data, dict):
            raise ClaudeStatusError("Claude 返回了意料之外的数据格式")

        data["_plan"] = oauth.get("subscriptionType")
        data["_rate_limit_tier"] = oauth.get("rateLimitTier")
        return data

    raise ClaudeStatusError("Claude 登录已过期，请重新运行 `claude` 登录")


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
SEPARATOR_COLOR = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.1)
BAR_TRACK_COLOR = NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.12)

CARD_WIDTH = 320
MARGIN_X = 22
TOP_PAD = 18
BOTTOM_PAD = 20
HEADER_HEIGHT = 26
TRAY_HEIGHT = 18.0
TRAY_ICON_SIZE = 15.0
TRAY_ICON_TEXT_GAP = 3.0
TRAY_GROUP_GAP = 10.0
TRAY_FONT_SIZE = 10.0
PROXY_DEFAULTS_KEY = "CodexQuotaHTTPProxy"

ROW_HEIGHT = 18
ROW_LABEL_WIDTH = 112
ROW_PERCENT_WIDTH = 44
ROW_BAR_GAP = 10
ROW_BAR_HEIGHT = 6
RESET_HEIGHT = 14
WINDOW_GAP = 10
DAILY_CHART_HEIGHT = 58
DAILY_CHART_ANIMATION_SECONDS = 0.55


def _remaining(window: dict[str, Any]) -> int:
    try:
        used = int(window.get("usedPercent", 0))
    except (TypeError, ValueError):
        used = 0
    return max(0, min(100, 100 - used))


def _codex_windows(snapshot: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Classify Codex windows by duration, preserving slot fallback.

    Codex can temporarily return a weekly-only limit in the primary slot.
    OpenUsage therefore treats the explicit duration as authoritative.
    """
    candidates: list[tuple[str, Optional[str], dict[str, Any]]] = []
    for fallback, key in (("session", "primary"), ("weekly", "secondary")):
        value = snapshot.get(key)
        if isinstance(value, dict):
            duration = value.get("windowDurationMins")
            if duration == 5 * 60:
                exact = "session"
            elif duration == 7 * 24 * 60:
                exact = "weekly"
            else:
                exact = None
            candidates.append((fallback, exact, value))

    rows: list[tuple[str, dict[str, Any]]] = []
    for wanted, label in (("session", "会话（5 小时）"), ("weekly", "每周")):
        match = next((window for _, exact, window in candidates if exact == wanted), None)
        if match is None:
            match = next(
                (
                    window
                    for fallback, exact, window in candidates
                    if exact is None and fallback == wanted
                ),
                None,
            )
        if match is not None:
            rows.append((label, match))
    return rows


def _claude_windows(usage: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []

    def append(label: str, value: Any) -> None:
        if isinstance(value, dict) and isinstance(value.get("utilization"), (int, float)):
            rows.append((label, value))

    append("会话（5 小时）", usage.get("five_hour"))
    append("每周", usage.get("seven_day"))
    append("Sonnet 每周", usage.get("seven_day_sonnet"))

    limits = usage.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
                continue
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            display_name = model.get("display_name") if isinstance(model, dict) else None
            percent = entry.get("percent")
            if display_name == "Fable" and isinstance(percent, (int, float)):
                append(
                    "Fable 每周",
                    {"utilization": percent, "resets_at": entry.get("resets_at")},
                )
                break
    return rows


def _format_claude_plan(usage: dict[str, Any]) -> Optional[str]:
    raw = usage.get("_plan")
    if not isinstance(raw, str) or not raw.strip():
        return None
    plan = raw.strip().replace("_", " ").title()
    tier = usage.get("_rate_limit_tier")
    if isinstance(tier, str):
        match = re.search(r"\d+x", tier, re.IGNORECASE)
        if match:
            plan += f" {match.group(0)}"
    return plan


def _format_token_count(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B Token"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M Token"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K Token"
    return f"{value} Token"


def _short_reset(timestamp: Any) -> str:
    if not isinstance(timestamp, int):
        return "重置时间不可用"
    now = datetime.now().astimezone()
    reset = datetime.fromtimestamp(timestamp).astimezone()
    seconds = max(0, timestamp - int(now.timestamp()))
    if seconds < 60:
        return "不到 1 分钟后重置"
    elif seconds < 3600:
        return f"{seconds // 60} 分钟后重置"
    elif reset.date() == now.date():
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟后重置"
    elif reset.date() == now.date() + timedelta(days=1):
        return f"明天 {reset:%H:%M} 重置"
    return f"{reset:%m-%d %H:%M} 重置"


def _tooltip_part(name: str, percent: Optional[int], failed: bool) -> str:
    if failed:
        return f"{name} 查询失败"
    if percent is None:
        return f"{name} —"
    return f"{name} {percent}%"


def _bar_color(remaining: int) -> "NSColor":
    return ACCENT if remaining > 50 else (WARN if remaining > 20 else DANGER)


def _tray_value(percent: Optional[int], error: bool) -> str:
    if error:
        return "!"
    if percent is None:
        return "—"
    return f"{max(0, min(100, percent))}%"


def _tray_image(
    codex: Optional[int] = None,
    claude: Optional[int] = None,
    codex_error: bool = False,
    claude_error: bool = False,
) -> "NSImage":
    """Draw two OpenUsage-style provider groups: mark followed by value."""
    font = NSFont.monospacedDigitSystemFontOfSize_weight_(
        TRAY_FONT_SIZE, NSFontWeightSemibold
    )
    attributes = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.blackColor(),
    }
    groups = [
        ("codex", NSString.stringWithString_(_tray_value(codex, codex_error))),
        ("claude", NSString.stringWithString_(_tray_value(claude, claude_error))),
    ]
    text_sizes = [text.sizeWithAttributes_(attributes) for _, text in groups]
    group_widths = [
        TRAY_ICON_SIZE + TRAY_ICON_TEXT_GAP + size.width for size in text_sizes
    ]
    width = math.ceil(sum(group_widths) + TRAY_GROUP_GAP)

    def draw(_rect) -> bool:
        x = 0.0
        for index, ((provider, text), text_size) in enumerate(zip(groups, text_sizes)):
            icon = _provider_icon(provider)
            if icon is not None:
                icon.drawInRect_(
                    NSMakeRect(
                        x,
                        (TRAY_HEIGHT - TRAY_ICON_SIZE) / 2,
                        TRAY_ICON_SIZE,
                        TRAY_ICON_SIZE,
                    )
                )
            x += TRAY_ICON_SIZE + TRAY_ICON_TEXT_GAP
            text.drawAtPoint_withAttributes_(
                NSMakePoint(x, (TRAY_HEIGHT - text_size.height) / 2), attributes
            )
            x += text_size.width
            if index == 0:
                x += TRAY_GROUP_GAP
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(
        NSMakeSize(width, TRAY_HEIGHT), False, draw
    )
    image.setTemplate_(True)
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


class _DailyTokenChart(_FlippedView):
    """Scrollable daily token chart showing seven days at a time."""

    visible_bar_count = 7

    def maximumScrollOffset(self) -> float:
        points = getattr(self, "points", [])
        if len(points) <= self.visible_bar_count:
            return 0.0
        slot_width = self.bounds().size.width / self.visible_bar_count
        return (len(points) - self.visible_bar_count) * slot_width

    def showLatest(self) -> None:
        self.scroll_offset = self.maximumScrollOffset()
        self.updateToolTips()
        self.setNeedsDisplay_(True)

    def updateToolTips(self) -> None:
        self.removeAllToolTips()
        self.tooltip_points = {}
        points = getattr(self, "points", [])
        if not points or self.bounds().size.width <= 0:
            return
        width = self.bounds().size.width
        slot_width = width / self.visible_bar_count
        scroll_offset = getattr(self, "scroll_offset", 0.0)
        for index, point in enumerate(points):
            slot_x = index * slot_width - scroll_offset
            left = max(0.0, slot_x)
            right = min(width, slot_x + slot_width)
            if right <= left:
                continue
            tag = self.addToolTipRect_owner_userData_(
                NSMakeRect(left, 0, right - left, self.bounds().size.height),
                self,
                None,
            )
            self.tooltip_points[int(tag)] = point

    def view_stringForToolTip_point_userData_(
        self, view, tag, point, user_data
    ) -> str:
        usage = getattr(self, "tooltip_points", {}).get(int(tag))
        if usage is None:
            return ""
        return f"{usage.day:%Y-%m-%d}  {_format_token_count(usage.total_tokens)}"

    def scrollWheel_(self, event) -> None:
        delta = event.scrollingDeltaX()
        if abs(delta) < 0.01:
            objc.super(_DailyTokenChart, self).scrollWheel_(event)
            return
        current = getattr(self, "scroll_offset", 0.0)
        self.scroll_offset = max(
            0.0, min(self.maximumScrollOffset(), current + delta)
        )
        self.updateToolTips()
        self.setNeedsDisplay_(True)

    def startAnimation(self) -> None:
        timer = getattr(self, "animation_timer", None)
        if timer is not None:
            timer.invalidate()
        self.animation_progress = 0.0
        self.animation_started_at = time.monotonic()
        self.animation_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 60.0, self, "animationTimerFired:", None, True
            )
        )
        self.setNeedsDisplay_(True)

    def animationTimerFired_(self, timer) -> None:
        elapsed = time.monotonic() - self.animation_started_at
        self.animation_progress = min(
            1.0, elapsed / DAILY_CHART_ANIMATION_SECONDS
        )
        self.setNeedsDisplay_(True)
        if self.animation_progress >= 1.0:
            timer.invalidate()
            self.animation_timer = None

    def drawRect_(self, rect) -> None:
        points = getattr(self, "points", [])
        if not points:
            return

        bounds = self.bounds()
        label_height = 15.0
        chart_top = 3.0
        chart_height = max(1.0, bounds.size.height - label_height - chart_top - 3.0)
        slot_width = bounds.size.width / self.visible_bar_count
        bar_width = min(18.0, max(6.0, slot_width - 9.0))
        scroll_offset = getattr(self, "scroll_offset", 0.0)
        first_visible = max(0, int(scroll_offset // slot_width))
        visible_points = points[
            first_visible : min(len(points), first_visible + self.visible_bar_count + 1)
        ]
        maximum = max((point.total_tokens for point in visible_points), default=0)
        animation_progress = getattr(self, "animation_progress", 1.0)
        label_font = NSFont.monospacedDigitSystemFontOfSize_weight_(9, NSFontWeightRegular)
        label_attributes = {
            NSFontAttributeName: label_font,
            NSForegroundColorAttributeName: MUTED_TEXT,
        }

        for index, point in enumerate(points):
            slot_x = index * slot_width - scroll_offset
            if slot_x + slot_width < 0 or slot_x > bounds.size.width:
                continue
            x = slot_x + (slot_width - bar_width) / 2
            track_rect = NSMakeRect(x, chart_top, bar_width, chart_height)
            radius = min(3.0, bar_width / 2)
            track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                track_rect, radius, radius
            )
            BAR_TRACK_COLOR.setFill()
            track.fill()

            if point.total_tokens > 0 and maximum > 0:
                # Stagger the bars from left to right, then ease each one out.
                bar_progress = max(
                    0.0,
                    min(
                        1.0,
                        animation_progress * 1.3
                        - max(0, index - first_visible) * 0.05,
                    ),
                )
                eased_progress = 1.0 - (1.0 - bar_progress) ** 3
                target_height = chart_height * point.total_tokens / maximum
                height = max(1.0, target_height * eased_progress)
                fill_rect = NSMakeRect(
                    x, chart_top + chart_height - height, bar_width, height
                )
                fill_radius = min(radius, height / 2)
                fill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    fill_rect, fill_radius, fill_radius
                )
                color = ACCENT.colorWithAlphaComponent_(
                    1.0 if index == len(points) - 1 else 0.62
                )
                color.setFill()
                fill.fill()

            if index == len(points) - 1:
                day_label = "今天"
            elif point.day.day == 1:
                day_label = f"{point.day.month}/1"
            else:
                day_label = str(point.day.day)
            text = NSString.stringWithString_(day_label)
            text_size = text.sizeWithAttributes_(label_attributes)
            text.drawAtPoint_withAttributes_(
                NSMakePoint(
                    slot_x + (slot_width - text_size.width) / 2,
                    chart_top + chart_height + 3.0,
                ),
                label_attributes,
            )


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

    def add_row(self, items: list[tuple[Any, float, float, float]], height: float) -> None:
        """Place several views side by side on one line.

        Each item is (view, x offset within the column, width, view height);
        shorter views are centred vertically against the row height.
        """
        for view, dx, width, view_height in items:
            dy = (height - view_height) / 2
            view.setFrame_(NSMakeRect(self.x + dx, self.y + dy, width, view_height))
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
        self.header_settings_button = None
        self._fetching = False
        self._refresh_timer = None
        self._last_close_time: Optional[float] = None
        saved_proxy = NSUserDefaults.standardUserDefaults().stringForKey_(PROXY_DEFAULTS_KEY)
        self.proxy_url = str(saved_proxy).strip() if saved_proxy else None
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
        button.setToolTip_("Codex · Claude 额度")
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

        # dot = _make_label("●", 17, NSFontWeightRegular, ACCENT)
        # dot.setFrame_(NSMakeRect(MARGIN_X, TOP_PAD, 16, HEADER_HEIGHT))
        # effect_view.addSubview_(dot)

        title = _make_label("用量", 15, NSFontWeightSemibold, PRIMARY_TEXT)
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

        settings_button = _make_icon_button("⚙︎", self, "settingsClicked:")
        settings_button.setFrame_(
            NSMakeRect(CARD_WIDTH - MARGIN_X - 84, TOP_PAD - 2, 24, HEADER_HEIGHT)
        )
        effect_view.addSubview_(settings_button)
        self.header_settings_button = settings_button

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
            "退出", "quitClicked:", ""
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

    def settingsClicked_(self, sender) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_("HTTP 代理")
        alert.setInformativeText_(
            "代理将同时用于 Codex 和 Claude 的网络请求。清空后恢复系统默认设置。"
        )
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 24))
        field.setPlaceholderString_("http://127.0.0.1:7890")
        field.setStringValue_(self.proxy_url or "")
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("保存")
        alert.addButtonWithTitle_("取消")
        alert.addButtonWithTitle_("清除")

        response = alert.runModal()
        if response == NSAlertThirdButtonReturn:
            self._save_proxy(None)
            return
        if response != NSAlertFirstButtonReturn:
            return
        try:
            proxy = normalize_proxy_url(field.stringValue())
        except QuotaError as exc:
            error_alert = NSAlert.alloc().init()
            error_alert.setMessageText_("代理地址无效")
            error_alert.setInformativeText_(str(exc))
            error_alert.addButtonWithTitle_("确定")
            error_alert.runModal()
            return
        self._save_proxy(proxy)

    def _save_proxy(self, proxy: Optional[str]) -> None:
        defaults = NSUserDefaults.standardUserDefaults()
        if proxy:
            defaults.setObject_forKey_(proxy, PROXY_DEFAULTS_KEY)
        else:
            defaults.removeObjectForKey_(PROXY_DEFAULTS_KEY)
        self.proxy_url = proxy
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
            codex_payload = fetch_quota(self.timeout, self.proxy_url)
        except (QuotaError, OSError) as exc:
            codex_error = str(exc)

        claude_usage: Optional[dict[str, Any]] = None
        claude_error: Optional[str] = None
        try:
            claude_usage = fetch_claude_usage(self.timeout, self.proxy_url)
        except (ClaudeStatusError, OSError) as exc:
            claude_error = str(exc)

        token_usage: Optional[dict[str, dict[str, Any]]] = None
        try:
            token_usage = scan_token_usage()
        except OSError:
            # Live quotas should remain usable if a local log directory becomes
            # unreadable while it is being scanned.
            token_usage = None

        AppHelper.callAfter(
            self._on_fetch_done,
            codex_payload,
            codex_error,
            claude_usage,
            claude_error,
            token_usage,
        )

    def _on_fetch_done(
        self,
        codex_payload: Optional[dict[str, Any]],
        codex_error: Optional[str],
        claude_usage: Optional[dict[str, Any]],
        claude_error: Optional[str],
        token_usage: Optional[dict[str, dict[str, Any]]],
    ) -> None:
        self._render_status(
            codex_payload, codex_error, claude_usage, claude_error, token_usage
        )
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
            "正在查询剩余额度…", 12, NSFontWeightRegular, MUTED_TEXT
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
        token_usage: Optional[dict[str, dict[str, Any]]],
    ) -> None:
        layout = _Layout(MARGIN_X, CARD_WIDTH - 2 * MARGIN_X, 0)

        if codex_error:
            tray_percent: Optional[int] = None
            self._layout_provider_heading(layout, "codex", "Codex")
            layout.space(6)
            label = _make_label(
                f"查询失败\n{codex_error}\n请运行 codex login status 检查登录状态。",
                12,
                NSFontWeightRegular,
                ERROR_TEXT,
            )
            label.setAlignment_(NSTextAlignmentCenter)
            label.setPreferredMaxLayoutWidth_(CARD_WIDTH - 2 * MARGIN_X)
            label.cell().setWraps_(True)
            layout.add(label, 72)
        else:
            tray_percent = self._layout_codex_payload(layout, codex_payload)

        layout.space(14)
        self._layout_token_usage(layout, token_usage.get("codex") if token_usage else None)

        layout.space(18)
        layout.add(_Separator.alloc().init(), 1)
        layout.space(16)
        claude_percent = self._layout_claude_usage(layout, claude_usage, claude_error)

        layout.space(14)
        self._layout_token_usage(layout, token_usage.get("claude") if token_usage else None)

        layout.space(18)
        updated = _make_label(
            f"更新于 {datetime.now().astimezone():%H:%M}",
            11,
            NSFontWeightRegular,
            MUTED_TEXT,
        )
        updated.setAlignment_(NSTextAlignmentRight)
        layout.add(updated, 16)

        self._clear_body()
        self._finish_layout(layout)

        self._update_status_item(
            tray_percent, bool(codex_error), claude_percent, bool(claude_error)
        )

    def _update_status_item(
        self,
        codex_percent: Optional[int],
        codex_failed: bool,
        claude_percent: Optional[int],
        claude_failed: bool,
    ) -> None:
        button = self.status_item.button()
        button.setImage_(
            _tray_image(codex_percent, claude_percent, codex_failed, claude_failed)
        )
        button.setToolTip_(
            _tooltip_part("Codex", codex_percent, codex_failed)
            + " · "
            + _tooltip_part("Claude", claude_percent, claude_failed)
        )

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
            self._layout_provider_heading(layout, "codex", heading)

            windows = _codex_windows(snapshot)
            rendered = 0
            for label_text, window in windows:
                layout.space(WINDOW_GAP)
                remaining = _remaining(window)
                if tray_percent is None:
                    tray_percent = remaining
                self._layout_window(layout, label_text, window, remaining)
                rendered += 1

            if not rendered:
                layout.space(8)
                layout.add(
                    _make_label("暂无限额窗口", 11, NSFontWeightRegular, MUTED_TEXT),
                    16,
                )

        reset_credits = payload.get("rateLimitResetCredits")
        if isinstance(reset_credits, dict):
            available = reset_credits.get("availableCount")
            if isinstance(available, (int, float)) and not isinstance(available, bool):
                layout.space(10)
                label = _make_label("可用限额重置", 11, NSFontWeightRegular, SUBTLE_TEXT)
                value = _make_label(
                    f"{max(0, int(available))} 次",
                    11,
                    NSFontWeightSemibold,
                    SECONDARY_TEXT,
                    NSTextAlignmentRight,
                )
                layout.add_row(
                    [
                        (label, 0, ROW_LABEL_WIDTH, 16),
                        (value, ROW_LABEL_WIDTH, layout.width - ROW_LABEL_WIDTH, 16),
                    ],
                    18,
                )

        return tray_percent

    def _layout_token_usage(
        self, layout: "_Layout", periods: Optional[dict[str, Any]]
    ) -> None:
        layout.add(_make_label("本地 Token 用量", 11, NSFontWeightSemibold, MUTED_TEXT), 16)
        if periods is None:
            layout.add(_make_label("Token 日志不可用", 11, NSFontWeightRegular, MUTED_TEXT), 16)
            return

        rows = [
            ("今天", periods.get("today")),
            ("昨天", periods.get("yesterday")),
            ("最近 30 天", periods.get("last30")),
        ]
        if not any(total and total.total_tokens > 0 for _, total in rows):
            layout.add(
                _make_label("最近 30 天暂无用量", 11, NSFontWeightRegular, MUTED_TEXT),
                16,
            )
            return

        for label_text, total in rows:
            count = total.total_tokens if total else 0
            calls = total.calls if total else 0
            label = _make_label(label_text, 11, NSFontWeightRegular, SUBTLE_TEXT)
            value = _make_label(
                f"{calls} 次调用  ·  {_format_token_count(count)}",
                11,
                NSFontWeightSemibold,
                SECONDARY_TEXT,
                NSTextAlignmentRight,
            )
            layout.add_row(
                [
                    (label, 0, ROW_LABEL_WIDTH, 16),
                    (value, ROW_LABEL_WIDTH, layout.width - ROW_LABEL_WIDTH, 16),
                ],
                18,
            )

        daily = periods.get("daily")
        if isinstance(daily, list) and daily:
            chart = _DailyTokenChart.alloc().init()
            chart.points = [
                point for point in daily if isinstance(point, DailyTokenUsage)
            ]
            if chart.points:
                hint = _make_label(
                    "← 左右滑动查看最近 30 天 →",
                    9,
                    NSFontWeightRegular,
                    MUTED_TEXT,
                    NSTextAlignmentRight,
                )
                layout.space(5)
                layout.add(hint, 13)
                layout.add(chart, DAILY_CHART_HEIGHT)
                chart.showLatest()
                chart.startAnimation()

    def _layout_claude_usage(
        self,
        layout: "_Layout",
        usage: Optional[dict[str, Any]],
        error: Optional[str],
    ) -> Optional[int]:
        if error:
            self._layout_provider_heading(layout, "claude", "Claude")
            layout.space(6)
            label = _make_label(error, 11, NSFontWeightRegular, ERROR_TEXT)
            label.setPreferredMaxLayoutWidth_(layout.width)
            label.cell().setWraps_(True)
            layout.add(label, 32)
            return None
        if usage is None:
            self._layout_provider_heading(layout, "claude", "Claude")
            layout.space(6)
            layout.add(_make_label("正在查询…", 11, NSFontWeightRegular, MUTED_TEXT), 16)
            return None

        plan = _format_claude_plan(usage)
        heading = "Claude" + (f"  ·  {plan}" if plan else "")
        self._layout_provider_heading(layout, "claude", heading)

        windows = _claude_windows(usage)
        tray_percent: Optional[int] = None
        rendered = 0
        for label_text, window_data in windows:
            layout.space(WINDOW_GAP)
            window = {
                "usedPercent": window_data.get("utilization", 0),
                "resetsAt": _parse_claude_reset(window_data.get("resets_at")),
            }
            remaining = _remaining(window)
            if tray_percent is None:
                tray_percent = remaining
            self._layout_window(layout, label_text, window, remaining)
            rendered += 1

        extra = usage.get("extra_usage")
        if isinstance(extra, dict) and extra.get("is_enabled") is True:
            used = extra.get("used_credits")
            limit = extra.get("monthly_limit")
            if isinstance(used, (int, float)):
                layout.space(WINDOW_GAP)
                self._layout_extra_usage(layout, float(used), limit)
                rendered += 1

        if not rendered:
            layout.space(8)
            layout.add(
                _make_label("暂无限额窗口", 11, NSFontWeightRegular, MUTED_TEXT),
                16,
            )

        return tray_percent

    def _layout_provider_heading(
        self, layout: "_Layout", provider: str, heading: str
    ) -> None:
        label = _make_label(heading, 13, NSFontWeightSemibold, SECONDARY_TEXT)
        icon = _provider_icon(provider)
        if icon is None:
            layout.add(label, 18)
            return
        icon_view = NSImageView.imageViewWithImage_(icon)
        icon_view.setContentTintColor_(SECONDARY_TEXT)
        layout.add_row(
            [(icon_view, 0, 16, 16), (label, 23, layout.width - 23, 18)],
            18,
        )

    def _layout_extra_usage(self, layout: "_Layout", used_cents: float, limit: Any) -> None:
        """Render Claude's optional monthly extra-usage spend in dollars."""
        name = _make_label("额外用量", 12, NSFontWeightSemibold, SUBTLE_TEXT)
        if isinstance(limit, (int, float)) and limit > 0:
            detail = f"${used_cents / 100:.2f} / ${float(limit) / 100:.2f}"
            fraction = max(0.0, min(1.0, used_cents / float(limit)))
        else:
            detail = f"${used_cents / 100:.2f}"
            fraction = 0.0

        value = _make_label(detail, 12, NSFontWeightSemibold, PRIMARY_TEXT)
        value.setAlignment_(NSTextAlignmentRight)
        layout.add_row(
            [
                (name, 0, ROW_LABEL_WIDTH, 16),
                (value, ROW_LABEL_WIDTH, layout.width - ROW_LABEL_WIDTH, 16),
            ],
            ROW_HEIGHT,
        )
        if fraction > 0:
            layout.space(3)
            bar = _RingBar.alloc().init()
            bar.fraction = fraction
            bar.bar_color = _bar_color(max(0, min(100, round((1 - fraction) * 100))))
            layout.add(bar, ROW_BAR_HEIGHT)

    def _layout_window(
        self, layout: "_Layout", label_text: str, window: dict[str, Any], remaining: int
    ) -> None:
        """One line per window: name, inline bar, percentage — then the reset
        time as a quieter second line."""
        name = _make_label(label_text, 12, NSFontWeightSemibold, SUBTLE_TEXT)

        bar = _RingBar.alloc().init()
        bar.fraction = remaining / 100.0
        bar.bar_color = _bar_color(remaining)

        percent = _make_label(
            f"{remaining}%", 13, NSFontWeightBold, PRIMARY_TEXT, NSTextAlignmentRight
        )

        bar_x = ROW_LABEL_WIDTH + ROW_BAR_GAP
        bar_width = layout.width - ROW_PERCENT_WIDTH - ROW_BAR_GAP - bar_x
        layout.add_row(
            [
                (name, 0, ROW_LABEL_WIDTH, 16),
                (bar, bar_x, bar_width, ROW_BAR_HEIGHT),
                (percent, layout.width - ROW_PERCENT_WIDTH, ROW_PERCENT_WIDTH, 16),
            ],
            ROW_HEIGHT,
        )
        layout.space(3)
        layout.add(
            _make_label(_short_reset(window.get("resetsAt")), 11, NSFontWeightRegular, MUTED_TEXT),
            RESET_HEIGHT,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="以 macOS 菜单栏原生 popover 显示 Codex 剩余额度")
    parser.add_argument(
        "--refresh", type=float, default=900, help="刷新间隔秒数（默认 900，即 15 分钟）"
    )
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
