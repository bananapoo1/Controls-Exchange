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

  const esc = (s='') => String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const shortDate = s => { try { return new Date(s).toLocaleDateString(undefined,{day:'numeric',month:'short'}); } catch { return ''; } };

  function openModal(id){ const el=document.getElementById(id); if(el){el.hidden=false; el.removeAttribute('aria-hidden'); document.body.classList.add('modal-open'); const closer=el.querySelector('.modal-close'); if(closer) closer.focus();} }
  function closeModal(id){ const el=document.getElementById(id); if(el){el.hidden=true; el.setAttribute('aria-hidden','true'); if(!document.querySelector('.modal-backdrop:not([hidden])')) document.body.classList.remove('modal-open');} }
  document.querySelectorAll('[data-close-modal]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.closeModal)));
  document.querySelectorAll('.modal-backdrop').forEach(el => { el.setAttribute('aria-hidden','true'); el.addEventListener('click', e => { if(e.target===el) closeModal(el.id); }); });
  document.addEventListener('keydown', e => {
    if(e.key === 'Escape'){
      const open=[...document.querySelectorAll('.modal-backdrop')].filter(el => !el.hidden);
      if(open.length){ open[open.length-1].id && closeModal(open[open.length-1].id); }
      closeMenu();
    }
  });

  const menuButton=document.querySelector('#menuButton');
  const mobileMenu=document.querySelector('#mobileMenu');
  function closeMenu(){ if(mobileMenu && !mobileMenu.hidden){ mobileMenu.hidden=true; menuButton?.setAttribute('aria-expanded','false'); menuButton?.setAttribute('aria-label','Open menu'); } }
  menuButton?.addEventListener('click', () => {
    const willOpen=mobileMenu.hidden;
    mobileMenu.hidden=!willOpen;
    menuButton.setAttribute('aria-expanded',String(willOpen));
    menuButton.setAttribute('aria-label',willOpen?'Close menu':'Open menu');
  });
  mobileMenu?.querySelectorAll('a').forEach(a => a.addEventListener('click', closeMenu));
  window.addEventListener('resize', () => { if(window.innerWidth>900) closeMenu(); });

  function renderResult(item){
    const trust=item.supplier_trust || {};
    const trustBits=[];
    if(trust.rfq_response_rate !== null && trust.rfq_response_rate !== undefined) trustBits.push(`${trust.rfq_response_rate}% RFQ response`);
    if(trust.avg_response_hours !== null && trust.avg_response_hours !== undefined) trustBits.push(`${trust.avg_response_hours}h avg reply`);
    const age=item.age_days===0?'confirmed today':(item.age_days===1?'confirmed yesterday':`${item.age_days ?? '?'}d since confirmation`);
    return `<article class="result-card selectable ${item.promoted ? 'featured' : ''}" data-id="${item.id}">
      <div class="result-top"><div class="select-row"><input class="result-select" type="checkbox" aria-label="Select ${esc(item.part_number)}" data-id="${item.id}"><span class="brand-pill">${esc(item.brand || 'Unbranded')}</span>${item.promoted ? '<span class="featured-badge">Featured</span>' : ''}</div><span class="condition">${esc(item.condition)}</span></div>
      <h3>${esc(item.part_number)}</h3><p>${esc(item.description || 'No description supplied')}</p>
      <div class="match-line"><span class="match-badge">${esc(item.match_reason || 'Match')}</span><span class="freshness freshness-${esc(item.freshness_state || 'fresh')}">${esc(item.freshness_label || 'Fresh')} · ${esc(age)}</span></div>
      ${item.catalog_part ? `<div class="result-catalog"><a class="catalog-link" href="/catalog/${item.catalog_part.id}">Canonical: ${esc(item.catalog_part.manufacturer)} ${esc(item.catalog_part.part_number)}</a><span class="reviewed-dot">${item.catalog_part.verified ? '✓ Reviewed' : 'Unreviewed'}${item.catalog_part.relation_count ? ` · ${item.catalog_part.relation_count} technical link${item.catalog_part.relation_count===1?'':'s'}` : ''}</span></div>` : ''}
      <div class="result-meta"><div><span>Quantity</span><strong>${item.quantity}</strong></div><div><span>Location</span><strong>${esc(item.location || 'Not stated')}</strong></div><div><span>Confirmed</span><strong>${shortDate(item.last_confirmed_at)}</strong></div></div>
      <div class="supplier-signal">${trustBits.length?esc(trustBits.join(' · ')):''}</div>
      <div class="result-bottom"><span><span class="verified-badge">✓ Verified</span> · ${item.supplier_id ? `<a href="/directory/${item.supplier_id}">${esc(item.supplier_name)}</a>` : esc(item.supplier_name)}</span><button class="button button-small button-dark single-rfq" data-id="${item.id}">Request quote</button></div>
    </article>`;
  }

  function syncSelection(){
    if(!selectionBar) return;
    selectedCount.textContent = selected.size;
    selectionBar.hidden = selected.size === 0;
    document.querySelectorAll('.result-card').forEach(card => card.classList.toggle('selected', selected.has(Number(card.dataset.id))));
  }

  function selectOne(id, checked){
    const card = document.querySelector(`.result-card[data-id="${id}"]`);
    const item = card?._item;
    if(checked && item) selected.set(id,item); else selected.delete(id);
    syncSelection();
  }

  async function runSearch(){
    if(!searchInput) return;
    const q=searchInput.value.trim();
    if(q.length < 2){ searchInput.focus(); return; }
    searchButton.disabled=true; searchButton.textContent='Searching…';
    try{
      const params=new URLSearchParams({q}); if(brandFilter?.value) params.set('brand',brandFilter.value); if(conditionFilter?.value) params.set('condition',conditionFilter.value);
      const res=await fetch(`/api/search?${params.toString()}`);
      const data=await res.json();
      selected.clear(); syncSelection(); section.hidden=false; title.textContent=`${data.count} result${data.count===1?'':'s'} for “${q}”`;
      if(data.results.length){
        grid.hidden=false; noResults.hidden=true; grid.innerHTML=data.results.map(renderResult).join('');
        data.results.forEach(item => { const card=document.querySelector(`.result-card[data-id="${item.id}"]`); if(card) card._item=item; });
        document.querySelectorAll('.result-select').forEach(cb => cb.addEventListener('change', () => selectOne(Number(cb.dataset.id), cb.checked)));
        document.querySelectorAll('.single-rfq').forEach(btn => btn.addEventListener('click', () => { selected.clear(); const id=Number(btn.dataset.id); const card=document.querySelector(`.result-card[data-id="${id}"]`); if(card?._item) selected.set(id,card._item); prepareRfq(); openModal('rfqModal'); syncSelection(); }));
      }else{
        grid.hidden=true; grid.innerHTML=''; noResults.hidden=false;
        if(wantedQuery) wantedQuery.value=q;
      }
      section.scrollIntoView({behavior:'smooth',block:'start'});
    }catch(err){
      grid.innerHTML='<div class="no-results">Search is temporarily unavailable. Please try again.</div>'; section.hidden=false;
    }finally{ searchButton.disabled=false; searchButton.textContent='Search inventory'; }
  }

  function prepareRfq(){
    if(!selected.size) return;
    const first=[...selected.values()][0];
    if(rfqPart) rfqPart.value = searchInput?.value.trim() || first.part_number;
    if(rfqInputs) rfqInputs.innerHTML=[...selected.keys()].map(id=>`<input type="hidden" name="inventory_ids" value="${id}">`).join('');
  }

  searchButton?.addEventListener('click', runSearch);
  searchInput?.addEventListener('keydown', e => { if(e.key==='Enter') runSearch(); });
  brandFilter?.addEventListener('change', () => { if(searchInput?.value.trim().length >= 2) runSearch(); });
  conditionFilter?.addEventListener('change', () => { if(searchInput?.value.trim().length >= 2) runSearch(); });
  document.querySelector('#clearSearch')?.addEventListener('click', () => { if(searchInput) searchInput.value=''; if(section)section.hidden=true; selected.clear(); syncSelection(); });
  openRfqButton?.addEventListener('click', () => { prepareRfq(); openModal('rfqModal'); });
  document.querySelector('#openWantedButton')?.addEventListener('click', () => openModal('wantedModal'));
  saveSearchButton?.addEventListener('click', () => {
    const q=searchInput?.value.trim() || ''; if(q.length<2) return;
    if(savedSearchQuery) savedSearchQuery.value=q;
    if(savedSearchBrand) savedSearchBrand.value=brandFilter?.value || '';
    if(savedSearchCondition) savedSearchCondition.value=conditionFilter?.value || '';
    if(savedSearchSummary) savedSearchSummary.textContent=`${q}${brandFilter?.value?' · '+brandFilter.value:''}${conditionFilter?.value?' · '+conditionFilter.value:''}`;
    openModal('saveSearchModal');
  });
  if(searchInput?.value.trim().length >= 2) window.addEventListener('DOMContentLoaded', () => runSearch());
})();
