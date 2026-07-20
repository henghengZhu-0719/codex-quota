#!/usr/bin/env python3
"""Display Codex allowance using the OAuth login stored by Codex."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from codex_usage import CodexUsageError, fetch_codex_usage


VERSION = "0.1.0"


class QuotaError(RuntimeError):
    """A user-facing quota query error."""


def normalize_proxy_url(value: Any) -> Optional[str]:
    """Validate an HTTP(S) proxy URL at the user-input boundary."""
    if value is None or not str(value).strip():
        return None
    proxy = str(value).strip()
    try:
        parsed = urlsplit(proxy)
        _ = parsed.port
    except ValueError as exc:
        raise QuotaError("代理地址中的端口无效") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise QuotaError("代理地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise QuotaError("代理地址缺少主机名")
    if parsed.query or parsed.fragment:
        raise QuotaError("代理地址不能包含查询参数或片段")
    return proxy


def fetch_quota(timeout: float, proxy_url: Optional[str] = None) -> dict[str, Any]:
    try:
        return fetch_codex_usage(timeout, normalize_proxy_url(proxy_url))
    except CodexUsageError as exc:
        raise QuotaError(str(exc)) from exc


def _format_duration(minutes: Any) -> str:
    if not isinstance(minutes, int):
        return "限额窗口"
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)} 天窗口"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时窗口"
    return f"{minutes} 分钟窗口"


def _format_reset(timestamp: Any) -> str:
    if not isinstance(timestamp, int):
        return "未知"
    reset = datetime.fromtimestamp(timestamp).astimezone()
    seconds = max(0, timestamp - int(time.time()))
    if seconds < 60:
        relative = "不到 1 分钟"
    elif seconds < 3600:
        relative = f"约 {seconds // 60} 分钟"
    elif seconds < 86400:
        relative = f"约 {seconds // 3600} 小时 {seconds % 3600 // 60} 分钟"
    else:
        relative = f"约 {seconds // 86400} 天 {seconds % 86400 // 3600} 小时"
    return f"{reset:%Y-%m-%d %H:%M:%S %Z}（{relative}后）"


def _color(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _bar(remaining: int, enabled: bool, width: int = 24) -> str:
    remaining = max(0, min(100, remaining))
    filled = round(width * remaining / 100)
    code = "32" if remaining > 50 else "33" if remaining > 20 else "31"
    return _color("█" * filled, code, enabled) + "░" * (width - filled)


def _snapshots(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    buckets = payload.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        return [(str(key), value) for key, value in buckets.items() if isinstance(value, dict)]
    snapshot = payload.get("rateLimits")
    return [("codex", snapshot)] if isinstance(snapshot, dict) else []


def render(payload: dict[str, Any], color: bool) -> str:
    rows: list[str] = [_color("Codex 剩余额度", "1;36", color)]
    snapshots = _snapshots(payload)
    if not snapshots:
        return "\n".join(rows + ["未返回限额信息。请确认当前使用 ChatGPT 账户登录 Codex。"])

    for index, (bucket_id, snapshot) in enumerate(snapshots):
        if index:
            rows.append("")
        name = snapshot.get("limitName") or bucket_id
        plan = snapshot.get("planType")
        heading = str(name) + (f" · {plan}" if plan else "")
        rows.append(_color(heading, "1", color))
        for label, key in (("短周期", "primary"), ("长周期", "secondary")):
            window = snapshot.get(key)
            if not isinstance(window, dict):
                continue
            used = int(window.get("usedPercent", 0))
            remaining = max(0, min(100, 100 - used))
            rows.append(
                f"  {label} {_format_duration(window.get('windowDurationMins')):<10} "
                f"{_bar(remaining, color)}  剩余 {remaining:>3}%"
            )
            rows.append(f"       重置：{_format_reset(window.get('resetsAt'))}")

        individual = snapshot.get("individualLimit")
        if isinstance(individual, dict):
            rows.append(
                f"  个人用量  {individual.get('used', '?')} / {individual.get('limit', '?')}"
                f"（剩余 {individual.get('remainingPercent', '?')}%）"
            )
        credits = snapshot.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited"):
                rows.append("  Credits：无限")
            elif credits.get("hasCredits"):
                rows.append(f"  Credits 余额：{credits.get('balance', '未知')}")
        reached = snapshot.get("rateLimitReachedType")
        if reached:
            rows.append(_color(f"  当前受限：{reached}", "31", color))

    reset_credits = payload.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict) and reset_credits.get("availableCount", 0):
        rows.append(f"\n可用限额重置次数：{reset_credits['availableCount']}")
    rows.append(f"\n查询时间：{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}")
    return "\n".join(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询当前 Codex 账户的剩余额度")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON，方便脚本处理")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="按指定秒数持续刷新")
    parser.add_argument("--timeout", type=float, default=15, help="单次查询超时秒数（默认 15）")
    parser.add_argument("--proxy", help="HTTP(S) 代理地址，例如 http://127.0.0.1:7890")
    parser.add_argument("--no-color", action="store_true", help="禁用 ANSI 颜色")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()
    if args.watch is not None and args.watch < 1:
        parser.error("--watch 必须大于或等于 1 秒")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def main() -> int:
    args = parse_args()
    use_color = not args.no_color and sys.stdout.isatty() and not os.getenv("NO_COLOR")
    try:
        while True:
            payload = fetch_quota(args.timeout, args.proxy)
            if args.watch is not None and sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(render(payload, use_color))
            if args.watch is None:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130
    except QuotaError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("提示：先运行 `codex login status` 检查登录状态。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
