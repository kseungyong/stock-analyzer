# Pattern Badge Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종목 분석 페이지의 패턴 배지 (잉태형/더블바텀 등) 를 클릭하면 모달 팝업으로 교과서 일러스트 + 해당 종목의 실제 검출 차트를 2탭으로 보여준다.

**Architecture:** YAML 메타데이터에서 정적 SVG 일러스트 즉시 로드 + 사용자가 "실제 차트" 탭 클릭 시 matplotlib lazy 생성. Flask 라우트 2개 (textbook/actual) + vanilla JS 모달 + memory LRU 캐시.

**Tech Stack:** Python 3.11+, Flask, PyYAML, matplotlib, yfinance, vanilla JavaScript (별도 빌드 단계 X), CSS

---

## File Structure

### 신규 (8 파일)
| 파일 | 책임 | 예상 라인 |
|---|---|---|
| `src/data/pattern_metadata.yaml` | 5 chart + 60+ candle 패턴 매핑 (Tier1 rich / Tier2 generic) | ~800 |
| `src/pattern_metadata.py` | YAML 로더 + lookup 헬퍼 + XSS 가드 | ~80 |
| `src/pattern_popup.py` | actual chart 빌더 + matplotlib 마킹 + LRU cache | ~200 |
| `src/static/pattern-modal.css` | 모달/탭/SVG 스타일 | ~100 |
| `src/static/pattern-modal.js` | 클릭 핸들러 + 모달 + 탭 + AJAX | ~180 |
| `tests/test_pattern_metadata.py` | YAML 로더 단위 | ~80 |
| `tests/test_pattern_popup.py` | actual chart 빌더 단위 (mock yfinance + matplotlib) | ~120 |
| `tests/test_pattern_routes.py` | `/api/pattern-popup/*` 라우트 통합 | ~120 |

### 수정 (2 파일)
- `requirements.txt` — `pyyaml>=6.0` 추가 (이미 있을 가능성 — 점검)
- `src/web_app.py` — 라우트 2개 추가 (~80 lines), 배지 렌더링 부분 3곳 수정 (~30 lines), 페이지 head 에 CSS/JS include (~5 lines)

---

## Task 1: Scaffolding — pyyaml + 디렉토리

**Files:**
- Modify: `requirements.txt`
- Create: `src/data/` (dir), `src/static/` (dir) (이미 존재할 수도 있음)

- [ ] **Step 1: pyyaml 의존성 확인**

Run:
```bash
cd /Users/sykim/Projects/stock-analyzer
grep -E '^pyyaml|^PyYAML' requirements.txt
```

이미 있으면 Step 2 skip. 없으면 추가:

```bash
echo "pyyaml>=6.0" >> requirements.txt
```

- [ ] **Step 2: 설치**

Run:
```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import yaml; print('pyyaml', yaml.__version__)"
```

Expected: `pyyaml 6.X.X` 출력 (또는 더 높은 버전)

- [ ] **Step 3: 디렉토리 생성**

```bash
mkdir -p src/data src/static
ls -la src/data src/static
```

Expected: 두 디렉토리 표시 (비어 있어도 OK)

- [ ] **Step 4: Flask static 동작 확인**

`src/static/test.txt` 임시 파일 만들어 Flask 가 정적 서빙하는지 확인:

```bash
echo "test" > src/static/test.txt
```

`src/web_app.py` 의 Flask app 초기화 부분 확인:

```bash
grep -nE 'Flask\(|static_folder|static_url_path' src/web_app.py | head -5
```

Flask 가 명시적 `static_folder` 설정 없으면 기본값 `static/` (app 모듈 디렉토리 기준). app 모듈이 `src/web_app.py` 이므로 `src/static/` 이 기본 정적 폴더. 확인 후 임시 파일 삭제:

```bash
rm src/static/test.txt
```

만약 Flask 가 static 서빙 안 되면 (404 등), `Flask(__name__, static_folder='static', static_url_path='/static')` 명시 필요. 그 경우 web_app.py 의 app 초기화 부분 수정 추가하고 Task 1 에 포함.

- [ ] **Step 5: 커밋**

```bash
git add requirements.txt
git commit -m "feat(pattern-modal): pyyaml dependency + 디렉토리 스캐폴딩

- pyyaml>=6.0 requirements.txt
- src/data, src/static 디렉토리 생성

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: pattern_metadata.py — YAML 로더 + lookup

**Files:**
- Create: `src/data/pattern_metadata.yaml` (초기 stub, 2 패턴만)
- Create: `src/pattern_metadata.py`
- Create: `tests/test_pattern_metadata.py`

- [ ] **Step 1: Failing 테스트 작성**

Create `tests/test_pattern_metadata.py`:

```python
"""pattern_metadata.py 단위 테스트."""
from __future__ import annotations

import pytest

from src import pattern_metadata as pm


def test_load_returns_dict_with_known_keys():
    data = pm.load_metadata()
    assert isinstance(data, dict)
    # 초기 stub 에 들어 있는 키 (Task 3 에서 채워질 예정 — 일단 2개만)
    assert "더블바텀(W)" in data
    assert "잉태형" in data


def test_lookup_returns_entry_with_required_fields():
    entry = pm.lookup("더블바텀(W)")
    assert entry is not None
    assert "svg" in entry
    assert "description_html" in entry
    assert "signal_typical" in entry
    assert entry["signal_typical"] in {"매수", "매도", "관망", "varies"}


def test_lookup_unknown_pattern_returns_none():
    assert pm.lookup("존재하지않는패턴") is None


def test_svg_xss_guard_raises_on_script_tag():
    """SVG 안에 <script> 가 있으면 startup 시 ValueError."""
    malicious_yaml = """
"악성패턴":
  svg: '<svg><script>alert(1)</script></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError, match="script"):
        pm._parse_and_validate(malicious_yaml)


def test_svg_xss_guard_case_insensitive():
    """대소문자 무관 차단."""
    malicious_yaml = """
"악성패턴":
  svg: '<svg><SCRIPT>alert(1)</SCRIPT></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError):
        pm._parse_and_validate(malicious_yaml)


def test_svg_xss_guard_with_whitespace():
    """<  script (공백 삽입) 도 차단."""
    malicious_yaml = """
"악성패턴":
  svg: '<svg>< script >alert(1)</script></svg>'
  description_html: '<p>x</p>'
  signal_typical: '관망'
"""
    with pytest.raises(ValueError):
        pm._parse_and_validate(malicious_yaml)
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py -v
```

Expected: ALL FAIL with `ModuleNotFoundError: No module named 'src.pattern_metadata'` 또는 유사 메시지

- [ ] **Step 3: pattern_metadata.yaml stub 작성**

Create `src/data/pattern_metadata.yaml`:

```yaml
# Pattern badge modal 메타데이터 — 교과서 일러스트 + 설명
# 형식: <패턴 한글이름>: { svg: <inline SVG>, description_html: <짧은 설명 HTML>, signal_typical: <매수/매도/관망/varies> }

"더블바텀(W)":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <polyline points="10,30 30,50 50,70 70,80 90,75 110,80 130,70 150,50 170,30 190,15"
                fill="none" stroke="#16A34A" stroke-width="2"/>
      <line x1="50" y1="55" x2="170" y2="55" stroke="#999" stroke-dasharray="3,3" stroke-width="1"/>
      <text x="50" y="92" font-size="8" fill="#16A34A">저점1</text>
      <text x="115" y="92" font-size="8" fill="#16A34A">저점2</text>
      <text x="100" y="50" font-size="7" fill="#666">넥라인</text>
      <text x="160" y="20" font-size="8" fill="#16A34A">돌파</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 두 저점이 비슷한 가격에서 형성되고 사이에 고점(넥라인). 알파벳 W 모양.</p>
    <p><strong>신호:</strong> 넥라인 돌파 시 강한 매수 신호. 하락 추세 종료 + 상승 전환.</p>
    <p><strong>검출:</strong> 저점1·2 가격이 ±3% 이내, 사이에 명확한 고점, (옵션) 넥라인 돌파.</p>

"잉태형":
  signal_typical: varies
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <!-- Bullish harami: 큰 흑봉 다음 작은 양봉 -->
      <rect x="60" y="20" width="20" height="60" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="70" y1="10" x2="70" y2="90" stroke="#7F1D1D"/>
      <rect x="100" y="40" width="15" height="25" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="107" y1="35" x2="107" y2="70" stroke="#16A34A"/>
      <text x="50" y="95" font-size="7" fill="#666">큰 흑봉</text>
      <text x="95" y="95" font-size="7" fill="#666">작은 양봉</text>
      <text x="20" y="50" font-size="7" fill="#666">(반전 시사)</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 큰 봉 다음에 그 봉의 시고저종 안에 완전히 들어가는 작은 봉 (2봉 패턴).</p>
    <p><strong>신호:</strong> 추세 반전 가능성. 흑봉→양봉 잉태는 매수, 양봉→흑봉 잉태는 매도. 단독 시그널 약함 — 다른 지표와 결합.</p>
    <p><strong>검출:</strong> talib CDLHARAMI (코드값 ±100).</p>
