from unittest.mock import patch

import codex_quota
import codex_quota_window


def test_proxy_url_requires_http_scheme_and_host():
    assert codex_quota.normalize_proxy_url(" http://127.0.0.1:7890 ") == (
        "http://127.0.0.1:7890"
    )
    for invalid in ("socks5://127.0.0.1:1080", "http://", "http://host:bad"):
        try:
            codex_quota.normalize_proxy_url(invalid)
        except codex_quota.QuotaError:
            pass
        else:
            raise AssertionError(f"invalid proxy accepted: {invalid}")


def test_codex_app_server_receives_proxy_environment():
    captured = {}

    class StopAfterSpawn(Exception):
        pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs["env"])
        raise StopAfterSpawn

    with patch.object(codex_quota.shutil, "which", return_value="/usr/bin/codex"), patch.object(
        codex_quota.subprocess, "Popen", side_effect=fake_popen
    ):
        try:
            codex_quota.fetch_quota(1, "http://127.0.0.1:7890")
        except StopAfterSpawn:
            pass

    assert captured["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert captured["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert captured["http_proxy"] == "http://127.0.0.1:7890"
    assert captured["https_proxy"] == "http://127.0.0.1:7890"


def test_claude_request_uses_explicit_proxy_opener():
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"five_hour":{"utilization":25}}'

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return Response()

    proxy_handler = object()
    with patch.object(
        codex_quota_window, "_read_claude_oauth_candidates", return_value=[{"accessToken": "token"}]
    ), patch.object(
        codex_quota_window.urllib.request, "ProxyHandler", return_value=proxy_handler
    ) as handler, patch.object(
        codex_quota_window.urllib.request, "build_opener", return_value=Opener()
    ) as builder:
        result = codex_quota_window.fetch_claude_usage(3, "http://127.0.0.1:7890")

    handler.assert_called_once_with(
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    )
    builder.assert_called_once_with(proxy_handler)
    assert calls == [("https://api.anthropic.com/api/oauth/usage", 3)]
    assert result["five_hour"]["utilization"] == 25
