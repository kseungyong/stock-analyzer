"""외인 ranking 자동 종목 overlay — git 추적 분리.

settings.yaml          = 사용자가 직접 등록한 종목 (git 추적 O)
config/foreign_ranking.yaml = foreign_ranking cron 자동 종목 (git 추적 X, gitignore)

매일 16:00 foreign-ranking cron 이 overlay 파일만 재작성하므로 settings.yaml 은
변경되지 않고 git working tree 가 더럽혀지지 않는다.

규칙:
- 로더 (main.load_config / web_app._load_config) 는 apply_overlay 로 두 소스 머지
- writer (web_app._save_config) 는 strip_overlay 로 settings.yaml 오염 방지
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

OVERLAY_PATH = Path(__file__).resolve().parent.parent / "config" / "foreign_ranking.yaml"
SOURCE_TAG = "foreign_ranking"


def load_overlay() -> dict:
    """overlay 파일을 {market: [entry, ...]} 로 로드. 없거나 비면 {}."""
    if not OVERLAY_PATH.exists():
        return {}
    data = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def write_overlay(by_market: dict) -> None:
    """overlay 파일 전체 교체."""
    OVERLAY_PATH.write_text(
        yaml.dump(by_market, allow_unicode=True, sort_keys=False,
                  default_flow_style=False),
        encoding="utf-8",
    )


def apply_overlay(config: dict) -> dict:
    """config['stocks'] 에 overlay 종목을 머지 (in-place 후 동일 dict 반환).

    중복 symbol 은 settings.yaml 우선 (사용자 등록이 overlay 보다 우선).
    """
    overlay = load_overlay()
    if not overlay:
        return config
    stocks = config.setdefault("stocks", {})
    for market, entries in overlay.items():
        if not entries:
            continue
        base = stocks.get(market) or []
        existing = {s["symbol"] for s in base}
        merged = list(base)
        for e in entries:
            sym = e.get("symbol")
            if sym and sym not in existing:
                merged.append(e)
                existing.add(sym)
        stocks[market] = merged
    return config


def strip_overlay(config: dict) -> dict:
    """source=foreign_ranking 항목을 제거한 deep copy 반환 (settings.yaml 저장 전용)."""
    out = copy.deepcopy(config)
    for market, group in out.get("stocks", {}).items():
        out["stocks"][market] = [
            s for s in group if s.get("source") != SOURCE_TAG
        ]
    return out
