(() => {
  const searchInput = document.querySelector('#heroSearch');
  const searchButton = document.querySelector('#heroSearchButton');
  const section = document.querySelector('#resultsSection');
  const grid = document.querySelector('#resultsGrid');
  const title = document.querySelector('#resultsTitle');
  const noResults = document.querySelector('#noResults');
  const selectionBar = document.querySelector('#selectionBar');
  const selectedCount = document.querySelector('#selectedCount');
  const openRfqButton = document.querySelector('#openRfqButton');
  const rfqPart = document.querySelector('#rfqPart');
  const wantedQuery = document.querySelector('#wantedQuery');
  const rfqInputs = document.querySelector('#rfqInventoryInputs');
  const selected = new Map();
  const brandFilter = document.querySelector('#brandFilter');
  const conditionFilter = document.querySelector('#conditionFilter');
  const saveSearchButton = document.querySelector('#saveSearchButton');
  const savedSearchQuery = document.querySelector('#savedSearchQuery');
  const savedSearchBrand = document.querySelector('#savedSearchBrand');
  const savedSearchCondition = document.querySelector('#savedSearchCondition');
  const savedSearchSummary = document.querySelector('#savedSearchSummary');
  const modalReturnFocus = new Map();

  const esc = (s = '') => String(s).replace(/[&<>'"]/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[c]));

  const shortDate = value => {
    if (!value) return 'Not stated';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Not stated';
    return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  };

  const focusableSelector = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    modalReturnFocus.set(id, document.activeElement);
    el.hidden = false;
    el.removeAttribute('aria-hidden');
    document.body.classList.add('modal-open');
    const target = el.querySelector('.modal-close') || el.querySelector(focusableSelector);
    target?.focus();
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = true;
    el.setAttribute('aria-hidden', 'true');
    if (!document.querySelector('.modal-backdrop:not([hidden])')) document.body.classList.remove('modal-open');
    const previous = modalReturnFocus.get(id);
    if (previous && typeof previous.focus === 'function') previous.focus();
    modalReturnFocus.delete(id);
  }

  document.querySelectorAll('[data-close-modal]').forEach(btn => {
    btn.addEventListener('click', () => closeModal(btn.dataset.closeModal));
  });

  document.querySelectorAll('.modal-backdrop').forEach(el => {
    el.setAttribute('aria-hidden', 'true');
    el.addEventListener('click', e => {
      if (e.target === el) closeModal(el.id);
    });
    el.addEventListener('keydown', e => {
      if (e.key !== 'Tab' || el.hidden) return;
      const focusable = [...el.querySelectorAll(focusableSelector)].filter(node => node.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
  });

  const menuButton = document.querySelector('#menuButton');
  const mobileMenu = document.querySelector('#mobileMenu');

  function closeMenu() {
    if (mobileMenu && !mobileMenu.hidden) {
      mobileMenu.hidden = true;
      menuButton?.setAttribute('aria-expanded', 'false');
      menuButton?.setAttribute('aria-label', 'Open menu');
    }
  }

  menuButton?.addEventListener('click', () => {
    if (!mobileMenu) return;
    const willOpen = mobileMenu.hidden;
    mobileMenu.hidden = !willOpen;
    menuButton.setAttribute('aria-expanded', String(willOpen));
    menuButton.setAttribute('aria-label', willOpen ? 'Close menu' : 'Open menu');
  });

  mobileMenu?.querySelectorAll('a').forEach(a => a.addEventListener('click', closeMenu));
  window.addEventListener('resize', () => { if (window.innerWidth > 900) closeMenu(); });

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const open = [...document.querySelectorAll('.modal-backdrop')].filter(el => !el.hidden);
    if (open.length) closeModal(open[open.length - 1].id);
    closeMenu();
    document.querySelectorAll('.account-menu[open]').forEach(menu => menu.removeAttribute('open'));
  });

  document.addEventListener('click', e => {
    document.querySelectorAll('.account-menu[open]').forEach(menu => {
      if (!menu.contains(e.target)) menu.removeAttribute('open');
    });
  });

  function renderResult(item) {
    const trust = item.supplier_trust || {};
    const trustBits = [];
    if (trust.rfq_response_rate !== null && trust.rfq_response_rate !== undefined) trustBits.push(`${trust.rfq_response_rate}% RFQ response`);
    if (trust.avg_response_hours !== null && trust.avg_response_hours !== undefined) trustBits.push(`${trust.avg_response_hours}h avg reply`);
    const age = item.age_days === 0 ? 'confirmed today' : (item.age_days === 1 ? 'confirmed yesterday' : `${item.age_days ?? '?'}d since confirmation`);
    return `<article class="result-card selectable ${item.promoted ? 'featured' : ''}" data-id="${item.id}">
      <div class="result-top">
        <div class="select-row">
          <input class="result-select" type="checkbox" aria-label="Select ${esc(item.part_number)}" data-id="${item.id}">
          <span class="brand-pill">${esc(item.brand || 'Unbranded')}</span>
          ${item.promoted ? '<span class="featured-badge">Featured</span>' : ''}
        </div>
        <span class="condition">${esc(item.condition || 'Not stated')}</span>
      </div>
      <h3>${esc(item.part_number)}</h3>
      <p>${esc(item.description || 'No description supplied')}</p>
      <div class="match-line">
        <span class="match-badge">${esc(item.match_reason || 'Match')}</span>
        <span class="freshness freshness-${esc(item.freshness_state || 'fresh')}">${esc(item.freshness_label || 'Fresh')} · ${esc(age)}</span>
      </div>
      ${item.catalog_part ? `<div class="result-catalog"><a class="catalog-link" href="/catalog/${item.catalog_part.id}">Canonical: ${esc(item.catalog_part.manufacturer)} ${esc(item.catalog_part.part_number)}</a><span class="reviewed-dot">${item.catalog_part.verified ? '✓ Reviewed' : 'Unreviewed'}${item.catalog_part.relation_count ? ` · ${item.catalog_part.relation_count} technical link${item.catalog_part.relation_count === 1 ? '' : 's'}` : ''}</span></div>` : ''}
      <div class="result-meta">
        <div><span>Quantity</span><strong>${esc(item.quantity ?? '—')}</strong></div>
        <div><span>Location</span><strong>${esc(item.location || 'Not stated')}</strong></div>
        <div><span>Confirmed</span><strong>${shortDate(item.last_confirmed_at)}</strong></div>
      </div>
      <div class="supplier-signal">${trustBits.length ? esc(trustBits.join(' · ')) : ''}</div>
      <div class="result-bottom">
        <span><span class="verified-badge">✓ Verified</span> · ${item.supplier_id ? `<a href="/directory/${item.supplier_id}">${esc(item.supplier_name)}</a>` : esc(item.supplier_name)}</span>
        <button class="button button-small button-dark single-rfq" type="button" data-id="${item.id}">Request quote</button>
      </div>
    </article>`;
  }

  function syncSelection() {
    if (!selectionBar || !selectedCount) return;
    selectedCount.textContent = selected.size;
    selectionBar.hidden = selected.size === 0;
    document.querySelectorAll('.result-card').forEach(card => {
      card.classList.toggle('selected', selected.has(Number(card.dataset.id)));
    });
  }

  function selectOne(id, checked) {
    const card = document.querySelector(`.result-card[data-id="${id}"]`);
    const item = card?._item;
    if (checked && item) selected.set(id, item);
    else selected.delete(id);
    syncSelection();
  }

  async function runSearch() {
    if (!searchInput || !searchButton || !section || !grid || !title || !noResults) return;
    const q = searchInput.value.trim();
    if (q.length < 2) {
      searchInput.focus();
      searchInput.setAttribute('aria-invalid', 'true');
      return;
    }
    searchInput.removeAttribute('aria-invalid');
    searchButton.disabled = true;
    searchButton.setAttribute('aria-busy', 'true');
    searchButton.textContent = 'Searching…';

    try {
      const params = new URLSearchParams({ q });
      if (brandFilter?.value) params.set('brand', brandFilter.value);
      if (conditionFilter?.value) params.set('condition', conditionFilter.value);

      const res = await fetch(`/api/search?${params.toString()}`, { headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error(`Search failed with ${res.status}`);
      const data = await res.json();
      if (!Array.isArray(data.results)) throw new Error('Malformed search response');

      selected.clear();
      syncSelection();
      section.hidden = false;
      title.textContent = `${data.count} result${data.count === 1 ? '' : 's'} for “${q}”`;

      if (data.results.length) {
        grid.hidden = false;
        noResults.hidden = true;
        grid.innerHTML = data.results.map(renderResult).join('');
        data.results.forEach(item => {
          const card = document.querySelector(`.result-card[data-id="${item.id}"]`);
          if (card) card._item = item;
        });
        document.querySelectorAll('.result-select').forEach(cb => {
          cb.addEventListener('change', () => selectOne(Number(cb.dataset.id), cb.checked));
        });
        document.querySelectorAll('.single-rfq').forEach(btn => {
          btn.addEventListener('click', () => {
            selected.clear();
            const id = Number(btn.dataset.id);
            const card = document.querySelector(`.result-card[data-id="${id}"]`);
            if (card?._item) selected.set(id, card._item);
            prepareRfq();
            openModal('rfqModal');
            syncSelection();
          });
        });
      } else {
        grid.hidden = true;
        grid.innerHTML = '';
        noResults.hidden = false;
        if (wantedQuery) wantedQuery.value = q;
      }

      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      grid.hidden = false;
      noResults.hidden = true;
      grid.innerHTML = '<div class="no-results"><div class="empty-state-icon">!</div><h3>Search is temporarily unavailable</h3><p>Please try again in a moment.</p></div>';
      section.hidden = false;
      title.textContent = 'Search unavailable';
      console.error(err);
    } finally {
      searchButton.disabled = false;
      searchButton.removeAttribute('aria-busy');
      searchButton.textContent = 'Search';
    }
  }

  function prepareRfq() {
    if (!selected.size) return;
    const first = [...selected.values()][0];
    if (rfqPart) rfqPart.value = searchInput?.value.trim() || first.part_number;
    if (rfqInputs) rfqInputs.innerHTML = [...selected.keys()].map(id => `<input type="hidden" name="inventory_ids" value="${id}">`).join('');
  }

  searchButton?.addEventListener('click', runSearch);
  searchInput?.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
  brandFilter?.addEventListener('change', () => { if (searchInput?.value.trim().length >= 2) runSearch(); });
  conditionFilter?.addEventListener('change', () => { if (searchInput?.value.trim().length >= 2) runSearch(); });

  document.querySelectorAll('.quick-search').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!searchInput) return;
      searchInput.value = btn.dataset.query || '';
      runSearch();
    });
  });

  document.querySelector('#clearSearch')?.addEventListener('click', () => {
    if (searchInput) searchInput.value = '';
    if (brandFilter) brandFilter.value = '';
    if (conditionFilter) conditionFilter.value = '';
    if (section) section.hidden = true;
    selected.clear();
    syncSelection();
    searchInput?.focus();
  });

  openRfqButton?.addEventListener('click', () => { prepareRfq(); openModal('rfqModal'); });
  document.querySelector('#openWantedButton')?.addEventListener('click', () => openModal('wantedModal'));

  saveSearchButton?.addEventListener('click', () => {
    const q = searchInput?.value.trim() || '';
    if (q.length < 2) return;
    if (savedSearchQuery) savedSearchQuery.value = q;
    if (savedSearchBrand) savedSearchBrand.value = brandFilter?.value || '';
    if (savedSearchCondition) savedSearchCondition.value = conditionFilter?.value || '';
    if (savedSearchSummary) savedSearchSummary.textContent = `${q}${brandFilter?.value ? ' · ' + brandFilter.value : ''}${conditionFilter?.value ? ' · ' + conditionFilter.value : ''}`;
    openModal('saveSearchModal');
  });

  if (searchInput?.value.trim().length >= 2) {
    window.addEventListener('DOMContentLoaded', () => runSearch(), { once: true });
  }
})();