```

- [ ] **Step 4: pattern_metadata.py 구현**

Create `src/pattern_metadata.py`:

```python
"""Pattern badge modal 메타데이터 로더.

YAML 파일에서 패턴별 SVG + 설명 텍스트 로드. 시작 시 1회 로드, 메모리 캐시.
보안: SVG 안에 <script> 차단 (간이 XSS 가드).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent / "data" / "pattern_metadata.yaml"
_SCRIPT_PATTERN = re.compile(r"<\s*script", re.IGNORECASE)

_cache: dict[str, Any] | None = None


def _parse_and_validate(yaml_text: str) -> dict[str, Any]:
    """YAML text → dict. SVG 에 <script> 있으면 ValueError."""
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"pattern_metadata.yaml: 루트는 dict 여야 함, got {type(data).__name__}")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"pattern_metadata.yaml: {name} 의 값이 dict 가 아님")
        svg = entry.get("svg", "")
        if _SCRIPT_PATTERN.search(svg):
            raise ValueError(
                f"pattern_metadata.yaml: {name} 의 svg 에 <script> 가 포함되어 있음 (XSS 위험)"
            )
        desc = entry.get("description_html", "")
        if _SCRIPT_PATTERN.search(desc):
            raise ValueError(
                f"pattern_metadata.yaml: {name} 의 description_html 에 <script> 가 포함되어 있음"
            )
    return data


def load_metadata() -> dict[str, Any]:
    """YAML 파일 로드. 1회 캐싱."""
    global _cache
    if _cache is None:
        if not _YAML_PATH.exists():
            raise FileNotFoundError(f"pattern_metadata.yaml not found at {_YAML_PATH}")
        with open(_YAML_PATH, "r", encoding="utf-8") as f:
            _cache = _parse_and_validate(f.read())
        logger.info("pattern_metadata 로드 완료: %d 패턴", len(_cache))
    return _cache


def lookup(pattern_name: str) -> dict[str, Any] | None:
    """패턴 이름 → entry dict 또는 None."""
    data = load_metadata()
    return data.get(pattern_name)


def reset_cache() -> None:
    """테스트용 캐시 리셋."""
    global _cache
    _cache = None
```

- [ ] **Step 5: 테스트 통과 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py -v
```

Expected: ALL PASS (6 tests)

- [ ] **Step 6: 커밋**

```bash
git add src/pattern_metadata.py src/data/pattern_metadata.yaml tests/test_pattern_metadata.py
git commit -m "feat(pattern-modal): YAML 로더 + XSS 가드 + stub 2 패턴

- src/pattern_metadata.py: load_metadata() + lookup() + <script> 차단
- src/data/pattern_metadata.yaml: 더블바텀(W) + 잉태형 초기 stub
- tests/test_pattern_metadata.py: 6 케이스 (정상 / 미정의 / XSS 3 변형)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: YAML 콘텐츠 — Chart 패턴 5종 (Tier 1)

**Files:**
- Modify: `src/data/pattern_metadata.yaml`

- [ ] **Step 1: 5 chart 패턴 entry 추가**

`src/data/pattern_metadata.yaml` 에 더블바텀(W) 다음, 잉태형 이전 위치에 4 entries 추가 (더블바텀은 이미 stub 으로 있음 — 그대로 유지):

```yaml
"더블탑(M)":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <polyline points="10,70 30,50 50,30 70,20 90,25 110,20 130,30 150,50 170,70 190,85"
                fill="none" stroke="#DC2626" stroke-width="2"/>
      <line x1="50" y1="45" x2="170" y2="45" stroke="#999" stroke-dasharray="3,3" stroke-width="1"/>
      <text x="50" y="15" font-size="8" fill="#DC2626">고점1</text>
      <text x="115" y="15" font-size="8" fill="#DC2626">고점2</text>
      <text x="100" y="42" font-size="7" fill="#666">넥라인</text>
      <text x="160" y="85" font-size="8" fill="#DC2626">이탈</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 두 고점이 비슷한 가격에서 형성되고 사이에 저점(넥라인). 알파벳 M 모양.</p>
    <p><strong>신호:</strong> 넥라인 이탈 시 강한 매도 신호. 상승 추세 종료 + 하락 전환.</p>
    <p><strong>검출:</strong> 고점1·2 가격이 ±3% 이내, 사이에 명확한 저점, (옵션) 넥라인 이탈.</p>

"헤드앤숄더":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <polyline points="10,80 30,55 50,45 70,55 90,75 110,30 130,55 150,45 170,55 190,80"
                fill="none" stroke="#DC2626" stroke-width="2"/>
      <line x1="30" y1="75" x2="170" y2="75" stroke="#999" stroke-dasharray="3,3" stroke-width="1"/>
      <text x="20" y="50" font-size="7" fill="#DC2626">좌어깨</text>
      <text x="95" y="25" font-size="7" fill="#DC2626">헤드</text>
      <text x="155" y="50" font-size="7" fill="#DC2626">우어깨</text>
      <text x="120" y="72" font-size="7" fill="#666">넥라인</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 3개 고점 — 가운데 고점(헤드)이 가장 높고, 양쪽 고점(어깨)이 비슷한 높이. 상승 추세 정점.</p>
    <p><strong>신호:</strong> 매도. 두 어깨를 잇는 넥라인 이탈 시 추세 전환 확정.</p>
    <p><strong>검출:</strong> 3개 고점에서 좌·우 어깨 가격 ±5% 이내, 헤드가 둘 다보다 높음.</p>

"역헤드앤숄더":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <polyline points="10,20 30,45 50,55 70,45 90,25 110,70 130,45 150,55 170,45 190,20"
                fill="none" stroke="#16A34A" stroke-width="2"/>
      <line x1="30" y1="25" x2="170" y2="25" stroke="#999" stroke-dasharray="3,3" stroke-width="1"/>
      <text x="20" y="40" font-size="7" fill="#16A34A">좌어깨</text>
      <text x="95" y="80" font-size="7" fill="#16A34A">헤드</text>
      <text x="155" y="40" font-size="7" fill="#16A34A">우어깨</text>
      <text x="120" y="22" font-size="7" fill="#666">넥라인</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 3개 저점 — 가운데 저점(헤드)이 가장 낮고, 양쪽 저점(어깨)이 비슷한 깊이. 하락 추세 바닥.</p>
    <p><strong>신호:</strong> 매수. 두 어깨를 잇는 넥라인 돌파 시 추세 전환 확정.</p>
    <p><strong>검출:</strong> 3개 저점에서 좌·우 어깨 가격 ±5% 이내, 헤드가 둘 다보다 낮음.</p>

"삼각형":
  signal_typical: varies
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <!-- 수렴 삼각형 -->
      <line x1="20" y1="20" x2="170" y2="50" stroke="#DC2626" stroke-width="1.5"/>
      <line x1="20" y1="80" x2="170" y2="50" stroke="#16A34A" stroke-width="1.5"/>
      <polyline points="20,55 40,30 60,65 80,35 100,55 120,40 140,55 160,48 175,52"
                fill="none" stroke="#1E40AF" stroke-width="1.5"/>
      <text x="180" y="50" font-size="7" fill="#666">꼭짓점</text>
      <text x="10" y="15" font-size="7" fill="#DC2626">고점 하락</text>
      <text x="10" y="92" font-size="7" fill="#16A34A">저점 상승</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 두 추세선 (상단 저항선 + 하단 지지선) 이 수렴. 변동성 축소 단계.</p>
    <p><strong>신호:</strong> 방향 불명 (varies). 꼭짓점 부근에서 한 방향으로 돌파 → 그 방향으로 추세 출발. 거래량 동반 시 신뢰도 ↑.</p>
    <p><strong>검출:</strong> 최근 3+ 고점이 하락 추세선, 3+ 저점이 상승 추세선 위. 두 선이 수렴.</p>
```

- [ ] **Step 2: yaml 파싱 + 5 패턴 lookup 검증**

```bash
.venv/bin/python -c "
from src import pattern_metadata as pm
pm.reset_cache()
data = pm.load_metadata()
print('loaded:', len(data), 'patterns')
for p in ['더블바텀(W)', '더블탑(M)', '헤드앤숄더', '역헤드앤숄더', '삼각형', '잉태형']:
    e = pm.lookup(p)
    assert e is not None, f'{p} 없음'
    assert 'svg' in e and 'description_html' in e
    print(f'  ✓ {p}: signal={e[\"signal_typical\"]}')
"
```

Expected: 6 entries OK 출력

- [ ] **Step 3: 기존 테스트 통과 재확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py -v
```

Expected: 6 PASS

- [ ] **Step 4: 커밋**

```bash
git add src/data/pattern_metadata.yaml
git commit -m "feat(pattern-modal): chart 패턴 5종 Tier1 YAML 콘텐츠

- 더블바텀(W) / 더블탑(M) / H&S / 역H&S / 삼각형 — SVG + 3-5줄 설명
- Tier 1 콘텐츠 (rich): 고유 일러스트 + 형성/신호/검출 설명

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: YAML 콘텐츠 — Top Candle 패턴 ~15종 (Tier 1)

**Files:**
- Modify: `src/data/pattern_metadata.yaml`

Tier1 캔들 패턴 선정 (자주 등장 + 명확한 시각 구분):
- 도지, 잠자리 도지, 묘비 도지
- 망치, 역망치, 교수형, 유성
- 장악형, 잉태형 (이미 있음), 잉태형 십자
- 새벽의 샛별, 저녁별
- 적삼병, 흑삼병
- 관통형, 먹구름

- [ ] **Step 1: 14 candle entries 추가 (잉태형은 stub 으로 이미 있음)**

`src/data/pattern_metadata.yaml` 끝에 다음 entries 추가:

```yaml
"도지":
  signal_typical: 관망
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <line x1="100" y1="20" x2="100" y2="80" stroke="#333" stroke-width="1"/>
      <line x1="80" y1="50" x2="120" y2="50" stroke="#333" stroke-width="2"/>
      <text x="50" y="92" font-size="7" fill="#666">시가 ≈ 종가</text>
      <text x="130" y="30" font-size="7" fill="#666">긴 위/아래꼬리</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 시가와 종가가 거의 같음 (몸통 거의 없음). 위·아래 꼬리는 있음.</p>
    <p><strong>신호:</strong> 매수·매도 균형. 추세 반전 가능성 시사하지만 단독으로는 약함. 위치 (상승/하락 끝) 가 중요.</p>

"잠자리 도지":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <line x1="100" y1="30" x2="100" y2="80" stroke="#333" stroke-width="1"/>
      <line x1="85" y1="30" x2="115" y2="30" stroke="#333" stroke-width="2"/>
      <text x="60" y="92" font-size="7" fill="#666">긴 아래꼬리</text>
      <text x="120" y="35" font-size="7" fill="#16A34A">시가=고가=종가</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 시가·종가·고가가 거의 같고 아래꼬리가 김.</p>
    <p><strong>신호:</strong> 매수. 하락 추세 끝에서 출현 시 강력한 반전 시사 (매수 압력으로 회복).</p>

"묘비 도지":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <line x1="100" y1="20" x2="100" y2="70" stroke="#333" stroke-width="1"/>
      <line x1="85" y1="70" x2="115" y2="70" stroke="#333" stroke-width="2"/>
      <text x="60" y="20" font-size="7" fill="#666">긴 위꼬리</text>
      <text x="120" y="75" font-size="7" fill="#DC2626">시가=저가=종가</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 시가·종가·저가가 거의 같고 위꼬리가 김.</p>
    <p><strong>신호:</strong> 매도. 상승 추세 끝에서 출현 시 강력한 반전 시사 (매도 압력으로 하락).</p>

"망치":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="92" y="30" width="16" height="20" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="100" y1="25" x2="100" y2="30" stroke="#16A34A"/>
      <line x1="100" y1="50" x2="100" y2="85" stroke="#16A34A"/>
      <text x="60" y="92" font-size="7" fill="#666">긴 아래꼬리 (몸통 2배 이상)</text>
      <text x="130" y="35" font-size="7" fill="#16A34A">작은 몸통</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 작은 몸통 + 긴 아래꼬리 (몸통의 2배 이상) + 짧은 위꼬리.</p>
    <p><strong>신호:</strong> 매수. 하락 추세 끝에 등장 시 강한 반전 신호. 일중 매도가 매수로 흡수됨을 의미.</p>

"역망치":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="92" y="55" width="16" height="20" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="100" y1="20" x2="100" y2="55" stroke="#16A34A"/>
      <line x1="100" y1="75" x2="100" y2="80" stroke="#16A34A"/>
      <text x="60" y="92" font-size="7" fill="#666">작은 몸통</text>
      <text x="130" y="35" font-size="7" fill="#16A34A">긴 위꼬리</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 작은 몸통 + 긴 위꼬리 (몸통의 2배 이상) + 짧은 아래꼬리. 망치 모양 뒤집힌 형태.</p>
    <p><strong>신호:</strong> 매수 (잠재). 하락 추세 끝에 등장 시 반전 후보 — 다음 봉에서 확인 필요.</p>

"교수형":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="92" y="20" width="16" height="20" fill="#DC2626" stroke="#7F1D1D" stroke-width="1.5"/>
      <line x1="100" y1="15" x2="100" y2="20" stroke="#7F1D1D"/>
      <line x1="100" y1="40" x2="100" y2="85" stroke="#7F1D1D"/>
      <text x="60" y="92" font-size="7" fill="#666">긴 아래꼬리 — 상승 끝에서</text>
      <text x="130" y="25" font-size="7" fill="#DC2626">작은 몸통</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 망치와 모양 동일 (작은 몸통 + 긴 아래꼬리) 하지만 상승 추세 끝에 등장.</p>
    <p><strong>신호:</strong> 매도. 상승 끝 매수 압력 약화 신호. 다음 봉에서 매도 확정 권장.</p>

"유성":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="92" y="55" width="16" height="20" fill="#DC2626" stroke="#7F1D1D" stroke-width="1.5"/>
      <line x1="100" y1="20" x2="100" y2="55" stroke="#7F1D1D"/>
      <line x1="100" y1="75" x2="100" y2="80" stroke="#7F1D1D"/>
      <text x="60" y="92" font-size="7" fill="#666">상승 끝에서 — 작은 몸통</text>
      <text x="130" y="35" font-size="7" fill="#DC2626">긴 위꼬리</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 작은 몸통 + 긴 위꼬리. 상승 추세 끝에 등장.</p>
    <p><strong>신호:</strong> 매도. 일중 고가 도달 후 종가가 시가 부근으로 회귀 — 상승 동력 소진.</p>

"장악형":
  signal_typical: varies
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <!-- bullish engulfing: 작은 흑봉 → 큰 양봉 -->
      <rect x="60" y="40" width="14" height="20" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="67" y1="35" x2="67" y2="65" stroke="#7F1D1D"/>
      <rect x="100" y="25" width="22" height="55" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="111" y1="20" x2="111" y2="85" stroke="#16A34A"/>
      <text x="50" y="92" font-size="7" fill="#666">작은 봉</text>
      <text x="95" y="92" font-size="7" fill="#16A34A">반대 색 큰 봉 (장악)</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 작은 봉 다음에 반대 색의 큰 봉이 이전 봉을 완전히 감쌈 (2봉 패턴).</p>
    <p><strong>신호:</strong> 강한 반전 시그널. 흑→양 장악 = 매수 (하락 끝), 양→흑 장악 = 매도 (상승 끝).</p>

"잉태형 십자":
  signal_typical: varies
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="60" y="20" width="20" height="60" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="70" y1="10" x2="70" y2="90" stroke="#7F1D1D"/>
      <line x1="105" y1="40" x2="115" y2="40" stroke="#333" stroke-width="2"/>
      <line x1="110" y1="35" x2="110" y2="50" stroke="#333" stroke-width="1"/>
      <text x="50" y="95" font-size="7" fill="#666">큰 봉</text>
      <text x="95" y="95" font-size="7" fill="#666">도지 (잉태)</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 잉태형의 변종 — 두 번째 봉이 도지 (시가=종가). 균형 더 강조됨.</p>
    <p><strong>신호:</strong> 잉태형보다 반전 강도 ↑. 추세 정지 + 반전 후보.</p>

"새벽의 샛별":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <!-- 3 candles: 큰 흑봉, 작은 도지, 큰 양봉 -->
      <rect x="40" y="20" width="16" height="60" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="48" y1="15" x2="48" y2="85" stroke="#7F1D1D"/>
      <line x1="90" y1="75" x2="106" y2="75" stroke="#333" stroke-width="2"/>
      <line x1="98" y1="68" x2="98" y2="85" stroke="#333"/>
      <rect x="140" y="20" width="16" height="60" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="148" y1="15" x2="148" y2="85" stroke="#16A34A"/>
      <text x="30" y="95" font-size="7" fill="#666">하락</text>
      <text x="78" y="95" font-size="7" fill="#666">정지</text>
      <text x="130" y="95" font-size="7" fill="#16A34A">상승</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 3봉 — 큰 흑봉 → 작은 봉 (gap down, 도지 가능) → 큰 양봉. 양봉이 첫 흑봉의 중간 이상.</p>
    <p><strong>신호:</strong> 강한 매수 신호. 하락 추세 바닥 확인. 거래량 동반 시 신뢰도 ↑.</p>

"저녁별":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="40" y="20" width="16" height="60" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="48" y1="15" x2="48" y2="85" stroke="#16A34A"/>
      <line x1="90" y1="25" x2="106" y2="25" stroke="#333" stroke-width="2"/>
      <line x1="98" y1="18" x2="98" y2="35" stroke="#333"/>
      <rect x="140" y="20" width="16" height="60" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="148" y1="15" x2="148" y2="85" stroke="#7F1D1D"/>
      <text x="30" y="95" font-size="7" fill="#16A34A">상승</text>
      <text x="78" y="95" font-size="7" fill="#666">정지</text>
      <text x="130" y="95" font-size="7" fill="#DC2626">하락</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 3봉 — 큰 양봉 → 작은 봉 (gap up, 도지 가능) → 큰 흑봉. 흑봉이 첫 양봉의 중간 이하.</p>
    <p><strong>신호:</strong> 강한 매도 신호. 상승 추세 정점 확인.</p>

"적삼병":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="40" y="55" width="14" height="25" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="47" y1="50" x2="47" y2="83" stroke="#16A34A"/>
      <rect x="80" y="40" width="14" height="30" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="87" y1="35" x2="87" y2="73" stroke="#16A34A"/>
      <rect x="120" y="25" width="14" height="30" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="127" y1="20" x2="127" y2="58" stroke="#16A34A"/>
      <text x="50" y="95" font-size="7" fill="#16A34A">3연속 양봉 (계단식 상승)</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 3개 연속 양봉이 계단식으로 상승. 각 봉의 종가가 이전 봉 안에서 열고 새 고가로 마감.</p>
    <p><strong>신호:</strong> 강한 매수 — 추세 시작. 다만 과매수 구간 (RSI 70+) 에선 단기 조정 가능.</p>

"흑삼병":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="40" y="20" width="14" height="25" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="47" y1="15" x2="47" y2="50" stroke="#7F1D1D"/>
      <rect x="80" y="30" width="14" height="30" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="87" y1="25" x2="87" y2="65" stroke="#7F1D1D"/>
      <rect x="120" y="45" width="14" height="30" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="127" y1="40" x2="127" y2="80" stroke="#7F1D1D"/>
      <text x="50" y="95" font-size="7" fill="#DC2626">3연속 흑봉 (계단식 하락)</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 3개 연속 흑봉이 계단식으로 하락. 적삼병의 반대.</p>
    <p><strong>신호:</strong> 강한 매도. 하락 추세 시작 시그널.</p>

"관통형":
  signal_typical: 매수
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="60" y="20" width="18" height="55" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="69" y1="15" x2="69" y2="80" stroke="#7F1D1D"/>
      <rect x="105" y="45" width="18" height="35" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="114" y1="40" x2="114" y2="85" stroke="#16A34A"/>
      <line x1="58" y1="48" x2="123" y2="48" stroke="#999" stroke-dasharray="2,2"/>
      <text x="40" y="95" font-size="7" fill="#666">긴 흑봉</text>
      <text x="95" y="95" font-size="7" fill="#16A34A">양봉이 흑봉 중간 이상 관통</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 긴 흑봉 다음에 양봉이 흑봉의 중간 이상까지 관통. Gap down 으로 시작 → 강한 매수.</p>
    <p><strong>신호:</strong> 매수. 하락 추세 끝 반전 신호.</p>

"먹구름":
  signal_typical: 매도
  svg: |
    <svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
      <rect x="60" y="20" width="18" height="55" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
      <line x1="69" y1="15" x2="69" y2="80" stroke="#16A34A"/>
      <rect x="105" y="20" width="18" height="35" fill="#DC2626" stroke="#7F1D1D"/>
      <line x1="114" y1="15" x2="114" y2="60" stroke="#7F1D1D"/>
      <line x1="58" y1="47" x2="123" y2="47" stroke="#999" stroke-dasharray="2,2"/>
      <text x="40" y="95" font-size="7" fill="#16A34A">긴 양봉</text>
      <text x="95" y="95" font-size="7" fill="#DC2626">흑봉이 양봉 중간 이하 관통</text>
    </svg>
  description_html: |
    <p><strong>형성:</strong> 긴 양봉 다음에 흑봉이 양봉의 중간 이하까지 관통. 관통형의 반대.</p>
    <p><strong>신호:</strong> 매도. 상승 추세 끝 반전 신호.</p>
```

- [ ] **Step 2: 로드 확인**

```bash
.venv/bin/python -c "
from src import pattern_metadata as pm
pm.reset_cache()
data = pm.load_metadata()
print('total:', len(data))
expected = ['도지', '잠자리 도지', '묘비 도지', '망치', '역망치', '교수형', '유성',
            '장악형', '잉태형', '잉태형 십자', '새벽의 샛별', '저녁별', '적삼병', '흑삼병', '관통형', '먹구름']
for p in expected:
    assert p in data, f'{p} 없음'
print('Tier1 candle 15+ 패턴 OK')
"
```

Expected: `total: 21`, `Tier1 candle 15+ 패턴 OK`

- [ ] **Step 3: 테스트 재실행 (기존 테스트 깨지지 않는지)**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py -v
```

Expected: 6 PASS

- [ ] **Step 4: 커밋**

```bash
git add src/data/pattern_metadata.yaml
git commit -m "feat(pattern-modal): Tier1 candle 패턴 15종 YAML 콘텐츠

- 도지/잠자리도지/묘비도지 (3)
- 망치/역망치/교수형/유성 (4)
- 장악형/잉태형십자 (2 — 잉태형은 stub 으로 이미)
- 새벽의 샛별/저녁별 (2)
- 적삼병/흑삼병 (2)
- 관통형/먹구름 (2)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: YAML 콘텐츠 — Tier 2 generic 템플릿 + 나머지 ~40 패턴

**Files:**
- Modify: `src/data/pattern_metadata.yaml`
- Modify: `src/pattern_metadata.py` (generic fallback 처리)

전략: 6개 generic SVG 템플릿 — bullish_reversal, bearish_reversal, bullish_continuation, bearish_continuation, neutral, doji_variant. 나머지 ~40 패턴 (CDLTAKURI, CDLBELTHOLD, CDLBREAKAWAY, CDLINNECK 등) 은 yaml 에 이름만 등록하고 `tier: 2` 표시 + 해당 generic 템플릿 키 참조.

- [ ] **Step 1: pattern_metadata.py 에 Tier 2 처리 로직 추가**

`src/pattern_metadata.py` 의 `lookup()` 함수 수정 — `tier: 2` entry 면 `generic_template` 참조해서 동적 합성:

```python
# (기존 imports + 변수 + _parse_and_validate + load_metadata + reset_cache 유지)

_GENERIC_TEMPLATES = {
    "bullish_reversal": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="50" y="25" width="14" height="50" fill="#DC2626" stroke="#7F1D1D"/>
            <line x1="57" y1="20" x2="57" y2="80" stroke="#7F1D1D"/>
            <rect x="90" y="35" width="14" height="40" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <line x1="97" y1="30" x2="97" y2="80" stroke="#16A34A"/>
            <path d="M 130 50 Q 150 30, 170 35" fill="none" stroke="#16A34A" stroke-width="2" stroke-dasharray="3,2"/>
            <polygon points="170,35 165,30 165,40" fill="#16A34A"/>
            <text x="40" y="95" font-size="7" fill="#666">하락</text>
            <text x="135" y="25" font-size="7" fill="#16A34A">매수 반전</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매수 반전 패턴. 하락 추세 끝에서 매수 신호를 시사하는 봉 조합.</p>"
                          + "<p>구체 검출 조건은 talib 의 해당 CDL 코드 정의를 따름.</p>",
    },
    "bearish_reversal": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="50" y="25" width="14" height="50" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <line x1="57" y1="20" x2="57" y2="80" stroke="#16A34A"/>
            <rect x="90" y="35" width="14" height="40" fill="#DC2626" stroke="#7F1D1D"/>
            <line x1="97" y1="30" x2="97" y2="80" stroke="#7F1D1D"/>
            <path d="M 130 50 Q 150 70, 170 65" fill="none" stroke="#DC2626" stroke-width="2" stroke-dasharray="3,2"/>
            <polygon points="170,65 165,60 165,70" fill="#DC2626"/>
            <text x="40" y="95" font-size="7" fill="#666">상승</text>
            <text x="135" y="85" font-size="7" fill="#DC2626">매도 반전</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매도 반전 패턴. 상승 추세 끝에서 매도 신호를 시사하는 봉 조합.</p>"
                          + "<p>구체 검출 조건은 talib 의 해당 CDL 코드 정의를 따름.</p>",
    },
    "bullish_continuation": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="30" y="55" width="12" height="25" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="60" y="45" width="12" height="20" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="90" y="50" width="12" height="15" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="120" y="35" width="12" height="30" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="150" y="20" width="12" height="35" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <text x="30" y="95" font-size="7" fill="#16A34A">매수 추세 지속</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매수 추세 지속 패턴. 일시 조정 후 상승 추세 재개.</p>",
    },
    "bearish_continuation": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="30" y="20" width="12" height="25" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="60" y="30" width="12" height="20" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="90" y="35" width="12" height="15" fill="#fff" stroke="#16A34A" stroke-width="1.5"/>
            <rect x="120" y="40" width="12" height="30" fill="#DC2626" stroke="#7F1D1D"/>
            <rect x="150" y="50" width="12" height="35" fill="#DC2626" stroke="#7F1D1D"/>
            <text x="30" y="95" font-size="7" fill="#DC2626">매도 추세 지속</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 매도 추세 지속 패턴. 일시 반등 후 하락 추세 재개.</p>",
    },
    "neutral": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <rect x="80" y="40" width="14" height="20" fill="#fff" stroke="#666" stroke-width="1.5"/>
            <line x1="87" y1="35" x2="87" y2="65" stroke="#666"/>
            <rect x="106" y="40" width="14" height="20" fill="#DC2626" stroke="#7F1D1D"/>
            <line x1="113" y1="35" x2="113" y2="65" stroke="#7F1D1D"/>
            <text x="40" y="95" font-size="7" fill="#666">방향 불명 (관망)</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 방향성 불명. 다른 지표와 결합하여 판단 필요.</p>",
    },
    "doji_variant": {
        "svg": '''<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
            <line x1="100" y1="20" x2="100" y2="80" stroke="#333" stroke-width="1"/>
            <line x1="80" y1="50" x2="120" y2="50" stroke="#333" stroke-width="3"/>
            <text x="40" y="95" font-size="7" fill="#666">도지 변종 — 시가 ≈ 종가</text>
        </svg>''',
        "description_html": "<p><strong>유형:</strong> 도지 변종. 시가와 종가가 거의 같은 균형 봉 — 추세 정지 시사.</p>",
    },
}


def lookup(pattern_name: str) -> dict[str, Any] | None:
    """패턴 이름 → entry. Tier 2 면 generic template 합성."""
    data = load_metadata()
    entry = data.get(pattern_name)
    if entry is None:
        return None
    # Tier 2: generic_template 키만 있고 svg/description 없음 → 템플릿 합성
    if entry.get("tier") == 2:
        template_key = entry.get("generic_template")
        template = _GENERIC_TEMPLATES.get(template_key)
        if template is None:
            logger.warning("pattern %s: generic_template %s not found", pattern_name, template_key)
            return None
        return {
            "svg": template["svg"],
            "description_html": (
                f'<p><strong>{pattern_name}</strong> ({entry.get("description_short", "")})</p>'
                + template["description_html"]
            ),
            "signal_typical": entry.get("signal_typical", "관망"),
            "tier": 2,
        }
    return entry
```

- [ ] **Step 2: 새 테스트 추가**

`tests/test_pattern_metadata.py` 끝에 추가:

```python
def test_lookup_tier2_synthesizes_from_generic_template():
    """tier:2 entry 는 generic template 합성."""
    pm.reset_cache()
    # _GENERIC_TEMPLATES 검증: 6 종류 있어야 함
    assert set(pm._GENERIC_TEMPLATES.keys()) == {
        "bullish_reversal", "bearish_reversal",
        "bullish_continuation", "bearish_continuation",
        "neutral", "doji_variant",
    }


def test_lookup_unknown_generic_template_returns_none():
    """잘못된 generic_template 키면 None + warning."""
    # 직접 _cache 조작 (테스트 격리)
    pm._cache = {
        "x": {"tier": 2, "generic_template": "nonexistent", "signal_typical": "관망"}
    }
    assert pm.lookup("x") is None
    pm.reset_cache()
```

- [ ] **Step 3: 테스트 통과 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py -v
```

Expected: 8 PASS

- [ ] **Step 4: 나머지 candle 패턴 ~40종 YAML 등록**

`src/data/pattern_metadata.yaml` 끝에 추가 (Tier 2):

```yaml
# === Tier 2: generic templates ===
"긴다리 도지":
  tier: 2
  generic_template: doji_variant
  signal_typical: 관망
  description_short: 위·아래꼬리 모두 긴 도지

"도지스타":
  tier: 2
  generic_template: doji_variant
  signal_typical: 관망
  description_short: gap 후 도지 출현 (반전 후보)

"새벽 도지스타":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 새벽의 샛별의 도지 버전 (반전 강도 ↑)

"저녁 도지스타":
  tier: 2
  generic_template: bearish_reversal
  signal_typical: 매도
  description_short: 저녁별의 도지 버전 (반전 강도 ↑)

"3봉 내부":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 3봉 내포 (반전 후보)

"3봉 외부":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 3봉 포함 (반전 후보)

"3선 타격":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 3연속 봉 + 반대색 큰 봉

"남쪽 3성":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 3개 흑봉 점진 감소 (반전 시사)

"버려진 아이":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: gap 으로 고립된 도지 (강한 반전)

"두 까마귀":
  tier: 2
  generic_template: bearish_reversal
  signal_typical: 매도
  description_short: 양봉 + 2 흑봉 (gap up 후 하락)

"상승갭 두 까마귀":
  tier: 2
  generic_template: bearish_reversal
  signal_typical: 매도
  description_short: gap up 후 2 흑봉 (상승 추세 약화)

"타구리":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 망치형 변종 (긴 아래꼬리)

"팽이":
  tier: 2
  generic_template: neutral
  signal_typical: 관망
  description_short: 작은 몸통 + 양쪽 꼬리 (균형)

"장대봉":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 꼬리 없는 큰 봉 (강한 한방향 압력)

"벨트홀드":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 시가 = 고가/저가 + 반대 방향 진행

"이탈형":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: gap 으로 추세 이탈 (5봉 패턴)

"종가 장대봉":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 종가 = 고가/저가 인 장대봉

"아기벽 가림":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 흑봉 + 가림 흑봉 (반전 후보)

"반격선":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 반대 색 봉 + 종가 동일

"사이드 갭 양봉":
  tier: 2
  generic_template: bullish_continuation
  signal_typical: 매수
  description_short: gap 후 2 양봉 (상승 지속)

"고파동":
  tier: 2
  generic_template: neutral
  signal_typical: 관망
  description_short: 매우 긴 위·아래꼬리 (변동성 높음)

"히카케":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: inside bar 함정 (반전 후보)

"수정 히카케":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 히카케 패턴의 추가 확인 봉 포함

"귀환 비둘기":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 하락 추세 끝 작은 양봉 (반전)

"동일 3까마귀":
  tier: 2
  generic_template: bearish_continuation
  signal_typical: 매도
  description_short: 같은 종가 3 흑봉 (하락 지속)

"인넥":
  tier: 2
  generic_template: bearish_continuation
  signal_typical: 매도
  description_short: 흑봉 + 양봉이 흑봉 저가 근처

"킥킹":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 반대 색 marubozu 연속 (강한 반전)

"킥킹 by 길이":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 킥킹 + 둘 중 긴 봉의 색이 신호 방향

"사다리 바닥":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 4 하락 흑봉 + 위꼬리 흑봉 + 양봉

"장대선":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 매우 긴 봉 (강한 한방향)

"매칭 저점":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 2 흑봉 종가 일치 (지지 형성)

"매트홀드":
  tier: 2
  generic_template: bullish_continuation
  signal_typical: 매수
  description_short: 양봉 + 작은 흑봉들 + 양봉 (상승 재개)

"온넥":
  tier: 2
  generic_template: bearish_continuation
  signal_typical: 매도
  description_short: 흑봉 + 양봉이 흑봉 저가 도달

"인력거꾼":
  tier: 2
  generic_template: doji_variant
  signal_typical: 관망
  description_short: 긴다리 도지 변종

"상승/하락 삼법":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 추세 + 3 작은 반대봉 + 추세 (지속)

"분리선":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: 반대 색 봉 + 시가 동일

"단봉":
  tier: 2
  generic_template: neutral
  signal_typical: 관망
  description_short: 매우 짧은 봉 (변동 미미)

"정체 패턴":
  tier: 2
  generic_template: bearish_reversal
  signal_typical: 매도
  description_short: 양봉 연속 후 작은 양봉 (정점 시사)

"막대 샌드위치":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 흑봉 + 양봉 + 흑봉 (반전)

"타스키 갭":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: gap + 반대 색 봉 (gap 부분 채움)

"끼움형":
  tier: 2
  generic_template: bearish_continuation
  signal_typical: 매도
  description_short: 흑봉 + 양봉이 흑봉 중간 미만 (하락 지속)

"3성":
  tier: 2
  generic_template: doji_variant
  signal_typical: varies
  description_short: 3 도지 연속 (매우 드문 반전)

"유니크 3강":
  tier: 2
  generic_template: bullish_reversal
  signal_typical: 매수
  description_short: 3 흑봉 + 마지막 흑봉 안의 작은 흑봉 (반전)

"사이드 갭 3법":
  tier: 2
  generic_template: neutral
  signal_typical: varies
  description_short: gap + 3 작은 봉 + gap 메꿈 (지속)
```

- [ ] **Step 5: 전체 로드 + Tier2 합성 검증**

```bash
.venv/bin/python -c "
from src import pattern_metadata as pm
pm.reset_cache()
data = pm.load_metadata()
print('total patterns:', len(data))
# Tier 2 합성 검증
for name in ['긴다리 도지', '타구리', '인넥', '매트홀드']:
    e = pm.lookup(name)
    assert e is not None, f'{name} synthesis 실패'
    assert '<svg' in e['svg'], f'{name} svg 누락'
    print(f'  ✓ {name}: tier={e.get(\"tier\")}, signal={e[\"signal_typical\"]}')
"
```

Expected: `total patterns: ~64`, 4 Tier 2 패턴 OK 출력

- [ ] **Step 6: 테스트 + 커밋**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py -v
```

Expected: 8 PASS

```bash
git add src/data/pattern_metadata.yaml src/pattern_metadata.py tests/test_pattern_metadata.py
git commit -m "feat(pattern-modal): Tier2 generic templates + ~40 캔들 패턴

- _GENERIC_TEMPLATES 6 종 (bullish/bearish reversal/continuation + neutral + doji)
- lookup() 가 tier:2 entry 면 template 합성
- 나머지 40+ candle 패턴 yaml 등록 (이름 + signal_typical + description_short + template 키)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: pattern_popup.py — 실제 차트 빌더

**Files:**
- Create: `src/pattern_popup.py`
- Create: `tests/test_pattern_popup.py`

- [ ] **Step 1: Failing 테스트 작성**

Create `tests/test_pattern_popup.py`:

```python
"""pattern_popup.py 단위 테스트."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src import pattern_popup as pp


