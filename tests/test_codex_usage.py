import json
from unittest.mock import patch

import codex_usage


def test_auth_file_is_enough_without_codex_executable(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "account_id": "account",
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(codex_usage, "_auth_paths", return_value=[auth_path]), patch.object(
        codex_usage, "_load_keychain_auth", return_value=None
    ):
        candidates = codex_usage.load_auth_candidates()

    assert len(candidates) == 1
    assert candidates[0].source == "file"
    assert candidates[0].tokens["access_token"] == "access"


def test_usage_response_is_normalized_for_existing_quota_ui():
    payload = codex_usage.normalize_usage(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 25,
                    "limit_window_seconds": 7 * 24 * 60 * 60,
                    "reset_at": 2_000_000_000,
                }
            },
            "credits": {"has_credits": True, "balance": 12.5},
        },
        {},
    )

    snapshot = payload["rateLimitsByLimitId"]["codex"]
    assert snapshot["planType"] == "pro"
    assert snapshot["primary"] == {
        "usedPercent": 25.0,
        "windowDurationMins": 10_080,
        "resetsAt": 2_000_000_000,
    }
    assert snapshot["credits"]["balance"] == 12.5


def test_direct_usage_request_sends_account_header_without_launching_codex():
    state = codex_usage.AuthState(
        {
            "tokens": {
                "access_token": "not-a-jwt",
                "account_id": "account-123",
            }
        },
        "file",
    )
    captured = {}

    def fake_request(request, timeout, proxy_url):
        captured["url"] = request.full_url
        captured["account"] = request.get_header("Chatgpt-account-id")
        captured["proxy"] = proxy_url
        return (
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 18_000,
                    }
                }
            },
            {},
        )

    with patch.object(codex_usage, "load_auth_candidates", return_value=[state]), patch.object(
        codex_usage, "_request_json", side_effect=fake_request
    ), patch.object(
        codex_usage.subprocess, "run", side_effect=AssertionError("Codex must not launch")
    ):
        result = codex_usage.fetch_codex_usage(5, "http://127.0.0.1:7890")

    assert captured == {
        "url": codex_usage.USAGE_URL,
        "account": "account-123",
        "proxy": "http://127.0.0.1:7890",
    }
    assert result["rateLimits"]["primary"]["usedPercent"] == 10
