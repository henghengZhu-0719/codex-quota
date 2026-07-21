"""Local Codex and Claude token accounting from their JSONL session logs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


@dataclass(frozen=True)
class TokenEvent:
    timestamp: datetime
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    identity: tuple[Any, ...] = ()
    is_sidechain: bool = False


@dataclass
class TokenTotals:
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def add(self, event: TokenEvent) -> None:
        self.input_tokens += event.input_tokens
        self.cached_tokens += event.cached_tokens
        self.cache_write_tokens += event.cache_write_tokens
        self.output_tokens += event.output_tokens
        self.reasoning_tokens += event.reasoning_tokens
        self.total_tokens += event.total_tokens


@dataclass(frozen=True)
class DailyTokenUsage:
    day: date
    total_tokens: int = 0


_FileSignature = tuple[int, int]
_FILE_CACHE: dict[tuple[str, str], tuple[_FileSignature, list[TokenEvent]]] = {}


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _raw_codex_usage(value: Any) -> Optional[dict[str, int]]:
    if not isinstance(value, dict):
        return None

    def first(*keys: str) -> int:
        for key in keys:
            if key in value:
                return _number(value[key])
        return 0

    input_tokens = first("input_tokens", "prompt_tokens", "input")
    cached = first("cached_input_tokens", "cache_read_input_tokens", "cached_tokens")
    output = first("output_tokens", "completion_tokens", "output")
    reasoning = first("reasoning_output_tokens", "reasoning_tokens")
    total = first("total_tokens")
    if total <= 0:
        total = input_tokens + output + reasoning
    return {
        "input": input_tokens,
        "cached": min(cached, input_tokens),
        "output": output,
        "reasoning": reasoning,
        "total": total,
    }


def _codex_delta(current: dict[str, int], previous: Optional[dict[str, int]]) -> dict[str, int]:
    if previous is None:
        return current
    return {key: max(0, value - previous.get(key, 0)) for key, value in current.items()}


def _has_value(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _is_child_codex_session(payload: dict[str, Any]) -> bool:
    if _has_value(payload.get("forked_from_id")) or _has_value(payload.get("parent_thread_id")):
        return True
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and _has_value(source.get("subagent"))


def parse_codex_file(path: Path) -> list[TokenEvent]:
    events: list[TokenEvent] = []
    previous: Optional[dict[str, int]] = None
    current_model = "gpt-5"
    saw_session_meta = False
    replay_gate: Optional[float] = None
    replay_without_time = False

    try:
        lines = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return events

    with lines:
        for raw_line in lines:
            if not any(
                marker in raw_line
                for marker in (
                    '"type":"turn_context"',
                    '"type": "turn_context"',
                    '"type":"session_meta"',
                    '"type": "session_meta"',
                    '"type":"token_count"',
                    '"type": "token_count"',
                    '"type":"task_started"',
                    '"type": "task_started"',
                )
            ):
                continue
            try:
                item = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            record_type = item.get("type")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            if record_type == "turn_context":
                model = payload.get("model") or payload.get("model_name")
                if isinstance(model, str) and model.strip():
                    current_model = model.strip()
                continue

            if record_type == "session_meta" and not saw_session_meta:
                saw_session_meta = True
                if _is_child_codex_session(payload):
                    created = _parse_time(item.get("timestamp"))
                    if created is None:
                        replay_without_time = True
                    else:
                        replay_gate = created.timestamp()
                continue

            if record_type != "event_msg":
                continue
            subtype = payload.get("type")
            if subtype == "task_started" and (replay_gate is not None or replay_without_time):
                started_at = payload.get("started_at")
                line_time = _parse_time(item.get("timestamp"))
                if isinstance(started_at, (int, float)):
                    threshold = replay_gate
                    if threshold is None and line_time is not None:
                        threshold = int(line_time.timestamp())
                    if threshold is not None and float(started_at) >= threshold:
                        replay_gate = None
                        replay_without_time = False
                continue
            if subtype != "token_count":
                continue

            timestamp = _parse_time(item.get("timestamp"))
            info = payload.get("info")
            if timestamp is None or not isinstance(info, dict):
                continue
            totals = _raw_codex_usage(info.get("total_token_usage"))
            if replay_gate is not None or replay_without_time:
                if totals is not None:
                    previous = totals
                continue
            if totals is not None and totals == previous:
                continue

            last = _raw_codex_usage(info.get("last_token_usage"))
            usage = last if last is not None else (_codex_delta(totals, previous) if totals else None)
            if totals is not None:
                previous = totals
            if usage is None or usage["total"] <= 0:
                continue

            model = payload.get("model") or info.get("model") or current_model
            identity = (
                timestamp.isoformat(),
                str(model),
                usage["input"],
                usage["cached"],
                usage["output"],
                usage["reasoning"],
                usage["total"],
            )
            events.append(
                TokenEvent(
                    timestamp=timestamp,
                    input_tokens=usage["input"],
                    cached_tokens=usage["cached"],
                    output_tokens=usage["output"],
                    reasoning_tokens=usage["reasoning"],
                    total_tokens=usage["total"],
                    identity=identity,
                )
            )
    return events


def _claude_event(item: dict[str, Any]) -> Optional[TokenEvent]:
    timestamp = _parse_time(item.get("timestamp"))
    message = item.get("message")
    if timestamp is None or not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    if not isinstance(usage.get("input_tokens"), (int, float)):
        return None
    if not isinstance(usage.get("output_tokens"), (int, float)):
        return None

    input_tokens = _number(usage.get("input_tokens"))
    output_tokens = _number(usage.get("output_tokens"))
    cache_read = _number(usage.get("cache_read_input_tokens"))
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        cache_write = _number(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_write += _number(cache_creation.get("ephemeral_1h_input_tokens"))
    else:
        cache_write = _number(usage.get("cache_creation_input_tokens"))
    total = input_tokens + cache_read + cache_write + output_tokens
    if total <= 0:
        return None

    message_id = message.get("id")
    request_id = item.get("requestId")
    if isinstance(message_id, str) and message_id:
        identity = ("message", message_id, request_id if isinstance(request_id, str) else None)
    else:
        identity = (
            "event",
            timestamp.isoformat(),
            message.get("model"),
            input_tokens,
            cache_read,
            cache_write,
            output_tokens,
        )
    return TokenEvent(
        timestamp=timestamp,
        input_tokens=input_tokens,
        cached_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        total_tokens=total,
        identity=identity,
        is_sidechain=item.get("isSidechain") is True,
    )


def parse_claude_file(path: Path) -> list[TokenEvent]:
    events: list[TokenEvent] = []
    try:
        lines = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return events
    with lines:
        for raw_line in lines:
            if '"usage"' not in raw_line:
                continue
            try:
                item = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(item, dict):
                event = _claude_event(item)
                if event is not None:
                    events.append(event)
    return events


def _walk_jsonl(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for directory, _, names in os.walk(root, followlinks=True):
        for name in names:
            if name.endswith(".jsonl"):
                found.append(Path(directory) / name)
    return sorted(found)


def _codex_files() -> list[Path]:
    raw = os.environ.get("CODEX_HOME", "").strip()
    homes = [Path(os.path.expanduser(value.strip())) for value in raw.split(",") if value.strip()]
    if not homes:
        homes = [Path.home() / ".codex"]
    files: list[Path] = []
    for home in homes:
        sources = [path for path in (home / "sessions", home / "archived_sessions") if path.is_dir()]
        if not sources:
            sources = [home]
        seen_relative: set[str] = set()
        for source in sources:
            for path in _walk_jsonl(source):
                try:
                    relative = str(path.relative_to(source))
                except ValueError:
                    relative = str(path)
                if relative not in seen_relative:
                    seen_relative.add(relative)
                    files.append(path)
    return files


def _claude_roots() -> list[Path]:
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if raw:
        configured = [Path(os.path.expanduser(value.strip())) for value in raw.split(",") if value.strip()]
    else:
        xdg = Path(os.path.expanduser(os.environ.get("XDG_CONFIG_HOME", "~/.config")))
        configured = [xdg / "claude", Path.home() / ".claude"]
    roots: list[Path] = []
    for value in configured:
        root = value.parent if value.name == "projects" else value
        if (root / "projects").is_dir():
            roots.append(root)

    cowork = Path.home() / "Library/Application Support/Claude/local-agent-mode-sessions"
    for pattern in ("*/*/local_*/.claude", "*/*/agent/local_*/.claude"):
        roots.extend(path for path in cowork.glob(pattern) if (path / "projects").is_dir())
    return list(dict.fromkeys(path.resolve() for path in roots))


def _cached_events(path: Path, parser: Callable[[Path], list[TokenEvent]]) -> list[TokenEvent]:
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (parser.__name__, str(path.resolve()))
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    events = parser(path)
    _FILE_CACHE[key] = (signature, events)
    return events


def _dedup_claude(events: Iterable[TokenEvent]) -> list[TokenEvent]:
    chosen: dict[tuple[Any, ...], TokenEvent] = {}
    message_entries: dict[str, tuple[Any, ...]] = {}
    for event in events:
        key = event.identity
        message_id = key[1] if len(key) >= 2 and key[0] == "message" else None
        collision = key if key in chosen else None
        if collision is None and isinstance(message_id, str):
            existing_key = message_entries.get(message_id)
            if existing_key is not None and (event.is_sidechain or chosen[existing_key].is_sidechain):
                collision = existing_key
        if collision is None:
            chosen[key] = event
            if isinstance(message_id, str):
                message_entries.setdefault(message_id, key)
            continue
        existing = chosen[collision]
        should_replace = existing.is_sidechain and not event.is_sidechain
        should_replace = should_replace or (
            existing.is_sidechain == event.is_sidechain and event.total_tokens > existing.total_tokens
        )
        if should_replace:
            del chosen[collision]
            chosen[key] = event
            if isinstance(message_id, str):
                message_entries[message_id] = key
    return list(chosen.values())


def _periods(events: Iterable[TokenEvent], now: Optional[datetime] = None) -> dict[str, Any]:
    local_now = (now or datetime.now().astimezone()).astimezone()
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    first_day = today - timedelta(days=29)
    result = {"today": TokenTotals(), "yesterday": TokenTotals(), "last30": TokenTotals()}
    daily_totals = {today - timedelta(days=offset): 0 for offset in range(30)}
    for event in events:
        day = event.timestamp.astimezone().date()
        if day < first_day or day > today:
            continue
        result["last30"].add(event)
        if day in daily_totals:
            daily_totals[day] += event.total_tokens
        if day == today:
            result["today"].add(event)
        elif day == yesterday:
            result["yesterday"].add(event)
    result["daily"] = [
        DailyTokenUsage(day=day, total_tokens=daily_totals[day])
        for day in sorted(daily_totals)
    ]
    return result


def scan_token_usage(now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """Return summary totals and a seven-day daily series for both providers."""
    codex_events = [event for path in _codex_files() for event in _cached_events(path, parse_codex_file)]
    codex_unique = {event.identity: event for event in codex_events}.values()

    claude_files = [path for root in _claude_roots() for path in _walk_jsonl(root / "projects")]
    claude_events = [event for path in claude_files for event in _cached_events(path, parse_claude_file)]
    return {
        "codex": _periods(codex_unique, now),
        "claude": _periods(_dedup_claude(claude_events), now),
    }
