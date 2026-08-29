"""Codex subscription usage via the local Codex login, without launching Codex."""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
KEYCHAIN_SERVICE = "Codex Auth"


class CodexUsageError(RuntimeError):
    pass


@dataclass
class AuthState:
    data: dict[str, Any]
    source: str
    path: Optional[Path] = None

    @property
    def tokens(self) -> dict[str, Any]:
        value = self.data.get("tokens")
        return value if isinstance(value, dict) else {}


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    candidates = [text]
    stripped = text.strip()
    if len(stripped) % 2 == 0:
        try:
            candidates.append(bytes.fromhex(stripped).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            pass
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _has_access_token(data: dict[str, Any]) -> bool:
    tokens = data.get("tokens")
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    return isinstance(token, str) and bool(token.strip())


def _auth_paths() -> list[Path]:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return [Path(os.path.expanduser(codex_home)) / "auth.json"]
    return [Path.home() / ".config/codex/auth.json", Path.home() / ".codex/auth.json"]


def _load_keychain_auth() -> Optional[AuthState]:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = _parse_json(result.stdout) if result.returncode == 0 else None
    return AuthState(data, "keychain") if data and _has_access_token(data) else None


def load_auth_candidates() -> list[AuthState]:
    candidates: list[AuthState] = []
    for path in _auth_paths():
        try:
            data = _parse_json(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if data and _has_access_token(data):
            candidates.append(AuthState(data, "file", path))
    keychain = _load_keychain_auth()
    if keychain is not None:
        candidates.append(keychain)
    if not candidates:
        raise CodexUsageError("未找到 Codex OAuth 登录，请在终端运行 `codex` 登录")
    return candidates


def _jwt_expiry(token: str) -> Optional[float]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    expiry = data.get("exp") if isinstance(data, dict) else None
    return float(expiry) if isinstance(expiry, (int, float)) else None


def _ca_file() -> Optional[str]:
    candidates = [
        os.environ.get("SSL_CERT_FILE", "").strip(),
        os.environ.get("REQUESTS_CA_BUNDLE", "").strip(),
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
    ]
    try:
        import certifi
    except ImportError:
        pass
    else:
        candidates.append(certifi.where())
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def ssl_context() -> ssl.SSLContext:
    """The frozen .app ships its own OpenSSL, whose compiled-in CA directory
    does not exist on macOS, so verification fails with
    CERTIFICATE_VERIFY_FAILED unless a CA bundle is loaded explicitly.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    ca_file = _ca_file()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    return context


def _opener(proxy_url: Optional[str]):
    handlers: list[Any] = [urllib.request.HTTPSHandler(context=ssl_context())]
    if proxy_url:
        handlers.append(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    return urllib.request.build_opener(*handlers)


def _request_json(
    request: urllib.request.Request,
    timeout: float,
    proxy_url: Optional[str],
) -> tuple[dict[str, Any], Mapping[str, str]]:
    try:
        with _opener(proxy_url).open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            headers = response.headers
    except urllib.error.HTTPError as exc:
        raise CodexUsageError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CodexUsageError(f"无法连接 Codex 用量接口：{reason}") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CodexUsageError("Codex 用量接口返回了无效 JSON") from exc
    if not isinstance(data, dict):
        raise CodexUsageError("Codex 用量接口返回了意外的数据格式")
    return data, headers


def _save_auth(state: AuthState) -> None:
    text = json.dumps(state.data, ensure_ascii=False, indent=2, sort_keys=True)
    if state.source == "keychain":
        try:
            subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                    text,
                ],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    if state.path is None:
        return
    state.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=str(state.path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, state.path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _refresh(state: AuthState, timeout: float, proxy_url: Optional[str]) -> str:
    refresh_token = state.tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexUsageError("Codex 登录已过期，请重新运行 `codex` 登录")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        REFRESH_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    data, _ = _request_json(request, timeout, proxy_url)
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexUsageError("Codex Token 刷新响应无效，请重新登录")
    state.tokens["access_token"] = access_token
    for response_key, auth_key in (
        ("refresh_token", "refresh_token"),
        ("id_token", "id_token"),
    ):
        value = data.get(response_key)
        if isinstance(value, str) and value:
            state.tokens[auth_key] = value
    state.data["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_auth(state)
    return access_token


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value) if isinstance(value, str) and value.strip() else None
    except ValueError:
        return None


def _window(value: Any, header_percent: Any = None) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict) and header_percent is None:
        return None
    source = value if isinstance(value, dict) else {}
    used = _number(source.get("used_percent"))
    if used is None:
        used = _number(header_percent)
    duration = _number(source.get("limit_window_seconds"))
    reset_at = _number(source.get("reset_at"))
    if reset_at is None:
        reset_after = _number(source.get("reset_after_seconds"))
        if reset_after is not None:
            reset_at = time.time() + reset_after
    result: dict[str, Any] = {}
    if used is not None:
        result["usedPercent"] = used
    if duration is not None:
        result["windowDurationMins"] = int(round(duration / 60))
    if reset_at is not None:
        result["resetsAt"] = int(reset_at)
    return result


def normalize_usage(body: dict[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    def snapshot(rate_limit: Any, name: str, include_account: bool) -> dict[str, Any]:
        limits = rate_limit if isinstance(rate_limit, dict) else {}
        value: dict[str, Any] = {"limitName": name}
        primary = _window(
            limits.get("primary_window"),
            headers.get("x-codex-primary-used-percent") if include_account else None,
        )
        secondary = _window(
            limits.get("secondary_window"),
            headers.get("x-codex-secondary-used-percent") if include_account else None,
        )
        if primary is not None:
            value["primary"] = primary
        if secondary is not None:
            value["secondary"] = secondary
        if include_account:
            value["planType"] = body.get("plan_type")
            credits = body.get("credits")
            if isinstance(credits, dict):
                value["credits"] = {
                    "hasCredits": credits.get("has_credits"),
                    "unlimited": credits.get("unlimited"),
                    "balance": credits.get("balance"),
                }
        return value

    main = snapshot(body.get("rate_limit"), "Codex", True)
    buckets: dict[str, dict[str, Any]] = {"codex": main}
    additional = body.get("additional_rate_limits")
    if isinstance(additional, list):
        for index, entry in enumerate(additional):
            if not isinstance(entry, dict):
                continue
            name = entry.get("limit_name") or entry.get("metered_feature") or f"Codex {index + 2}"
            bucket_id = str(entry.get("metered_feature") or entry.get("limit_name") or index)
            buckets[bucket_id] = snapshot(entry.get("rate_limit"), str(name), False)

    result: dict[str, Any] = {"rateLimits": main, "rateLimitsByLimitId": buckets}
    reset_credits = body.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict):
        result["rateLimitResetCredits"] = {
            "availableCount": reset_credits.get("available_count", 0)
        }
    return result


def _fetch_with_state(
    state: AuthState,
    timeout: float,
    proxy_url: Optional[str],
) -> dict[str, Any]:
    access_token = str(state.tokens.get("access_token", "")).strip()
    expiry = _jwt_expiry(access_token)
    if expiry is not None and expiry - time.time() <= 5 * 60:
        access_token = _refresh(state, timeout, proxy_url)

    def request_for(token: str) -> urllib.request.Request:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "CodexQuota",
        }
        account_id = state.tokens.get("account_id")
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-Id"] = account_id
        return urllib.request.Request(USAGE_URL, headers=headers)

    try:
        body, headers = _request_json(request_for(access_token), timeout, proxy_url)
    except CodexUsageError as exc:
        if str(exc) not in ("HTTP 401", "HTTP 403"):
            raise
        access_token = _refresh(state, timeout, proxy_url)
        body, headers = _request_json(request_for(access_token), timeout, proxy_url)
    return normalize_usage(body, headers)


def fetch_codex_usage(timeout: float, proxy_url: Optional[str] = None) -> dict[str, Any]:
    last_auth_error: Optional[CodexUsageError] = None
    for state in load_auth_candidates():
        try:
            return _fetch_with_state(state, timeout, proxy_url)
        except CodexUsageError as exc:
            if "登录" in str(exc) or str(exc) in ("HTTP 401", "HTTP 403"):
                last_auth_error = exc
                continue
            raise
    raise last_auth_error or CodexUsageError("Codex 登录不可用")
