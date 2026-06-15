"""토스증권 Open API 클라이언트 — accounts + holdings 조회 (읽기 전용).

인증: OAuth2 client_credentials. 토큰 캐시 파일(TTL 24h).
응답은 {"result": ...} envelope. 에러 시 {"result": {"error": {...}}}.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_URL = "https://openapi.tossinvest.com"
_TOKEN_CACHE = Path.home() / ".cache" / "stock-analyzer" / "toss_token.json"
_REQUEST_INTERVAL = 0.1
_ENV_PATHS = [Path(__file__).resolve().parent.parent / ".env"]

try:
    import httpx
    def _http_get(url, **kw): return httpx.get(url, timeout=15, **kw)
    def _http_post(url, **kw): return httpx.post(url, timeout=15, **kw)
except ImportError:
    import requests
    def _http_get(url, **kw): return requests.get(url, timeout=15, **kw)
    def _http_post(url, **kw): return requests.post(url, timeout=15, **kw)


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def _load_credentials() -> tuple[str, str]:
    cid = os.environ.get("TOSS_CLIENT_ID", "")
    csec = os.environ.get("TOSS_CLIENT_SECRET", "")
    if cid and csec:
        return cid, csec
    for env_path in _ENV_PATHS:
        env = _load_dotenv(env_path)
        if env.get("TOSS_CLIENT_ID") and env.get("TOSS_CLIENT_SECRET"):
            return env["TOSS_CLIENT_ID"], env["TOSS_CLIENT_SECRET"]
    raise RuntimeError("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정 — env 또는 .env 필요")


def _load_cached_token() -> str | None:
    try:
        if not _TOKEN_CACHE.exists():
            return None
        payload = json.loads(_TOKEN_CACHE.read_text())
        if payload.get("expires_at", 0) > time.time() + 60:
            return payload.get("access_token")
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_token(token: str, expires_in: int) -> None:
    _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE.write_text(json.dumps(
        {"access_token": token, "expires_at": int(time.time()) + int(expires_in)}
    ))


def _issue_token(cid: str, csec: str) -> str:
    resp = _http_post(
        f"{_BASE_URL}/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    body = resp.json()
    _save_token(body["access_token"], body.get("expires_in", 86400))
    logger.info("토스 토큰 신규 발급 — expires_in=%ss", body.get("expires_in"))
    return body["access_token"]


def _unwrap(body: dict):
    """{"result": ...} envelope 언랩. error 객체면 RuntimeError."""
    if not isinstance(body, dict) or "result" not in body:
        raise RuntimeError(f"예상치 못한 응답 구조: {str(body)[:200]}")
    result = body["result"]
    if isinstance(result, dict) and "error" in result:
        err = result["error"] or {}
        raise RuntimeError(f"토스 API 에러: {err.get('code')} {err.get('message')}")
    return result


class TossClient:
    def __init__(self) -> None:
        self._cid, self._csec = _load_credentials()
        self._token: str | None = None
        self._last_request_at = 0.0

    def __enter__(self): return self
    # 모듈 레벨 _http_get 이 매 호출 새 연결 — 정리할 client 핸들 없음 (의도된 단순화)
    def __exit__(self, *exc): return None

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        cached = _load_cached_token()
        self._token = cached or _issue_token(self._cid, self._csec)
        return self._token

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.time()

    def _get(self, path: str, extra_headers: dict | None = None):
        self._throttle()
        token = self._ensure_token()
        headers = {"authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        resp = _http_get(f"{_BASE_URL}{path}", headers=headers)
        if resp.status_code == 401:
            # TTL 미만이지만 서버가 거부한 토큰 — unlink 가 _ensure_token 재발급을 강제 (load-bearing)
            logger.warning("토스 401 — 토큰 재발급")
            self._token = None
            try:
                _TOKEN_CACHE.unlink()
            except OSError:
                pass
            headers["authorization"] = f"Bearer {self._ensure_token()}"
            resp = _http_get(f"{_BASE_URL}{path}", headers=headers)
        resp.raise_for_status()
        return _unwrap(resp.json())

    def fetch_accounts(self) -> list[dict]:
        """[{accountNo, accountSeq, accountType}, ...] (빈 리스트 가능)."""
        result = self._get("/api/v1/accounts")
        return result if isinstance(result, list) else []

    def fetch_holdings(self, account_seq: int | str) -> list[dict]:
        """보유 종목 items[]. X-Tossinvest-Account 헤더에 accountSeq(정수) 사용."""
        result = self._get(
            "/api/v1/holdings",
            extra_headers={"X-Tossinvest-Account": str(account_seq)},
        )
        if isinstance(result, dict):
            return result.get("items", []) or []
        return []