@pytest.fixture(autouse=True)
def reset_cache():
    pp._chart_cache.clear()
    yield
    pp._chart_cache.clear()


def test_find_detection_chart_pattern_latest():
    """date 미지정 시 가장 최근 chart_pattern 반환."""
    pj = {
        "chart_patterns": [
            {"name": "더블바텀(W)", "to_date": "2026-04-01", "low1": {}, "low2": {}},
            {"name": "더블바텀(W)", "to_date": "2026-05-10", "low1": {}, "low2": {}},
            {"name": "더블탑(M)", "to_date": "2026-05-12", "high1": {}, "high2": {}},
        ],
        "candles": [],
    }
    d = pp.find_detection(pj, "더블바텀(W)", date=None)
    assert d is not None
    assert d["to_date"] == "2026-05-10"


def test_find_detection_candle_by_date():
    """date 지정 시 정확 매칭."""
    pj = {
        "chart_patterns": [],
        "candles": [
            {"name": "잉태형", "date": "2026-05-10", "signal": "매수"},
            {"name": "잉태형", "date": "2026-05-13", "signal": "매수"},
        ],
    }
    d = pp.find_detection(pj, "잉태형", date="2026-05-10")
    assert d is not None
    assert d["date"] == "2026-05-10"


def test_find_detection_returns_none_when_pattern_absent():
    pj = {"chart_patterns": [], "candles": []}
    assert pp.find_detection(pj, "더블바텀(W)", date=None) is None


