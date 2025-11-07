(function(){
  const $ = (sel,root=document)=>root.querySelector(sel);
  const $$= (sel,root=document)=>Array.from(root.querySelectorAll(sel));

  function debounce(fn, ms=250){
    let t; return (...args)=>{ clearTimeout(t); t=setTimeout(()=>fn(...args),ms); };
  }

  function badgeOf(t){
    if(t==='person') return 'شخص';
    if(t==='item') return 'کالا';
    if(t==='invoice') return 'فاکتور';
    if(t==='receive') return 'دریافت';
    if(t==='payment') return 'پرداخت';
    if(t==='cheque') return 'چک';
    return t || 'نتیجه';
  }

  async function fetchResults(type, q, sort){
    const p = (typeof window.prefix === 'string' ? window.prefix : (window.APP_PREFIX || '')) || '';
    const order = sort || (document.body?.dataset?.searchSort || '');
    const url = `${p}/api/search?kind=${encodeURIComponent(type)}&q=${encodeURIComponent(q)}${order ? `&sort=${encodeURIComponent(order)}` : ''}`;
    const r = await fetch(url);
    if(!r.ok) return [];
    const j = await r.json();
        return Array.isArray(j) ? j : [];
  }

  function renderResults(box, rows, type){
    // resolve list element (may be legacy wrapper). If a portal list was created
    // for legacy mode (moved to document.body), prefer that stored reference.
    const list = box._legacyList || $(".search-results", box) || (box._legacyWrapper && box._legacyWrapper.querySelector('.search-results'));
  if(!rows || !rows.length){ list.innerHTML=""; try{ list.hidden = true; list.style.display = 'none'; }catch(e){} return; }

  // Filter client-side by requested type to avoid showing items when the
  // input is intended for persons (or vice versa). Backend should respect
  // the `kind` param but some legacy paths may return mixed types.
  try{
    if(type && type !== 'all'){
      // strict, case-insensitive match: only include rows that explicitly declare the matching type
      rows = rows.filter(r => (r && r.type) ? (String(r.type).toLowerCase() === String(type).toLowerCase()) : false);
    }
  }catch(e){ /* ignore filter errors */ }

    list.innerHTML = rows.map(r=>{
      const detailParts = [];
      if(r.meta){
        const parts = String(r.meta).split('•').map(p=>p.trim()).filter(Boolean);
        parts.forEach(p=> detailParts.push(`<div class="res-sub">${p}</div>`));
      }
      if(type === 'item'){
        if(r.stock) detailParts.push(`<div class="res-sub">موجودی: ${r.stock}</div>`);
        if(r.price) detailParts.push(`<div class="res-sub">قیمت: ${r.price}</div>`);
      }else if(type === 'person'){
        if(r.balance) detailParts.push(`<div class="res-sub">مانده: ${r.balance}</div>`);
      }
      const metaHtml = detailParts.length ? `<div class="res-meta">${detailParts.join('')}</div>` : '';
      // Use the actual result type for the badge when available.
      const rowType = r.type || type;
      return `
        <div class="res-item" data-id="${r.id}" data-kind="${rowType}" data-title="${(r.code||'') + ' — ' + (r.name||'')}">
          <div class="res-head">
            <span class="res-badge">${badgeOf(rowType)}</span>
            <span class="res-code">${r.code||''}</span>
            <span class="res-title">${r.name||''}</span>
          </div>
          ${metaHtml}
        </div>`;
    }).join("");

    try{ list.hidden = false; list.style.display = 'block'; }catch(e){}

    // Instrumentation: mark which script rendered this list and log for debugging.
    try{
      try{ list.setAttribute('data-rendered-by','unified'); }catch(e){}
      try{ list.setAttribute('data-rendered-at', String(Date.now())); }catch(e){}
      try{ console.debug && console.debug('search-unified: renderResults', { type: type, rows: rows ? rows.length : 0, box: box }); }catch(e){}
    }catch(e){}

    // Position the dropdown to avoid viewport clipping and match the input width.
    // For legacy wrappers we use fixed positioning (so parent overflow doesn't clip),
    // otherwise position absolute relative to the closest box/container.
    try{
      // determine input element robustly: legacy portal wrapper, or look for
      // .search-input, .search-inp, or a plain input inside the box
      const inp = (box._legacyWrapper ? (box._legacyWrapper.querySelector('input') || box._legacyWrapper.querySelector('input[type=text]')) : (box.querySelector('.search-input') || box.querySelector('.search-inp') || box.querySelector('input')));
      if(inp){
        const irect = inp.getBoundingClientRect();
        const viewportH = window.innerHeight || document.documentElement.clientHeight;
        const spaceBelow = viewportH - irect.bottom;
        const spaceAbove = irect.top;
        // prefer available space; use CSS max-height value (320) as a guide
        const cssMax = 320;
        let useAbove = false;
        if(spaceBelow < 120 && spaceAbove > spaceBelow) useAbove = true;

        // choose dimensions
        const maxHeight = Math.min(cssMax, useAbove ? Math.max(60, spaceAbove - 12) : Math.max(60, spaceBelow - 12));

  const isLegacy = !!box._legacyWrapper;
  if(isLegacy){
          // ensure portal'd list (if any) uses fixed positioning
          list.style.position = 'fixed';
          list.style.zIndex = 2147483648;
          list.style.width = `${irect.width}px`;
          list.style.maxHeight = `${maxHeight}px`;
          if(useAbove){
            // place above input
            list.style.top = `${Math.max(8, irect.top - 6 - maxHeight)}px`;
            list.style.left = `${irect.left}px`;
          }else{
            list.style.left = `${irect.left}px`;
            list.style.top = `${irect.bottom + 6}px`;
          }
  }else{
          // non-legacy: position absolute within box container
          // ensure parent box is positioned (it should be). Compute offset left relative to box.
          const containerRect = box.getBoundingClientRect();
          list.style.position = 'absolute';
          list.style.zIndex = 2147483648;
          list.style.width = `${irect.width}px`;
          list.style.maxHeight = `${maxHeight}px`;
          // if opening above, set bottom so it sits above the input
          if(useAbove){
            list.classList.add('open-above');
            // compute bottom offset from box
            const bottomOffset = containerRect.bottom - irect.top + 6; // positive px from container bottom
            list.style.bottom = `${bottomOffset}px`;
            list.style.top = 'auto';
            // left relative to container
            list.style.left = `${irect.left - containerRect.left}px`;
          }else{
            list.classList.remove('open-above');
            list.style.top = `${irect.bottom - containerRect.top + 6}px`;
            list.style.bottom = 'auto';
            list.style.left = `${irect.left - containerRect.left}px`;
          }
        }
      }
    }catch(e){console.warn(e);}

    // انتخاب با موس
    $$(".res-item", list).forEach(it=>{
      it.addEventListener("click", ()=>{
        selectResult(box, it.getAttribute("data-id"), it.getAttribute("data-title"), it.getAttribute('data-kind'));
      });
    });
  }

  function selectResult(box, id, title){
    $(".search-selected-id", box.parentElement).value = id;
    $(".search-input", box).value = title || "";
    $(".search-results", box).hidden = true;

    // ایونت سفارشی برای ماژول‌ها (آیتم انتخاب شد)
    box.dispatchEvent(new CustomEvent("search:selected", {
      bubbles: true, detail: { id, title }
    }));
  }

  function moveActive(list, dir){
    const items = $$(".res-item", list);
    if(!items.length) return;
    const cur = items.findIndex(x=>x.classList.contains("active"));
    let nx = (cur<0)? (dir>0?0:(items.length-1))
                    : (cur+dir+items.length)%items.length;
    if(cur>=0) items[cur].classList.remove("active");
    items[nx].classList.add("active");
    items[nx].scrollIntoView({block:"nearest"});
  }

  function pickActive(list){
    const it = $(".res-item.active", list) || $(".res-item", list);
    if(!it) return null;
    return { id: it.getAttribute("data-id"), title: it.getAttribute("data-title") };
  }

  function initOne(wrap){
    // normalize the incoming wrapper: caller may pass .search-wrap (container),
    // .search-box (inner), or .search-wrapper (legacy per-field). Detect and
    // unify to a `wrap` that we can use to find type/sort attrs.
    let container = wrap;
    if(wrap && wrap.classList && wrap.classList.contains('search-box')){
      container = wrap.parentElement || wrap;
    }
  // try to get declared data-type from container; defer final inference until
  // we have the input element so we can inspect its id/name/attributes.
  let type = (container.getAttribute && container.getAttribute("data-type")) || null;
    // support two markup styles:
    // 1) unified: .search-box > .search-input, .search-results, .search-selected-id
    // 2) legacy: .search-wrapper > input (search input) and .search-results
    let box = $(".search-box", container);
    let legacy = false;
    let sw = null;
    if(!box){
      // if the container itself is a search-wrap, treat it as the box
      if(container.classList && container.classList.contains('search-wrap')){
        box = container;
      } else {
        // if the container itself is a legacy .search-wrapper, use it directly
        if(container.classList && container.classList.contains('search-wrapper')){
          sw = container;
        }else{
          // otherwise try to find a legacy wrapper inside this container
          sw = $(".search-wrapper", container) || null;
        }
        if(!sw){
          // nothing to init here
          return;
        }
        legacy = true;
        // create a small facade object to map to expected selectors
        box = document.createElement('div');
        box.className = 'search-box legacy-box';
        // attach references for later lookup
        box._legacyWrapper = sw;
        // append a small sentinel element so queries using box can run
        sw.appendChild(box);
      // If we found a legacy .search-results inside the wrapper, move it to document.body
      // so fixed positioning is not affected by parent overflow/transform in some browsers.
      try{
        const legacyList = sw.querySelector('.search-results') || sw.querySelector('.item_results');
        if(legacyList){
          // detach and append to body as a portal; remember original parent
          legacyList._originalParent = legacyList.parentElement;
          document.body.appendChild(legacyList);
          // store a quick reference on box for renderResults to use
          box._legacyList = legacyList;
        }
      }catch(e){/* ignore portal errors */}
    }

  const input = legacy ? (box._legacyWrapper.querySelector('input') || box._legacyWrapper.querySelector('input[type=text]')) : $(".search-input", box);
  const list = legacy ? (box._legacyWrapper.querySelector('.search-results') || box._legacyWrapper.querySelector('.item_results')) : $(".search-results", box);
    if(!input || !list) return;
    // mark this container as handled by unified search so other scripts skip it
    try{
      if(legacy && box._legacyWrapper) box._legacyWrapper.setAttribute('data-search-handler','unified');
      if(box) box.setAttribute('data-search-handler','unified');
      if(list) list.setAttribute('data-search-handler','unified');
    }catch(e){}

  // Infer the intended kind for this input if not explicitly set.
  try{
    if(!type){
      // prefer explicit attributes on the input
      type = input.getAttribute('data-kind') || input.dataset.kind || input.getAttribute('data-type') || input.dataset.type || null;
    }
    // Additional heuristics: placeholder text or enclosing customer-lock indicating a person field
    if(!type){
      try{
        const ph = (input.getAttribute('placeholder') || '').toLowerCase();
        if(ph.includes('مشتری') || ph.includes('تأمین') || ph.includes('طرف حساب') || ph.includes('customer') || ph.includes('person')){
          type = 'person';
        }
      }catch(e){}
    }
    if(!type){
      try{ if(input.closest && input.closest('.customer-lock')){ type = 'person'; } }catch(e){}
    }
    if(!type){
      const nid = (input.id || '').toLowerCase();
      const nname = (input.name || '').toLowerCase();
      // common patterns that denote a person/customer field
      if(nid.includes('person') || nname.includes('person') || nid.includes('customer') || nname.includes('customer') || nid.includes('person_search') ){
        type = 'person';
      } else if(nid.includes('item') || nname.includes('item') || nid.includes('product') || nname.includes('product') || nid.includes('goods')){
        type = 'item';
      } else {
        // fallback: if this is inside an entities container with data-kind, try that
        const parentKind = container.querySelector && container.querySelector('[data-kind]') ? (container.querySelector('[data-kind]').getAttribute('data-kind')) : null;
        if(parentKind) type = parentKind;
      }
    }
    // final fallback — assume item for legacy behavior if still unknown
    if(!type) type = 'item';
  }catch(e){ type = type || 'item'; }

    const onSearch = debounce(async ()=>{
      const q = (input && input.value) ? input.value.trim() : '';
      if(q.length<1){ list.innerHTML=""; try{ list.hidden=true; list.style.display='none'; }catch(e){} return; }
      // For global wrappers, prefer the selected kind/sort controls if present
      let effectiveType = type;
      try{
        const kindSel = container.querySelector && container.querySelector('#global-kind');
        if(kindSel) effectiveType = kindSel.value || '';
      }catch(e){/* ignore */}
      let sortPref = wrap.getAttribute('data-sort') || '';
      try{
        const sortSel = container.querySelector && container.querySelector('#global-sort');
        if(sortSel) sortPref = sortSel.value || sortPref;
      }catch(e){/* ignore */}
      const rows = await fetchResults(effectiveType || 'all', q, sortPref);
      renderResults(box, rows, effectiveType || 'all');
    }, 220);

    input.addEventListener("input", onSearch);
    input.addEventListener("focus", ()=>{
      if(list.innerHTML.trim()) list.hidden=false;
    });

    input.addEventListener("keydown", (e)=>{
      if(list.hidden && (e.key==="ArrowDown"||e.key==="ArrowUp")){ list.hidden=false; e.preventDefault(); return; }
      if(e.key==="ArrowDown"){ moveActive(list, +1); e.preventDefault(); }
      else if(e.key==="ArrowUp"){ moveActive(list, -1); e.preventDefault(); }
      else if(e.key==="Enter"){
        const pick = pickActive(list);
        if(pick){ selectResult(box, pick.id, pick.title); e.preventDefault(); }
      }else if(e.key==="Escape"){ list.hidden=true; }
    });

    document.addEventListener("click",(ev)=>{
      // if legacy wrapper, hide when clicking outside that wrapper
      const containerEl = legacy ? box._legacyWrapper : container;
      try{ if(!containerEl.contains(ev.target)) list.hidden=true; }catch(e){}
    });
  }

  // Override selectResult to support populating legacy hidden inputs
  const _origSelect = selectResult;
  function selectResult(box, id, title, kind){
    // Populate the most appropriate hidden input for this search box.
    try{
      const parent = box.parentElement || box._legacyWrapper || document;
      let sel = null;
      // Prefer an .item_id or .search-selected-id within the same table row (if any)
      try{
        const row = parent.closest ? parent.closest('tr') : null;
        if(row){ sel = row.querySelector('.item_id') || row.querySelector('.search-selected-id'); }
      }catch(e){}
      // If not found in row, look inside the parent wrapper
      if(!sel){ sel = parent.querySelector('.search-selected-id') || parent.querySelector('.item_id'); }
      // If still not found, fall back to global person_token
      if(!sel){ sel = document.querySelector('#person_token') || document.querySelector('input[name="person_token"]'); }

      if(sel){ try{ sel.value = id; }catch(e){} }
    }catch(e){ /* ignore */ }
    // update visible input if available
    try{ const inp = box.querySelector('.search-input') || (box._legacyWrapper && box._legacyWrapper.querySelector('input')); if(inp) inp.value = title || ""; }catch(e){}
    // hide results
    try{ const list = box.querySelector('.search-results') || (box._legacyWrapper && box._legacyWrapper.querySelector('.search-results')); if(list) list.hidden = true; }catch(e){}
    // dispatch event for modules and global listeners
    try{ box.dispatchEvent(new CustomEvent("search:selected", { bubbles:true, detail:{ id, title, kind } })); }catch(e){}
    try{ window.dispatchEvent(new CustomEvent('search:selected', { detail: { id, title, kind } })); }catch(e){}

    // If this search widget is the global/top search, perform default navigation
    try{
      const p = (typeof window.prefix === 'string' ? window.prefix : (window.APP_PREFIX || '')) || '';
      const parentEl = parent;
      const globalWrap = parentEl.closest ? parentEl.closest('.global-search') : null;
      if(globalWrap){
        const k = (kind || '').toLowerCase();
        if(k === 'invoice'){
          window.location = p + '/invoice/' + encodeURIComponent(id);
          return;
        }else if(k === 'receive' || k === 'payment' || k === 'cash'){
          window.location = p + '/cash/' + encodeURIComponent(id);
          return;
        }else if(k === 'item' || k === 'person'){
          window.location = p + '/entities?kind=' + encodeURIComponent(k) + '&q=' + encodeURIComponent(title || '');
          return;
        }else{
          window.location = p + '/entities?q=' + encodeURIComponent(title || '');
          return;
        }
      }
      // If this selection happened inside a filters form (entities list),
      // navigate to entity edit/cardex for direct inspection.
      try{
        const filtersForm = parentEl.closest ? parentEl.closest('form.filters, .filters') : null;
        if(filtersForm){
          window.location = p + '/entities/' + encodeURIComponent(id) + '/edit';
          return;
        }
      }catch(e){/* ignore */}
    }catch(e){ /* ignore navigation errors */ }
  }

  document.addEventListener("DOMContentLoaded", ()=>{
    // initialize all search widgets (including the global topbar and any
    // containers explicitly marked as ajax via data-ajax="1"). We make
    // this the single authoritative search implementation for the app so
    // all modules (current and future) use the same behavior and avoid
    // double-initialization/flicker between different search scripts.
    $$(".search-wrap, .search-box, .search-wrapper, [data-ajax=\"1\"]").forEach(initOne);
    try{ window.SEARCH_UNIFIED_ACTIVE = true; }catch(e){}
  });
})();
