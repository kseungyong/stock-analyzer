"""cleanup — 관심 가치 떨어진 종목 자동 정리.

조건 (모두 AND):
1. composite < -5 (Tech + BNF + Pattern×0.5)
2. 최근 7일 (5+ row) 모두 조건 1 만족
3. ETF/인덱스 아님
4. portfolio 보유 중 아님
5. settings.yaml 에 pinned 또는 note 없음
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import composite_history

logger = logging.getLogger(__name__)

_ETF_PREFIXES = ("KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO")
_ETF_SYMBOLS = {
    "SPY", "QQQ", "VTI", "VOO", "IWM",
    "KORU", "QLD", "TECL", "USD", "SOXL", "TQQQ",
}

_COMPOSITE_THRESHOLD = -5.0
_MIN_HISTORY_ROWS = 5
_SAFETY_LIMIT = 10


def is_etf(symbol: str, name: str) -> bool:
    """ETF/인덱스 식별. symbol 화이트리스트 또는 name prefix 매칭."""
    sym_upper = symbol.upper()
    if sym_upper in _ETF_SYMBOLS:
        return True
    name_upper = name.upper()
    return any(name_upper.startswith(p) for p in _ETF_PREFIXES)


def should_remove(
    symbol: str,
    name: str,
    history_rows: list[tuple[int, float]],
    is_held: bool,
    is_pinned_or_noted: bool,
) -> bool:
    """5개 조건 AND 판정. True면 삭제 후보."""
    if is_etf(symbol, name):
        return False
    if is_held:
        return False
    if is_pinned_or_noted:
        return False
    if len(history_rows) < _MIN_HISTORY_ROWS:
        return False
    return all(composite < _COMPOSITE_THRESHOLD for _, composite in history_rows)


def find_candidates(config: dict, held_symbols: set[str]) -> list[dict]:
    """settings.yaml 의 모든 종목을 검사하여 삭제 후보 list 반환.

    Args:
        config: yaml.safe_load 된 settings.yaml dict
        held_symbols: portfolio 보유 종목 symbol set

    Returns:
        [{"symbol", "name", "market", "composite_avg", "days"}, ...]
    """
    candidates = []
    stocks_by_market = config.get("stocks", {})
    for market, group in stocks_by_market.items():
        for stock in group:
            symbol = stock["symbol"]
            name = stock["name"]
            is_held = symbol in held_symbols
            is_pinned_or_noted = bool(
                stock.get("pinned") or stock.get("note")
            )
            history_rows = composite_history.recent(symbol, days=7)
            if not should_remove(
                symbol, name, history_rows, is_held, is_pinned_or_noted,
            ):
                continue
            composites = [c for _, c in history_rows]
            candidates.append({
                "symbol": symbol,
                "name": name,
                "market": market,
                "composite_avg": sum(composites) / len(composites),
                "days": len(history_rows),
            })
    return candidates


def _git_commit_push(config_path: Path, log_path: Path,
                     candidates: list[dict]) -> bool:
    """git add + commit + push. push 실패는 warn only.

    Returns: commit 성공 여부 (push 무관).
    """
    # repo_root는 git rev-parse로 검색 (config_path 위치에 의존하지 않음)
    try:
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=config_path.resolve().parent,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.warning("cleanup: git repo root 탐색 실패: %s", e)
        return False
    try:
        body_lines = "\n".join(
            f"- {c['symbol']} {c['name']} (composite_avg={c['composite_avg']:.2f})"
            for c in candidates
        )
        msg = (
            f"chore(cleanup): 자동 제거 {len(candidates)}종목 "
            "(composite < -5, 7일 연속)\n\n"
            f"{body_lines}\n\n"
            "Triggered by: ai.stock-analyzer.cleanup launchd cron\n"
            "Restore: git revert <this-sha>"
        )
        subprocess.run(
            ["git", "add", str(config_path), str(log_path)],
            cwd=repo_root, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=repo_root, check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning("cleanup git commit 실패: %s", e)
        return False
    try:
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo_root, check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning("cleanup git push 실패 (로컬 commit 유지): %s", e)
    return True


def apply(
    candidates: list[dict],
    config_path: Path | str,
    log_path: Path | str,
    dry_run: bool = False,
) -> dict:
    """settings.yaml 수정 + 로그 + git commit.

    Returns: {"removed": N, "limited": bool, "dry_run": bool}
    """
    import yaml

    config_path = Path(config_path)
    log_path = Path(log_path)

    result = {"removed": 0, "limited": False, "dry_run": dry_run}

    if not candidates:
        return result

    if len(candidates) > _SAFETY_LIMIT:
        logger.warning(
            "cleanup safety limit 초과 (%d > %d) — abort. 의심스러운 대량 삭제 감지.",
            len(candidates), _SAFETY_LIMIT,
        )
        result["limited"] = True
        return result

    if dry_run:
        for c in candidates:
            logger.info(
                "[DRY-RUN] would remove: %s %s composite_avg=%.2f days=%d",
                c["symbol"], c["name"], c["composite_avg"], c["days"],
            )
        return result

    # settings.yaml 수정
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    remove_set = {c["symbol"] for c in candidates}
    for market in list(config.get("stocks", {}).keys()):
        config["stocks"][market] = [
            s for s in config["stocks"][market]
            if s["symbol"] not in remove_set
        ]
    config_path.write_text(
        yaml.dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # 로그 append
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    log_lines = "\n".join(
        f"{now_kst}\t{c['symbol']}\t{c['name']}\t"
        f"composite_avg={c['composite_avg']:.2f}\tdays={c['days']}"
        for c in candidates
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(log_lines + "\n")

    # git commit + push
    _git_commit_push(config_path, log_path, candidates)
    result["removed"] = len(candidates)
    return result
