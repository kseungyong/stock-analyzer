"""토스 holdings → portfolio 미러링.

toss_client(외부 I/O) 와 분리된 비즈니스 로직. 토스 API 없이 단위 테스트 가능.
"""
from __future__ import annotations

import logging
import math
import os

from src import portfolio as portfolio_db
from src.toss_client import TossClient

logger = logging.getLogger(__name__)


def _krx_listing() -> dict[str, str]:
    """{6자리코드: '.KS'|'.KQ'} 매핑. stock_search 캐시 재사용."""
    from src.stock_search import _load_krx_cache
    out: dict[str, str] = {}
    for item in _load_krx_cache():
        sym = item.get("symbol", "")  # 예: '005930.KS'
        if sym.endswith((".KS", ".KQ")) and len(sym) > 3:
            code, suffix = sym[:-3], sym[-3:]
            out[code] = suffix
    return out


def _to_sa_symbol(holding: dict) -> str | None:
    """토스 holding → stock-analyzer symbol. 변환 불가 시 None."""
    sym = str(holding.get("symbol", "")).strip()
    if not sym:
        return None
    country = str(holding.get("marketCountry", "")).strip().upper()
    if country == "US":
        return sym
    if country == "KR":
        suffix = _krx_listing().get(sym, ".KS")
        return f"{sym}{suffix}"
    logger.warning("알 수 없는 marketCountry=%s symbol=%s — skip", country, sym)
    return None


class SyncAborted(RuntimeError):
    """안전장치 발동으로 sync 중단 (포트폴리오 무변경)."""


def _to_float(v) -> float | None:
    try:
        f = float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None
    return f if math.isfinite(f) else None


def mirror_to_portfolio(username: str, holdings: list[dict]) -> dict:
    """토스 holdings 로 portfolio 전체 미러링.

    Returns: {added, updated, removed, skipped, failed, target_count}
    Raises: SyncAborted (50% 삭제 가드, TOSS_SYNC_FORCE=1 로 우회)
    """
    target: dict[str, tuple[float, float]] = {}
    skipped = 0
    for h in holdings:
        sym = _to_sa_symbol(h)
        qty = _to_float(h.get("quantity"))
        avg = _to_float(h.get("averagePurchasePrice"))
        if sym is None or qty is None or avg is None or qty <= 0 or avg <= 0:
            skipped += 1
            logger.info("skip holding: symbol=%s qty=%s avg=%s",
                        h.get("symbol"), h.get("quantity"), h.get("averagePurchasePrice"))
            continue
        target[sym] = (avg, qty)

    current = {r["symbol"] for r in portfolio_db.list_holdings(username)}
    to_remove = current - target.keys()

    # 안전장치: 삭제가 현재 보유의 50% 초과면 abort (FORCE 로 우회)
    # 단 소규모 포트폴리오(<3종목)는 1종목 정리도 50% 초과가 되므로 제외 —
    # 가드의 목적은 토스 API 일시오류로 인한 '대량삭제' 방지이지 정상적인 종목 교체가 아님.
    if len(current) >= 3 and len(to_remove) > len(current) * 0.5:
        if os.environ.get("TOSS_SYNC_FORCE") != "1":
            raise SyncAborted(
                f"삭제 대상 {len(to_remove)}/{len(current)} 이 50% 초과 — "
                f"대량삭제 의심. TOSS_SYNC_FORCE=1 로 강제 가능."
            )
        logger.warning("TOSS_SYNC_FORCE — 50%% 가드 우회, %d 종목 제거", len(to_remove))

    added = updated = removed = failed = 0
    for sym, (avg, qty) in target.items():
        try:
            is_new = portfolio_db.add_holding(username, sym, avg, qty)
            if is_new:
                added += 1
            else:
                updated += 1
        except Exception as e:
            failed += 1
            logger.warning("미러링 add 실패 — %s: %s", sym, e)
    for sym in to_remove:
        try:
            if portfolio_db.remove_holding(username, sym):
                removed += 1
        except Exception as e:
            failed += 1
            logger.warning("미러링 remove 실패 — %s: %s", sym, e)

    result = {"added": added, "updated": updated, "removed": removed,
              "skipped": skipped, "failed": failed, "target_count": len(target)}
    logger.info("미러링 완료 — %s", result)
    return result


def run_sync(username: str, *, dry_run: bool = False,
             account_seq: int | str | None = None) -> dict:
    """fetch accounts → holdings → 미러링. dry_run 이면 DB 무변경 diff 만.

    Raises: SyncAborted (빈 계좌 / 50% 가드), RuntimeError (API 에러)
    """
    with TossClient() as client:
        accounts = client.fetch_accounts()
        if not accounts:
            raise SyncAborted("계좌 조회 결과 없음 — sync 중단 (포트폴리오 무변경)")
        # account_seq 우선순위: 인자 → 환경변수 → 첫 계좌
        seq = account_seq or os.environ.get("TOSS_SYNC_ACCOUNT_SEQ") \
            or accounts[0].get("accountSeq")
        holdings = client.fetch_holdings(seq)

    if dry_run:
        # mirror 대신 diff 만 계산 — DB 무변경
        target = {}
        skipped = 0
        for h in holdings:
            sym = _to_sa_symbol(h)
            qty = _to_float(h.get("quantity"))
            avg = _to_float(h.get("averagePurchasePrice"))
            if sym is None or qty is None or avg is None or qty <= 0 or avg <= 0:
                skipped += 1
                continue
            target[sym] = (avg, qty)
        current = {r["symbol"] for r in portfolio_db.list_holdings(username)}
        return {"dry_run": True, "target_count": len(target), "skipped": skipped,
                "would_add": sorted(target.keys() - current),
                "would_remove": sorted(current - target.keys())}

    return mirror_to_portfolio(username, holdings)
