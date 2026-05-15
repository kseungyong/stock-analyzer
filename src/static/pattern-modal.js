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
      <div class="pm-modal" role="dialog" aria-modal="true" aria-labelledby="pm-modal-title">
        <div class="pm-modal-header">
          <h2 class="pm-modal-title" id="pm-modal-title">패턴</h2>
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
    // move focus to close button for keyboard accessibility
    const closeBtn = backdrop.querySelector('.pm-modal-close');
    if (closeBtn) closeBtn.focus();
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
        if (currentPattern !== pattern) return;  // stale, ignore
        panelTextbook.innerHTML =
          '<div class="pm-textbook-svg">' + j.svg + '</div>' +
          '<div class="pm-textbook-desc">' + j.description_html + '</div>';
      })
      .catch(function (e) {
        if (currentPattern !== pattern) return;  // stale error, ignore
        panelTextbook.innerHTML = '<div class="pm-error">' + escapeHTML(e.message) + '</div>';
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
        if (currentPattern !== pattern || currentSymbol !== symbol || currentDate !== date) return;  // stale, ignore
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
        if (currentPattern !== pattern || currentSymbol !== symbol || currentDate !== date) return;  // stale error, ignore
        panelActual.innerHTML = '<div class="pm-error">' + escapeHTML(e.message) + '</div>';
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