def test_build_chart_caption_chart_pattern():
    detection = {
        "name": "더블바텀(W)",
        "low1": {"date": "2026-04-15", "price": 65000},
        "low2": {"date": "2026-05-08", "price": 65500},
        "neckline": 68000,
        "current": 70000,
        "breakout": True,
    }
    caption = pp._build_caption(detection)
    assert "65000" in caption or "65,000" in caption
    assert "넥라인" in caption or "68000" in caption or "68,000" in caption
    assert "돌파" in caption


def test_build_chart_caption_candle_pattern():
    detection = {"name": "잉태형", "date": "2026-05-13", "signal": "매수"}
    caption = pp._build_caption(detection)
    assert "2026-05-13" in caption
    assert "잉태형" in caption


def test_render_chart_with_mock_data():
    """매칭 데이터 + yfinance mock → base64 PNG 응답."""
    ohlc = pd.DataFrame({
        "Open":  [100.0] * 60,
        "High":  [110.0] * 60,
        "Low":   [90.0]  * 60,
        "Close": [105.0] * 60,
        "Volume": [1000] * 60,
    }, index=pd.date_range("2026-03-15", periods=60))
    detection = {
        "name": "더블바텀(W)",
        "low1": {"date": "2026-04-15", "price": 95},
        "low2": {"date": "2026-05-08", "price": 96},
        "neckline": 105,
        "current": 110,
        "breakout": True,
        "from_date": "2026-04-15",
        "to_date": "2026-05-08",
    }
    with patch.object(pp, "_fetch_ohlc", return_value=ohlc):
        result = pp.build_actual_chart("005930.KS", "더블바텀(W)", date=None, pattern_json={
            "chart_patterns": [detection],
            "candles": [],
        })
    assert result["chart_b64"] is not None
    assert len(result["chart_b64"]) > 100  # base64 PNG
    assert "caption" in result
    assert result["signal_at_detection"] == "매수"


