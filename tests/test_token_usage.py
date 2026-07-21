import json
from datetime import datetime, timezone

from token_usage import _dedup_claude, _periods, parse_claude_file, parse_codex_file


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_codex_uses_cumulative_delta_and_skips_unchanged_snapshot(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        [
            {
                "timestamp": "2026-07-20T08:00:00Z",
                "type": "session_meta",
                "payload": {"id": "root"},
            },
            {
                "timestamp": "2026-07-20T08:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 80,
                            "output_tokens": 20,
                            "total_tokens": 100,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-07-20T08:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 80,
                            "output_tokens": 20,
                            "total_tokens": 100,
                        },
                        "last_token_usage": {
                            "input_tokens": 8,
                            "output_tokens": 2,
                            "total_tokens": 10,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-07-20T08:03:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 120,
                            "output_tokens": 30,
                            "total_tokens": 150,
                        }
                    },
                },
            },
        ],
    )

    events = parse_codex_file(path)

    assert [event.total_tokens for event in events] == [100, 50]
    assert sum(event.total_tokens for event in events) == 150


def test_claude_prefers_parent_message_over_sidechain_replay(tmp_path):
    path = tmp_path / "session.jsonl"
    base = {
        "timestamp": "2026-07-20T08:00:00Z",
        "message": {
            "id": "msg-1",
            "model": "claude-sonnet-4",
            "usage": {"input_tokens": 80, "output_tokens": 20},
        },
    }
    sidechain = {
        **base,
        "requestId": "side-request",
        "isSidechain": True,
        "message": {
            **base["message"],
            "usage": {"input_tokens": 800, "output_tokens": 200},
        },
    }
    parent = {**base, "requestId": "parent-request", "isSidechain": False}
    _write_jsonl(path, [sidechain, parent])

    events = _dedup_claude(parse_claude_file(path))

    assert len(events) == 1
    assert events[0].total_tokens == 100
    assert events[0].is_sidechain is False


def test_periods_bucket_tokens_by_local_calendar_day(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            {
                "timestamp": "2026-07-20T08:00:00Z",
                "requestId": "today",
                "message": {
                    "id": "today",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 30,
                        "output_tokens": 40,
                    },
                },
            },
            {
                "timestamp": "2026-07-19T08:00:00Z",
                "requestId": "yesterday",
                "message": {
                    "id": "yesterday",
                    "usage": {"input_tokens": 25, "output_tokens": 25},
                },
            },
        ],
    )
    events = parse_claude_file(path)

    periods = _periods(events, datetime(2026, 7, 20, 12, tzinfo=timezone.utc))

    assert periods["today"].total_tokens == 100
    assert periods["today"].calls == 1
    assert periods["yesterday"].total_tokens == 50
    assert periods["yesterday"].calls == 1
    assert periods["last30"].total_tokens == 150
    assert periods["last30"].calls == 2
    assert len(periods["daily"]) == 30
    assert periods["daily"][0].day.isoformat() == "2026-06-21"
    assert [point.total_tokens for point in periods["daily"][-2:]] == [50, 100]
    assert periods["daily"][-1].day.isoformat() == "2026-07-20"
