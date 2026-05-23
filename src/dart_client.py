"""dart_client — DART OpenAPI HTTP wrapper.

corp_code 매핑 + Phase A 9개 endpoint (DS001 list + DS005 6 + DS004 2).
"""
from __future__ import annotations

import io
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

import requests

from src import dart_cache

logger = logging.getLogger(__name__)

_DART_BASE = "https://opendart.fss.or.kr/api"
_CORP_CODE_URL = _DART_BASE + "/corpCode.xml"
_HTTP_TIMEOUT = 30


def _api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY 환경변수 없음")
    return key


def _parse_corp_code_xml(xml_text: str) -> list[dict]:
    """CORPCODE.xml → [{corp_code, corp_name, stock_code, modify_date}, ...]"""
    root = ET.fromstring(xml_text)
    rows = []
    for elem in root.findall(".//list"):
        rows.append({
            "corp_code": (elem.findtext("corp_code") or "").strip(),
            "corp_name": (elem.findtext("corp_name") or "").strip(),
            "stock_code": (elem.findtext("stock_code") or "").strip(),
            "modify_date": (elem.findtext("modify_date") or "").strip(),
        })
    return rows


def _to_krx_code(symbol: str) -> str:
    """'005930.KS' → '005930' (suffix 제거 + 6자리 zfill)."""
    return symbol.split(".")[0].zfill(6)


def get_corp_code(symbol: str) -> str | None:
    """yfinance 심볼 → DART corp_code. 캐시 조회 only."""
    krx = _to_krx_code(symbol)
    return dart_cache.get_corp_code_by_stock(krx)


def download_corp_codes() -> int:
    """corpCode.xml ZIP 다운로드 → XML parse → corp_codes 테이블 UPSERT.

    Returns: 갱신된 row 수.
    """
    url = _CORP_CODE_URL
    try:
        resp = requests.get(
            url, params={"crtfc_key": _api_key()}, timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("corpCode.xml 다운로드 실패: %s", e)
        return 0
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_text = zf.read("CORPCODE.xml").decode("utf-8")
        rows = _parse_corp_code_xml(xml_text)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        logger.warning("corpCode.xml ZIP/XML 파싱 실패: %s", e)
        return 0
    return dart_cache.upsert_corp_codes(rows)


def refresh_corp_codes_if_stale(days: int = 7) -> int:
    """마지막 modify_date 가 N일 이전이면 download. 아니면 0 반환."""
    last = dart_cache.corp_codes_last_modify_date()
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y%m%d")
            age_days = (datetime.now() - last_dt).days
            if age_days < days:
                logger.info("corp_codes %d일 stale (< %d) — skip download", age_days, days)
                return 0
        except ValueError:
            logger.warning("corp_codes modify_date 파싱 실패: %s — 재다운로드", last)
    return download_corp_codes()