def test_render_chart_returns_null_when_ohlc_empty():
    """OHLC fetch 실패 → null + caption."""
    detection = {"name": "잉태형", "date": "2026-05-13", "signal": "매수"}
    with patch.object(pp, "_fetch_ohlc", return_value=pd.DataFrame()):
        result = pp.build_actual_chart("005930.KS", "잉태형", date="2026-05-13", pattern_json={
            "chart_patterns": [],
            "candles": [detection],
        })
    assert result["chart_b64"] is None
    assert "차트 데이터 없음" in result["caption"]


def test_lru_cache_hit_on_second_call():
    """동일 (symbol, pattern, date) 두 번째 호출 시 _fetch_ohlc 미호출."""
    ohlc = pd.DataFrame({
        "Open": [100.0] * 60, "High": [110.0] * 60,
        "Low": [90.0] * 60, "Close": [105.0] * 60, "Volume": [1000] * 60,
    }, index=pd.date_range("2026-03-15", periods=60))
    detection = {"name": "잉태형", "date": "2026-05-13", "signal": "매수"}
    pj = {"chart_patterns": [], "candles": [detection]}
    
    mock = MagicMock(return_value=ohlc)
    with patch.object(pp, "_fetch_ohlc", mock):
        pp.build_actual_chart("005930.KS", "잉태형", date="2026-05-13", pattern_json=pj)
        pp.build_actual_chart("005930.KS", "잉태형", date="2026-05-13", pattern_json=pj)
    assert mock.call_count == 1  # 두 번째는 cache hit
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_popup.py -v
```

Expected: ALL FAIL with `ModuleNotFoundError: No module named 'src.pattern_popup'`

- [ ] **Step 3: pattern_popup.py 구현**

Create `src/pattern_popup.py`:

```python
"""실제 차트 탭 데이터 빌더 — matplotlib 으로 패턴 마킹 차트 생성."""
from __future__ import annotations

import base64
import io
import logging
import time
from collections import OrderedDict
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# 메모리 LRU cache — key: (symbol, pattern, date), value: (result_dict, expires_at)
_chart_cache: OrderedDict[tuple[str, str, str | None], tuple[dict, float]] = OrderedDict()
_CACHE_MAX = 128
_CACHE_TTL = 3600  # 1 hour


def _fetch_ohlc(symbol: str) -> pd.DataFrame:
    """yfinance 60일 OHLC fetch. 실패 시 빈 DF."""
    try:
        df = yf.Ticker(symbol).history(period="60d")
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        logger.warning("yfinance fetch %s 실패: %s", symbol, e)
        return pd.DataFrame()


def find_detection(
    pattern_json: dict, pattern_name: str, date: str | None
) -> dict | None:
    """pattern_json 안에서 (pattern_name, date) 검출 row 찾기.
    
    date 미지정 시: 가장 최근 (to_date 또는 date 기준).
    """
    candidates: list[dict] = []
    for cp in pattern_json.get("chart_patterns") or []:
        if cp.get("name") == pattern_name:
            candidates.append({**cp, "_kind": "chart", "_sort_date": cp.get("to_date", "")})
    for c in pattern_json.get("candles") or []:
        if c.get("name") == pattern_name:
            candidates.append({**c, "_kind": "candle", "_sort_date": c.get("date", "")})
    if not candidates:
        return None
    if date is None:
        candidates.sort(key=lambda x: x["_sort_date"], reverse=True)
        return candidates[0]
    for c in candidates:
        if c["_sort_date"] == date:
            return c
    return None


def _build_caption(detection: dict) -> str:
    """검출 dict → 사람이 읽을 caption."""
    name = detection.get("name", "")
    if detection.get("_kind") == "chart":
        if name == "더블바텀(W)":
            l1 = detection.get("low1", {})
            l2 = detection.get("low2", {})
            neck = detection.get("neckline")
            br = "돌파" if detection.get("breakout") else "미돌파"
            return (
                f"저점1 {l1.get('date','')} {l1.get('price','?'):,} → "
                f"저점2 {l2.get('date','')} {l2.get('price','?'):,} "
                f"(넥라인 {neck:,} {br})"
            ) if l1 and l2 else detection.get("details", name)
        if name == "더블탑(M)":
            h1 = detection.get("high1", {})
            h2 = detection.get("high2", {})
            neck = detection.get("neckline")
            br = "이탈" if detection.get("breakdown") else "유지"
            return (
                f"고점1 {h1.get('date','')} {h1.get('price','?'):,} → "
                f"고점2 {h2.get('date','')} {h2.get('price','?'):,} "
                f"(넥라인 {neck:,} {br})"
            ) if h1 and h2 else detection.get("details", name)
        if name in ("헤드앤숄더", "역헤드앤숄더"):
            ls = detection.get("left_shoulder", {})
            h = detection.get("head", {})
            rs = detection.get("right_shoulder", {})
            return (
                f"좌어깨 {ls.get('date','')} {ls.get('price','?'):,} / "
                f"헤드 {h.get('date','')} {h.get('price','?'):,} / "
                f"우어깨 {rs.get('date','')} {rs.get('price','?'):,}"
            ) if ls and h and rs else detection.get("details", name)
        return detection.get("details", name)
    # candle
    date_str = detection.get("date", "")
    signal = detection.get("signal", "")
    return f"{date_str} — {name} ({signal})"


