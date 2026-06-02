"""KIS Developers OpenAPI 클라이언트 — 외인/기관/연기금 순매수 ranking 조회.

토큰 발급은 매 프로세스마다 1회 (file cache). 호출은 rate limit (12 req/s) 준수.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://openapi.koreainvestment.com:9443"
_TOKEN_CACHE = Path.home() / ".cache" / "stock-analyzer" / "kis_token.json"
_REQUEST_INTERVAL = 0.1  # 100ms


def _load_dotenv(path: Path) -> dict[str, str]:
    """간이 .env 파서. 따옴표/주석 처리."""
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
    """환경변수 → .env (stock-analyzer) → .env (auto-trader, fallback) 순서로 KIS 자격증명 로드."""
    key = os.environ.get("KIS_APP_KEY", "")
    secret = os.environ.get("KIS_APP_SECRET", "")
    if key and secret:
        return key, secret

    for env_path in (
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / "auto-trader" / ".env",
    ):
        env = _load_dotenv(env_path)
        if env.get("KIS_APP_KEY") and env.get("KIS_APP_SECRET"):
            return env["KIS_APP_KEY"], env["KIS_APP_SECRET"]

    raise RuntimeError("KIS_APP_KEY/KIS_APP_SECRET 미설정 — env 또는 .env 필요")


def _load_cached_token() -> str | None:
    """cache 만료 안 됐으면 토큰 반환. 만료/없음/corrupt → None."""
    try:
        if not _TOKEN_CACHE.exists():
            return None
        payload = json.loads(_TOKEN_CACHE.read_text())
        if payload.get("expires_at", 0) > time.time() + 60:  # 1분 마진
            return payload.get("access_token")
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_token(token: str, expires_in: int) -> None:
    _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    }
    _TOKEN_CACHE.write_text(json.dumps(payload))


def _issue_token(key: str, secret: str, client: httpx.Client) -> str:
    """KIS OAuth2 토큰 발급. TTL 약 24시간 (response: expires_in 86400)."""
    resp = client.post(
        f"{_BASE_URL}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": key, "appsecret": secret},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    expires_in = body.get("expires_in", 86400)
    _save_token(token, expires_in)
    logger.info("KIS 토큰 신규 발급 — expires_in=%ds", expires_in)
    return token


class KISClient:
    """KIS API rate-limited 클라이언트. with-context 권장."""

    def __init__(self) -> None:
        self._key, self._secret = _load_credentials()
        self._client = httpx.Client(timeout=15.0)
        self._token: str | None = None
        self._last_request_at = 0.0

    def __enter__(self) -> "KISClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        cached = _load_cached_token()
        if cached:
            self._token = cached
            return cached
        self._token = _issue_token(self._key, self._secret, self._client)
        return self._token

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.time()

    def fetch_foreign_institution_total(
        self, *, market_div: str = "V", sort_code: str = "0",
    ) -> list[dict]:
        """외국인/기관/연기금 등 모든 투자자 순매수 30종목.

        market_div:
          V = 전체 KOSPI 우선 (실측 응답)
          1000 = KOSPI, 2000 = KOSDAQ — KIS 문서상 시장 구분
        sort_code:
          0 = 순매수상위 (foreign + institution combined)
          1 = 순매도상위
          기타 코드 (외인 단독, 기관 단독) — KIS 문서 참조
        """
        self._throttle()
        token = self._ensure_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._key,
            "appsecret": self._secret,
            "tr_id": "FHPTJ04400000",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_COND_SCR_DIV_CODE": "16449",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_RANK_SORT_CLS_CODE": sort_code,
            "FID_ETC_CLS_CODE": "0",
        }
        url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/foreign-institution-total"
        resp = self._client.get(url, headers=headers, params=params)
        if resp.status_code == 401:
            # 토큰 만료 — 재발급 후 1회 재시도
            logger.warning("KIS 401 — 토큰 재발급")
            self._token = None
            try:
                _TOKEN_CACHE.unlink()
            except OSError:
                pass
            token = self._ensure_token()
            headers["authorization"] = f"Bearer {token}"
            resp = self._client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("rt_cd") != "0":
            raise RuntimeError(
                f"KIS foreign-institution-total rt_cd={body.get('rt_cd')} "
                f"msg={body.get('msg1','')}"
            )
        return body.get("output", []) or []
