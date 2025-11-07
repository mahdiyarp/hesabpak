// Lightweight fully-AJAX search widget
// Attaches to .search-wrapper and .search-box usages and dispatches a
// 'search:selected' CustomEvent with detail = {id, kind, title, extra}
(function(){
  if(typeof window === 'undefined') return;
  // Disabled: search-unified.js is the single authoritative search handler.
  // Keeping this file present for backward compatibility, but do not run
  // initialization to avoid double-initialization and flicker.
  try{ console.info('search-ajax: disabled — search-unified will handle searches'); }catch(e){}
  return;
  const PREFIX = window.APP_PREFIX || '';

  function $(sel, el){ return (el || document).querySelector(sel); }
  function $all(sel, el){ return Array.from((el || document).querySelectorAll(sel)); }

  function debounce(fn, wait){ let t; return function(...a){ clearTimeout(t); t = setTimeout(()=>fn.apply(this,a), wait); }; }

  function buildUrl(q, kind, sort){
    const params = new URLSearchParams();
    params.set('q', q);
    if(kind) params.set('kind', kind);
    if(sort) params.set('sort', sort);
    return PREFIX + '/api/search?' + params.toString();
  }

  function renderResults(container, items){
    // If this container belongs to unified search, do not overwrite its results.
    try{
      if(container && container.closest && container.closest('[data-search-handler="unified"]')){
        return;
      }
    }catch(e){}
    container.innerHTML = '';
    if(!items || !items.length){ container.innerHTML = '<div class="no-results muted" style="padding:8px">موردی یافت نشد</div>'; return; }
    const ul = document.createElement('div');
    ul.className = 'search-list';
    items.forEach(it=>{
      const row = document.createElement('div');
      row.className = 'search-row';
      row.tabIndex = 0;
      row.dataset.id = it.id;
      row.dataset.kind = it.kind || '';
      row.dataset.title = it.title || it.name || '';
      row.innerHTML = `<div class="sr-main">${it.title || it.name || ''}</div><div class="sr-sub muted">${it.extra || ''}</div>`;
      row.addEventListener('click',()=>{
        container.dispatchEvent(new CustomEvent('search-picked',{detail:it, bubbles:true}));
      });
      row.addEventListener('keydown', (e)=>{ if(e.key==='Enter') row.click(); });
      ul.appendChild(row);
    });
    container.appendChild(ul);
  }

  function positionFixedResults(input, results){
    // position near input to avoid clipping by parent overflow
    const rect = input.getBoundingClientRect();
    results.style.position = 'fixed';
    results.style.left = (rect.left) + 'px';
    results.style.top = (rect.bottom + 6) + 'px';
    results.style.minWidth = Math.max(rect.width, 220) + 'px';
    results.style.maxHeight = '40vh';
    results.style.overflow = 'auto';
    results.style.zIndex = 2147483647;
  }

  function initOne(container){
    // container can be .search-wrapper or .search-box or .search-wrap
    // if this container or its descendants are already handled by unified search, skip
    try{
      if(container.querySelector && container.querySelector('[data-search-handler="unified"]')) return;
      if(container.closest && container.closest('[data-search-handler="unified"]')) return;
    }catch(e){}
    let input = $('.search-inp, input[type="text"], input.search-inp, input.search-input', container) || $('.search-input',container) || container.querySelector('input');
    if(!input) return;
    let results = container.querySelector('.search-results') || document.createElement('div');
    if(!container.contains(results)){
      results.className = 'search-results';
      results.hidden = true;
      container.appendChild(results);
    }

    const kind = container.dataset.searchKind || input.dataset.searchKind || input.getAttribute('data-search-kind') || '';
    const sortPref = container.dataset.searchSort || document.body.dataset.searchSort || '';

    const doSearch = debounce(async function(){
      const q = input.value.trim();
      if(!q){ results.hidden = true; results.style.display = 'none'; return; }
      try{
        const url = buildUrl(q, kind, sortPref);
        const res = await fetch(url, {cache:'no-store'});
        const data = await res.json();
        const items = Array.isArray(data) ? data : (data.rows || data.items || []);
        renderResults(results, items);
        // show and position
        results.hidden = false; results.style.display = 'block';
        positionFixedResults(input, results);
      }catch(err){
        results.innerHTML = '<div class="muted">خطا در دریافت نتایج</div>';
        results.hidden = false; results.style.display = 'block';
        positionFixedResults(input, results);
        console.error('search-ajax error', err);
      }
    }, 200);

    input.addEventListener('input', doSearch);
    input.addEventListener('focus', doSearch);
    // hide on outside click
    document.addEventListener('click', function(ev){ if(ev.target.closest && ev.target.closest('.search-wrapper')===container) return; results.hidden = true; results.style.display = 'none'; });

    // forward picked event to set hidden inputs if present
    results.addEventListener('search-picked', function(ev){
      const it = ev.detail;
      // set common hidden inputs if exist
      const selId = container.querySelector('.search-selected-id') || document.querySelector('.item_id') || document.querySelector('#person_token') || container.querySelector('input[name="person_token"]');
      if(selId){ try{ selId.value = it.id; }catch(e){} }
      // also dispatch a global event 'search:selected'
      window.dispatchEvent(new CustomEvent('search:selected', {detail:it}));
      // If this search widget is the global/search-wrap (topbar), perform a default navigation
      try{
        const globalWrap = container.closest ? container.closest('.global-search') : null;
        if(globalWrap){
          const kind = (it.kind || it.type || '').toLowerCase();
          if(kind === 'invoice'){
            window.location = PREFIX + '/invoice/' + encodeURIComponent(it.id);
            return;
          }else if(kind === 'receive' || kind === 'payment'){
            window.location = PREFIX + '/cash/' + encodeURIComponent(it.id);
            return;
          }else if(kind === 'item' || kind === 'person'){
            window.location = PREFIX + '/entities?kind=' + encodeURIComponent(kind) + '&q=' + encodeURIComponent(it.title || it.name || '');
            return;
          }else{
            window.location = PREFIX + '/entities?q=' + encodeURIComponent(it.title || it.name || '');
            return;
          }
        }
      }catch(e){ /* ignore navigation errors */ }

      // hide
      results.hidden = true; results.style.display = 'none';
    });
  }

  // initialize only the global top search and elements explicitly marked for ajax
  // (data-ajax="1") to avoid conflicting with search-unified widgets.
  function initAll(){
    // global search wrapper (topbar) and any container with data-ajax="1"
    $all('.search-wrap.global-search, [data-ajax="1"]').forEach(initOne);
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initAll); else initAll();

  // observe DOM additions: init newly added global search or ajax-marked wrappers
  const obs = new MutationObserver(muts=>{
    muts.forEach(m=>{
      (m.addedNodes || []).forEach(n=>{
        try{
          if(n && n.nodeType === 1){
            if(n.matches && (n.matches('.search-wrap.global-search') || n.matches('[data-ajax="1"]'))){ initOne(n); }
            else if(n.querySelector && (n.querySelector('.search-wrap.global-search') || n.querySelector('[data-ajax="1"]'))){ initAll(); }
          }
        }catch(e){}
      });
    });
  });
  obs.observe(document.body,{childList:true, subtree:true});

})();