def _render_chart(ohlc: pd.DataFrame, detection: dict) -> str:
    """OHLC + detection → matplotlib chart → base64 PNG."""
    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    dates = ohlc.index
    ax.plot(dates, ohlc["Close"], color="#1E40AF", linewidth=1.5, label="Close")
    
    kind = detection.get("_kind")
    if kind == "chart":
        # chart pattern markers
        for coord_key, color_label in [
            ("low1", ("#16A34A", "저점1")),
            ("low2", ("#16A34A", "저점2")),
            ("high1", ("#DC2626", "고점1")),
            ("high2", ("#DC2626", "고점2")),
            ("left_shoulder", ("#7C3AED", "좌어깨")),
            ("head", ("#7C3AED", "헤드")),
            ("right_shoulder", ("#7C3AED", "우어깨")),
        ]:
            pt = detection.get(coord_key)
            if pt and pt.get("date") and pt.get("price") is not None:
                try:
                    d = pd.to_datetime(pt["date"]).tz_localize(dates.tz) if dates.tz else pd.to_datetime(pt["date"])
                    ax.plot(d, pt["price"], "o", color=color_label[0], markersize=8)
                    ax.annotate(color_label[1], (d, pt["price"]),
                                textcoords="offset points", xytext=(5, 5), fontsize=8)
                except Exception as e:
                    logger.debug("marker %s 실패: %s", coord_key, e)
        # neckline
        neck = detection.get("neckline")
        if neck is not None:
            ax.axhline(neck, color="#999", linestyle="--", linewidth=1)
        # detection range box
        from_d = detection.get("from_date")
        to_d = detection.get("to_date")
        if from_d and to_d:
            try:
                d1 = pd.to_datetime(from_d).tz_localize(dates.tz) if dates.tz else pd.to_datetime(from_d)
                d2 = pd.to_datetime(to_d).tz_localize(dates.tz) if dates.tz else pd.to_datetime(to_d)
                ax.axvspan(d1, d2, alpha=0.15, color="#FBBF24")
            except Exception as e:
                logger.debug("range box 실패: %s", e)
    elif kind == "candle":
        # candle pattern: vertical line at detection date
        det_date = detection.get("date")
        if det_date:
            try:
                d = pd.to_datetime(det_date).tz_localize(dates.tz) if dates.tz else pd.to_datetime(det_date)
                ax.axvline(d, color="#DC2626" if detection.get("signal") == "매도" else "#16A34A",
                           linestyle="--", linewidth=1.5)
                # 라벨
                price_at = ohlc.loc[ohlc.index >= d, "Close"]
                if not price_at.empty:
                    ax.annotate(f"↓ {detection.get('name','')}", (d, price_at.iloc[0]),
                                textcoords="offset points", xytext=(5, 15), fontsize=9,
                                fontweight="bold",
                                color="#DC2626" if detection.get("signal") == "매도" else "#16A34A")
            except Exception as e:
                logger.debug("candle marker 실패: %s", e)
    
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def build_actual_chart(
    symbol: str, pattern: str, date: str | None, pattern_json: dict
) -> dict[str, Any]:
    """모달의 "실제 차트" 탭 데이터 빌드.
    
    Returns:
        {"chart_b64": str|None, "caption": str, "signal_at_detection": str|None,
         "symbol": str, "pattern": str, "date": str|None}
    """
    cache_key = (symbol, pattern, date)
    now = time.time()
    
    # cache check
    cached = _chart_cache.get(cache_key)
    if cached is not None:
        result, expires_at = cached
        if expires_at > now:
            _chart_cache.move_to_end(cache_key)
            return result
        del _chart_cache[cache_key]
    
    detection = find_detection(pattern_json, pattern, date)
    if detection is None:
        return {
            "chart_b64": None, "caption": "검출 데이터 없음",
            "signal_at_detection": None, "symbol": symbol, "pattern": pattern, "date": date,
        }
    
    ohlc = _fetch_ohlc(symbol)
    if ohlc.empty:
        result = {
            "chart_b64": None, "caption": "차트 데이터 없음 — yfinance fetch 실패",
            "signal_at_detection": detection.get("signal"),
            "symbol": symbol, "pattern": pattern, "date": date,
        }
    else:
        try:
            png_b64 = _render_chart(ohlc, detection)
            result = {
                "chart_b64": png_b64,
                "caption": _build_caption(detection),
                "signal_at_detection": detection.get("signal"),
                "symbol": symbol, "pattern": pattern, "date": date,
            }
        except Exception as e:
            logger.exception("차트 렌더 실패 %s %s: %s", symbol, pattern, e)
            result = {
                "chart_b64": None, "caption": "차트 데이터 없음 — 렌더 실패",
                "signal_at_detection": detection.get("signal"),
                "symbol": symbol, "pattern": pattern, "date": date,
            }
    
    # cache store + LRU eviction
    _chart_cache[cache_key] = (result, now + _CACHE_TTL)
    if len(_chart_cache) > _CACHE_MAX:
        _chart_cache.popitem(last=False)
    return result
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_popup.py -v
```

Expected: 8 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/pattern_popup.py tests/test_pattern_popup.py
git commit -m "feat(pattern-modal): pattern_popup — 실제 차트 빌더 + LRU cache

- find_detection(): chart_patterns/candles 에서 (pattern, date) 매칭
- _build_caption(): 검출 dict → 한글 caption
- _render_chart(): matplotlib 1-subplot + 마커/넥라인/범위박스/세로선
- build_actual_chart(): 엔트리 함수 + memory LRU (TTL 1h, max 128)
- 8 단위 테스트 (mocked yfinance + matplotlib)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Flask 라우트 추가 — /api/pattern-popup/{textbook,actual}

**Files:**
- Modify: `src/web_app.py` (라우트 2개 추가)
- Create: `tests/test_pattern_routes.py`

- [ ] **Step 1: Failing 라우트 테스트 작성**

Create `tests/test_pattern_routes.py`:

```python
"""/api/pattern-popup/* 라우트 통합 테스트."""
from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from src import pattern_metadata as pm
from src import pattern_popup as pp
from src.web_app import app


@pytest.fixture(autouse=True)
def reset_caches():
    pm.reset_cache()
    pp._chart_cache.clear()
    yield


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_textbook_returns_known_pattern(client):
    resp = client.get("/api/pattern-popup/textbook?pattern=더블바텀(W)")
    assert resp.status_code == 200
    j = resp.get_json()
    assert j["pattern"] == "더블바텀(W)"
    assert "<svg" in j["svg"]
    assert "<p" in j["description_html"]
    assert j["signal_typical"] == "매수"


def test_textbook_returns_404_for_unknown(client):
    resp = client.get("/api/pattern-popup/textbook?pattern=없는패턴XYZ")
    assert resp.status_code == 404


def test_textbook_missing_pattern_param(client):
    resp = client.get("/api/pattern-popup/textbook")
    assert resp.status_code == 400


def test_actual_with_mocked_data(client, monkeypatch):
    """analysis cache 에서 pattern_json 가져와서 actual chart 응답."""
    pj = {
        "chart_patterns": [{
            "name": "더블바텀(W)",
            "to_date": "2026-05-10",
            "from_date": "2026-04-15",
            "low1": {"date": "2026-04-15", "price": 95},
            "low2": {"date": "2026-05-08", "price": 96},
            "neckline": 105, "current": 110, "breakout": True,
        }],
        "candles": [],
    }
    ohlc = pd.DataFrame({
        "Open": [100.0]*60, "High": [110.0]*60,
        "Low": [90.0]*60, "Close": [105.0]*60, "Volume":[1000]*60,
    }, index=pd.date_range("2026-03-15", periods=60))
    
    # mock cache_row fetch + yfinance
    monkeypatch.setattr(pp, "_fetch_ohlc", lambda s: ohlc)
    monkeypatch.setattr(
        "src.web_app._fetch_pattern_json_for_symbol",
        lambda sym: pj,
    )
    
    resp = client.get("/api/pattern-popup/actual?symbol=005930.KS&pattern=더블바텀(W)")
    assert resp.status_code == 200
    j = resp.get_json()
    assert j["symbol"] == "005930.KS"
    assert j["chart_b64"] is not None
    assert "넥라인" in j["caption"] or "돌파" in j["caption"]


def test_actual_returns_null_chart_when_no_detection(client, monkeypatch):
    """검출 없음 → 200 + null."""
    monkeypatch.setattr(
        "src.web_app._fetch_pattern_json_for_symbol",
        lambda sym: {"chart_patterns": [], "candles": []},
    )
    resp = client.get("/api/pattern-popup/actual?symbol=005930.KS&pattern=더블바텀(W)")
    assert resp.status_code == 200
    j = resp.get_json()
    assert j["chart_b64"] is None


def test_actual_missing_required_params(client):
    resp = client.get("/api/pattern-popup/actual?symbol=005930.KS")  # pattern 누락
    assert resp.status_code == 400
    resp = client.get("/api/pattern-popup/actual?pattern=잉태형")  # symbol 누락
    assert resp.status_code == 400


def test_actual_returns_404_when_no_cache_row(client, monkeypatch):
    """analysis cache row 자체 없으면 404."""
    monkeypatch.setattr(
        "src.web_app._fetch_pattern_json_for_symbol",
        lambda sym: None,
    )
    resp = client.get("/api/pattern-popup/actual?symbol=UNKNOWN.KS&pattern=더블바텀(W)")
    assert resp.status_code == 404
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_routes.py -v
```

Expected: ALL FAIL (라우트 미구현)

- [ ] **Step 3: 라우트 + 헬퍼 추가**

먼저 web_app.py 에 헬퍼 함수 추가할 위치 확인:

```bash
grep -n '^def ' src/web_app.py | head -20
```

`src/web_app.py` 의 import 섹션 (상단) 에 추가:

```python
from src import pattern_metadata as _pattern_meta
from src import pattern_popup as _pattern_popup
```

같은 파일 어느 헬퍼 함수 영역 (다른 라우트 정의 부근) 에 추가:

```python
def _fetch_pattern_json_for_symbol(symbol: str) -> dict | None:
    """analysis cache 에서 symbol 의 pattern_json 가져오기.
    
    Returns:
        파싱된 dict or None (cache row 없음 또는 pattern_json 컬럼 비어있음).
    """
    import json as _json
    import sqlite3
    from src.analysis_cache import _DB_PATH  # 기존 cache DB 경로
    
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT pattern_json FROM analysis_cache WHERE symbol = ?", (symbol,))
        row = cur.fetchone()
        conn.close()
        if row is None or row[0] is None:
            return None
        return _json.loads(row[0])
    except Exception as e:
        logger.warning("_fetch_pattern_json_for_symbol %s 실패: %s", symbol, e)
        return None


@app.route("/api/pattern-popup/textbook", methods=["GET"])
def api_pattern_popup_textbook():
    """교과서 탭 — 패턴별 정적 SVG + 설명."""
    pattern = request.args.get("pattern")
    if not pattern:
        return jsonify({"error": "pattern parameter required"}), 400
    entry = _pattern_meta.lookup(pattern)
    if entry is None:
        return jsonify({"error": f"unknown pattern: {pattern}"}), 404
    return jsonify({
        "pattern": pattern,
        "svg": entry["svg"],
        "description_html": entry["description_html"],
        "signal_typical": entry["signal_typical"],
    })


@app.route("/api/pattern-popup/actual", methods=["GET"])
def api_pattern_popup_actual():
    """실제 차트 탭 — 종목의 해당 패턴 검출 위치 마킹 차트."""
    symbol = request.args.get("symbol")
    pattern = request.args.get("pattern")
    date = request.args.get("date")  # optional
    if not symbol or not pattern:
        return jsonify({"error": "symbol and pattern required"}), 400
    
    pattern_json = _fetch_pattern_json_for_symbol(symbol)
    if pattern_json is None:
        return jsonify({"error": f"no analysis cache for symbol: {symbol}"}), 404
    
    result = _pattern_popup.build_actual_chart(symbol, pattern, date, pattern_json)
    return jsonify(result)
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
.venv/bin/python -m pytest tests/test_pattern_routes.py -v
```

Expected: 7 PASS

- [ ] **Step 5: 기존 web_app 테스트 회귀 점검**

```bash
.venv/bin/python -m pytest tests/test_web_app.py -v 2>&1 | tail -10
```

Expected: 118+ PASS (이전 횟수와 일치, 0 failed)

- [ ] **Step 6: 커밋**

```bash
git add src/web_app.py tests/test_pattern_routes.py
git commit -m "feat(pattern-modal): /api/pattern-popup/{textbook,actual} 라우트

- textbook: pattern_metadata.lookup 직접 → 200 + svg/description/signal_typical
- actual: analysis_cache pattern_json 추출 → pattern_popup.build_actual_chart
- 400: 필수 파라미터 누락
- 404: 미정의 패턴 / cache row 없음
- _fetch_pattern_json_for_symbol 헬퍼 — sqlite3 직접 조회
- 7 라우트 통합 테스트

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Frontend — pattern-modal.css + pattern-modal.js

**Files:**
- Create: `src/static/pattern-modal.css`
- Create: `src/static/pattern-modal.js`

- [ ] **Step 1: CSS 작성**

Create `src/static/pattern-modal.css`:

```css
/* Pattern modal — 배지 클릭 시 학습용 팝업 */

.pm-modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: none;
  z-index: 9999;
  align-items: center; justify-content: center;
  padding: 1rem;
}
.pm-modal-backdrop.pm-open { display: flex; }

.pm-modal {
  background: #fff;
  border-radius: 12px;
  max-width: 720px; width: 100%;
  max-height: 90vh; overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  display: flex; flex-direction: column;
}

.pm-modal-header {
  padding: 1rem 1.25rem;
  border-bottom: 1px solid #e5e7eb;
  display: flex; align-items: center; justify-content: space-between;
}
.pm-modal-title { font-size: 1.1rem; font-weight: 600; margin: 0; }
.pm-modal-close {
  background: transparent; border: none; cursor: pointer;
  font-size: 1.5rem; line-height: 1; color: #6b7280;
  padding: 0; margin-left: 1rem;
}
.pm-modal-close:hover { color: #111; }

.pm-tabs {
  display: flex;
  border-bottom: 1px solid #e5e7eb;
  background: #f9fafb;
}
.pm-tab {
  flex: 1; padding: 0.75rem 1rem;
  background: transparent; border: none; cursor: pointer;
  font-size: 0.95rem; color: #6b7280;
  border-bottom: 2px solid transparent;
}
.pm-tab.pm-active { color: #1E40AF; border-bottom-color: #1E40AF; background: #fff; font-weight: 600; }

.pm-tab-panel {
  padding: 1.25rem;
  display: none;
}
.pm-tab-panel.pm-active { display: block; }

.pm-textbook-svg {
  background: #f9fafb;
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
}
.pm-textbook-svg svg { width: 100%; height: auto; max-width: 100%; display: block; }

.pm-textbook-desc p { margin: 0.5rem 0; line-height: 1.55; color: #1f2937; }
.pm-textbook-desc strong { color: #111; }

.pm-actual-img {
  background: #f9fafb;
  border-radius: 8px;
  padding: 0.5rem;
  margin-bottom: 1rem;
}
.pm-actual-img img { width: 100%; height: auto; max-width: 100%; display: block; }

.pm-caption {
  font-size: 0.9rem;
  color: #4b5563;
  background: #eff6ff;
  padding: 0.75rem 1rem;
  border-radius: 6px;
  border-left: 3px solid #1E40AF;
}

.pm-loading {
  text-align: center;
  padding: 2rem;
  color: #6b7280;
}
.pm-loading::after {
  content: "...";
  display: inline-block;
  animation: pm-dots 1s steps(4, end) infinite;
}
@keyframes pm-dots { to { content: "...."; } }

.pm-error {
  background: #fef2f2;
  color: #991b1b;
  padding: 1rem;
  border-radius: 6px;
  border-left: 3px solid #DC2626;
}

/* badge 안의 패턴 이름 링크 */
a[data-pattern] {
  color: inherit;
  text-decoration: underline dotted;
  text-underline-offset: 2px;
  cursor: pointer;
}
a[data-pattern]:hover { text-decoration: underline; }
```

- [ ] **Step 2: JS 작성**

Create `src/static/pattern-modal.js`:

```javascript
/* Pattern badge modal — vanilla JS, 외부 라이브러리 없음 */
(function () {
  'use strict';

  let modal, backdrop, titleEl, tabTextbook, tabActual, panelTextbook, panelActual;
  let currentSymbol = null, currentPattern = null, currentDate = null;
  let actualLoaded = false;

  function ensureModal() {
    if (modal) return;
    backdrop = document.createElement('div');
    backdrop.className = 'pm-modal-backdrop';
    backdrop.innerHTML = `
      <div class="pm-modal" role="dialog" aria-modal="true">
        <div class="pm-modal-header">
          <h2 class="pm-modal-title">패턴</h2>
          <button class="pm-modal-close" aria-label="닫기">×</button>
        </div>
        <div class="pm-tabs">
          <button class="pm-tab pm-active" data-panel="textbook">교과서</button>
          <button class="pm-tab" data-panel="actual">실제 차트</button>
        </div>
        <div class="pm-tab-panel pm-active" data-panel="textbook">
          <div class="pm-loading">로딩 중</div>
        </div>
        <div class="pm-tab-panel" data-panel="actual">
          <div class="pm-loading">탭 클릭 시 로드됩니다</div>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    modal = backdrop.querySelector('.pm-modal');
    titleEl = backdrop.querySelector('.pm-modal-title');
    tabTextbook = backdrop.querySelector('.pm-tab[data-panel="textbook"]');
    tabActual = backdrop.querySelector('.pm-tab[data-panel="actual"]');
    panelTextbook = backdrop.querySelector('.pm-tab-panel[data-panel="textbook"]');
    panelActual = backdrop.querySelector('.pm-tab-panel[data-panel="actual"]');

    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) closeModal();
    });
    backdrop.querySelector('.pm-modal-close').addEventListener('click', closeModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && backdrop.classList.contains('pm-open')) closeModal();
    });
    tabTextbook.addEventListener('click', function () { switchTab('textbook'); });
    tabActual.addEventListener('click', function () { switchTab('actual'); });
  }

  function openModal(pattern, symbol, date) {
    ensureModal();
    currentPattern = pattern;
    currentSymbol = symbol;
    currentDate = date;
    actualLoaded = false;
    titleEl.textContent = symbol ? `${pattern} — ${symbol}` : pattern;
    panelTextbook.innerHTML = '<div class="pm-loading">로딩 중</div>';
    panelActual.innerHTML = '<div class="pm-loading">탭 클릭 시 로드됩니다</div>';
    switchTab('textbook');
    backdrop.classList.add('pm-open');
    loadTextbook(pattern);
  }

  function closeModal() {
    backdrop.classList.remove('pm-open');
  }

  function switchTab(name) {
    [tabTextbook, tabActual].forEach(function (t) { t.classList.remove('pm-active'); });
    [panelTextbook, panelActual].forEach(function (p) { p.classList.remove('pm-active'); });
    if (name === 'textbook') {
      tabTextbook.classList.add('pm-active');
      panelTextbook.classList.add('pm-active');
    } else {
      tabActual.classList.add('pm-active');
      panelActual.classList.add('pm-active');
      if (!actualLoaded && currentSymbol) {
        actualLoaded = true;
        loadActual(currentSymbol, currentPattern, currentDate);
      }
    }
  }

  function loadTextbook(pattern) {
    const url = '/api/pattern-popup/textbook?pattern=' + encodeURIComponent(pattern);
    fetch(url)
      .then(function (r) {
        if (r.status === 404) throw new Error('미정의 패턴: ' + pattern);
        if (!r.ok) throw new Error('서버 응답 오류: ' + r.status);
        return r.json();
      })
      .then(function (j) {
        panelTextbook.innerHTML =
          '<div class="pm-textbook-svg">' + j.svg + '</div>' +
          '<div class="pm-textbook-desc">' + j.description_html + '</div>';
      })
      .catch(function (e) {
        panelTextbook.innerHTML = '<div class="pm-error">' + e.message + '</div>';
      });
  }

  function loadActual(symbol, pattern, date) {
    let url = '/api/pattern-popup/actual?symbol=' + encodeURIComponent(symbol) +
              '&pattern=' + encodeURIComponent(pattern);
    if (date) url += '&date=' + encodeURIComponent(date);
    fetch(url)
      .then(function (r) {
        if (r.status === 404) throw new Error('이 종목에는 해당 분석 결과 없음');
        if (!r.ok) throw new Error('서버 응답 오류: ' + r.status);
        return r.json();
      })
      .then(function (j) {
        if (j.chart_b64) {
          panelActual.innerHTML =
            '<div class="pm-actual-img"><img src="data:image/png;base64,' + j.chart_b64 +
            '" alt="' + (j.pattern || '') + ' chart"></div>' +
            '<div class="pm-caption">' + escapeHTML(j.caption || '') + '</div>';
        } else {
          panelActual.innerHTML =
            '<div class="pm-error">' + escapeHTML(j.caption || '차트 데이터 없음') + '</div>';
        }
      })
      .catch(function (e) {
        panelActual.innerHTML = '<div class="pm-error">' + e.message + '</div>';
      });
  }

  function escapeHTML(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c];
    });
  }

  // global click delegation
  document.addEventListener('click', function (e) {
    const link = e.target.closest('a[data-pattern]');
    if (!link) return;
    e.preventDefault();
    const pattern = link.getAttribute('data-pattern');
    const symbol = link.getAttribute('data-symbol');
    const date = link.getAttribute('data-date') || null;
    openModal(pattern, symbol, date);
  });
})();
```

- [ ] **Step 3: Flask 가 static 서빙하는지 manual 확인**

```bash
.venv/bin/python -c "
from src.web_app import app
print('static_folder:', app.static_folder)
print('static_url_path:', app.static_url_path)
"
```

Expected: static_folder 가 `src/static` (절대경로) 비슷한 형태로 출력. `app.static_url_path` 가 `/static` (기본).

만약 static_folder 가 다른 곳을 가리키면 (예: `src/static` 이 아닌 다른 path), `Flask(__name__, ...)` 초기화 부분 수정 필요. 일반적으로 Flask 는 app module 폴더 기준 `static/` 이 기본 — `src/web_app.py` 의 app 이므로 `src/static/` 이 자동 매핑.

- [ ] **Step 4: Smoke — Flask test client 로 정적 파일 fetch**

```bash
.venv/bin/python -c "
from src.web_app import app
app.config['TESTING'] = True
c = app.test_client()
r = c.get('/static/pattern-modal.css')
print('CSS:', r.status_code, len(r.data), 'bytes')
r = c.get('/static/pattern-modal.js')
print('JS:', r.status_code, len(r.data), 'bytes')
"
```

Expected: 두 줄 다 `200`, byte 수 0보다 큼.

- [ ] **Step 5: 커밋**

```bash
git add src/static/pattern-modal.css src/static/pattern-modal.js
git commit -m "feat(pattern-modal): vanilla JS 모달 + CSS

- src/static/pattern-modal.css: backdrop/modal/탭/SVG-img 스타일, 모바일 max-width
- src/static/pattern-modal.js: event delegation [data-pattern] 클릭 → 모달 오픈
  - 교과서 탭: 즉시 fetch /api/pattern-popup/textbook
  - 실제 차트 탭: lazy fetch /api/pattern-popup/actual (탭 클릭 시 1회)
  - ESC / backdrop / X 버튼 닫기
  - escapeHTML 헬퍼 (caption XSS 방지)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 배지 렌더링 수정 + 페이지 head 에 CSS/JS include

**Files:**
- Modify: `src/web_app.py` (3 위치 + head)

이 task 가 가장 까다로움 — 기존 inline HTML 빌딩 코드를 수정해야 함. 정확한 위치 파악 필수.

- [ ] **Step 1: 위치 재확인**

```bash
grep -n 'pattern_badge_html\|"top_patterns"\|chart_pattern.*name\|candle_summary' src/web_app.py | head -20
```

기대 위치:
- `~1519`: 대시보드 배지 빌딩 (pattern_badge_html)
- `~1898`: 상세 페이지 "주요 패턴" 텍스트
- `~1955`: 상세 페이지 chart_patterns 리스트
- 또 다른 위치: candle_summary 출력

각 위치를 read 해서 정확한 텍스트 식별.

- [ ] **Step 2: 헬퍼 함수 — 패턴 이름 → 링크 변환**

`src/web_app.py` 의 import 섹션 (또는 헬퍼 함수 영역) 에 추가:

```python
def _pattern_link(pattern_name: str, symbol: str | None = None, date: str | None = None) -> str:
    """패턴 이름 → 모달 트리거 anchor.
    
    pattern_name: 한글 패턴명 (escape 처리)
    symbol: 선택 — analysis 컨텍스트 있을 때만
    date: 선택 — 다중 검출 식별
    """
    from markupsafe import escape as _esc
    attrs = f'data-pattern="{_esc(pattern_name)}"'
    if symbol:
        attrs += f' data-symbol="{_esc(symbol)}"'
    if date:
        attrs += f' data-date="{_esc(date)}"'
    return f'<a href="#" {attrs}>{_esc(pattern_name)}</a>'
```

- [ ] **Step 3: 대시보드 배지 수정 (`~1519`)**

기존 코드 (`web_app.py:1519~`):
```python
tops_text = " · ".join(tops[:2]) if tops else ""
if tops_text:
    pattern_badge_html = (
        f'<span class="badge" style="background:{color};color:#fff;">📈 {psig}: {tops_text}</span>'
    )
```

수정 — `tops_text` 빌드를 각 패턴 이름을 link 로:
```python
tops_links = [_pattern_link(p, symbol=symbol) for p in tops[:2]]
tops_text = " · ".join(tops_links) if tops_links else ""
if tops_text:
    pattern_badge_html = (
        f'<span class="badge" style="background:{color};color:#fff;">📈 {psig}: {tops_text}</span>'
    )
```

여기서 `symbol` 변수가 해당 스코프에서 사용 가능한지 확인 — 대시보드 row 빌드 컨텍스트에서 보통 종목 심볼 변수가 있음 (e.g., `sym`, `symbol`, 또는 `row["symbol"]`). 정확한 변수명 grep 으로 확인하여 사용.

- [ ] **Step 4: 상세 페이지 "주요 패턴" 수정 (`~1898`)**

기존 (`web_app.py:1898~`):
```python
tops = " · ".join(summary.get("top_patterns") or []) or "(패턴 없음)"
```

수정:
```python
top_list = summary.get("top_patterns") or []
if top_list:
    tops = " · ".join(_pattern_link(p, symbol=symbol) for p in top_list)
else:
    tops = "(패턴 없음)"
```

symbol 변수가 스코프에 있는지 확인 — `_build_pattern_section` 류 함수의 시그너처에 추가해야 할 수도. 추가 시 caller 들도 같이 수정.

- [ ] **Step 5: 상세 페이지 chart_patterns 카드 수정 (`~1955`)**

기존:
```python
rows.append(
    f'<li><span style="color:{ccolor};font-weight:600;">{escape(cp.get("name",""))}</span>'
    f' — {escape(csig)} (신뢰도 {cp.get("confidence",0)*100:.0f}%){range_str}'
    f'<br><small style="color:#475569;">{details}</small></li>'
)
```

수정 — pattern name 부분을 link 로:
```python
pname = cp.get("name", "")
pname_link = _pattern_link(pname, symbol=symbol, date=cp.get("to_date") or cp.get("from_date"))
rows.append(
    f'<li><span style="color:{ccolor};font-weight:600;">{pname_link}</span>'
    f' — {escape(csig)} (신뢰도 {cp.get("confidence",0)*100:.0f}%){range_str}'
    f'<br><small style="color:#475569;">{details}</small></li>'
)
```

- [ ] **Step 6: 캔들 패턴 리스트 (있다면) 동일 처리**

candle 패턴이 별도 섹션으로 렌더링되는 곳도 같은 패턴으로 수정. `grep -n "candles.*pj\|candles.*pattern_json\|candle.*name" src/web_app.py` 로 확인.

- [ ] **Step 7: 페이지 head 에 CSS/JS include**

기존 web_app.py 의 페이지 layout 빌드 부분 (보통 `<head>` 또는 base template 류) 에서 CSS link 와 JS script 추가. inline HTML 이라 head 가 함수 내 문자열로 있을 가능성 큼.

```bash
grep -n 'topbar-link\|<head>\|<link rel="stylesheet"' src/web_app.py | head -5
```

찾은 위치에 추가:
```html
<link rel="stylesheet" href="/static/pattern-modal.css">
<script src="/static/pattern-modal.js" defer></script>
```

- [ ] **Step 8: Manual smoke — Flask test client 로 페이지 fetch**

```bash
.venv/bin/python -c "
from src.web_app import app
app.config['TESTING'] = True
c = app.test_client()
# 인증 우회 (TESTING 모드에서 기본 anonymous OK 인지 점검)
r = c.get('/')
print('Dashboard:', r.status_code)
body = r.data.decode('utf-8', errors='ignore')
print('pattern-modal.css 포함:', '/static/pattern-modal.css' in body)
print('pattern-modal.js 포함:', '/static/pattern-modal.js' in body)
print('data-pattern 링크 존재:', 'data-pattern=' in body)
"
```

Expected: 적어도 `pattern-modal.css 포함: True`, `pattern-modal.js 포함: True`. data-pattern 은 종목 분석 결과 있을 때만 보임.

- [ ] **Step 9: 회귀 테스트 + 전체 leaders/web_app 테스트**

```bash
.venv/bin/python -m pytest tests/test_web_app.py tests/test_pattern_routes.py tests/test_pattern_popup.py tests/test_pattern_metadata.py -v 2>&1 | tail -15
```

Expected: 모두 PASS, 0 failed

- [ ] **Step 10: 커밋**

```bash
git add src/web_app.py
git commit -m "feat(pattern-modal): 배지 패턴 이름을 클릭 가능 링크로 + CSS/JS include

- _pattern_link 헬퍼: data-pattern/symbol/date attrs 빌더
- 대시보드 배지 (line ~1519): top_patterns 이름 각각 링크
- 상세 페이지 '주요 패턴' (line ~1898): top_patterns 각 링크
- 상세 페이지 chart_patterns 카드 (line ~1955): name 링크 + data-date
- 캔들 패턴 카드: name 링크 + data-date
- 페이지 head 에 pattern-modal.css + pattern-modal.js include

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 통합 verification + manual smoke

**Files:** (수정 없음 — 검증만)

- [ ] **Step 1: 전체 신규 + 기존 테스트 실행**

```bash
.venv/bin/python -m pytest tests/test_pattern_metadata.py tests/test_pattern_popup.py tests/test_pattern_routes.py tests/test_web_app.py -v 2>&1 | tail -20
```

Expected: 모든 테스트 PASS, 0 failed. 약 130+ tests.

- [ ] **Step 2: pyflakes/import 점검**

```bash
.venv/bin/python -c "
from src import pattern_metadata, pattern_popup
from src.web_app import app
print('imports OK')
print('routes:', [r.rule for r in app.url_map.iter_rules() if 'pattern' in r.rule])
"
```

Expected: `imports OK`, routes 에 `/api/pattern-popup/textbook`, `/api/pattern-popup/actual` 표시

- [ ] **Step 3: Manual smoke — 로컬 dev 서버**

```bash
.venv/bin/python main.py --web --port 8888 &
sleep 3
# 라우트 작동 확인
curl -s "http://localhost:8888/api/pattern-popup/textbook?pattern=더블바텀(W)" | head -c 200
echo ""
curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://localhost:8888/api/pattern-popup/textbook?pattern=없는것"
curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://localhost:8888/static/pattern-modal.css"
# 종료
pkill -f "main.py --web --port 8888"
```

Expected:
- 첫 curl: JSON 응답 (pattern/svg/description/signal_typical 키 포함)
- 두 번째: `HTTP 404`
- 세 번째: `HTTP 200`

- [ ] **Step 4: Push 전 최종 lint/format 점검 (있으면)**

```bash
# pre-commit hook 또는 ruff/black 적용 중인지 확인
ls .pre-commit-config.yaml pyproject.toml 2>/dev/null
```

있으면 해당 명령 실행. 없으면 skip.

- [ ] **Step 5: 푸시**

```bash
git push origin main
git log --oneline origin/main..HEAD  # 0줄이어야 함 (모두 push됨)
```

- [ ] **Step 6: Mac mini 배포 안내 출력**

배포 시 본인이 Mac mini 에서 실행할 명령:

```
ssh sykim-macmini "cd /Users/sykim/Projects/stock-analyzer && \
  git pull origin main && \
  .venv/bin/pip install -r requirements.txt && \
  launchctl kickstart -k gui/\$(id -u)/ai.stock-analyzer.web"
```

또는 본인이 Mac mini 에서 직접:
```
cd /Users/sykim/Projects/stock-analyzer
git pull origin main
.venv/bin/pip install -r requirements.txt
launchctl kickstart -k gui/$(id -u)/ai.stock-analyzer.web
```

- [ ] **Step 7: 배포 후 브라우저 검증** (사용자 작업)

`http://localhost:8080/` 또는 운영 URL 에서 분석된 종목 row 클릭 → 상세 페이지 → "📊 패턴 분석" 카드에서 패턴 이름 (예: 더블바텀, 잉태형) 클릭 → 모달 팝업 정상 표시 확인. 교과서 탭 즉시 표시, 실제 차트 탭 클릭 시 PNG 로드.

5개 표본 확인 권장: 더블바텀, 잉태형, 망치, 헤드앤숄더, 도지 (도지는 Tier2 데이터지만 generic template 으로 표시되는지 확인).

---

## Self-Review (writing-plans 가이드 §자체 리뷰)

**1. Spec coverage:**
- §2 Scope chart 5 + candle 60+ 티어드 → Task 3 (chart 5) + Task 4 (candle Tier1 15) + Task 5 (candle Tier2 40+) ✓
- §3 비기능: <100ms textbook, lazy actual, LRU cache → Task 6 LRU cache + Task 7 lazy 라우트 ✓
- §4.1 데이터 소스: pattern_json + yfinance → Task 6 pattern_popup, Task 7 _fetch_pattern_json_for_symbol ✓
- §4.2 백엔드 라우트 2개 → Task 7 ✓
- §4.3 차트 마킹 (chart pattern markers + candle vertical line) → Task 6 _render_chart ✓
- §4.4 H&S neckline 부재 fallback → Task 6 _render_chart 가 neckline None 인 경우 axhline skip, _build_caption 도 details fallback ✓
- §4.5 프론트엔드 JS + CSS → Task 8 ✓
- §4.6 메모리 LRU only → Task 6 _chart_cache OrderedDict ✓
- §5 Flow → 각 task 의 동작 순서 ✓
- §6 Files → File Structure section 일치 ✓
- §7 Security: <script> 가드, path traversal 회피, CSRF GET → Task 2 _SCRIPT_PATTERN, Task 6 디스크 캐시 없음, Task 7 GET 만 ✓
- §8 Testing 전략 → 각 task TDD 스텝 ✓
- §10 Migration → Task 10 deploy 안내 ✓

**2. Placeholder scan:**
- Task 9 Step 3-6 의 `~1519`, `~1898`, `~1955` 가 정확한 라인이 아닐 수 있어 Step 1 에서 grep 으로 재확인 후 작업 명시 → placeholder 아님 (정확 위치 식별 step 포함)
- 모든 Step 에 실제 코드 포함 — "implement later" 없음 ✓

**3. Type consistency:**
- `lookup()` 시그너처 `(pattern_name) → dict | None` Task 2 정의, Task 5 변경 (tier:2 합성) — return shape 동일 (svg/description_html/signal_typical 키 보장) ✓
- `find_detection(pj, name, date) → dict | None` Task 6 정의, Task 7 통합 테스트에서 사용 일치 ✓
- `build_actual_chart(symbol, pattern, date, pattern_json) → dict` Task 6 정의, Task 7 라우트 호출 일치 ✓
- `_pattern_link(name, symbol, date)` Task 9 Step 2 정의, Step 3/4/5 사용 일치 ✓

수정 사항 없음. 진행 가능.

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-05-15-pattern-badge-modal.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks, fast iteration. Sub-skill: `superpowers:subagent-driven-development`.

**2. Inline Execution** — batch execution in this session with checkpoints. Sub-skill: `superpowers:executing-plans`.

**Which approach?**
