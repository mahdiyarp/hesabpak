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
    const list = $(".search-results", box);
    if(!rows.length){ list.innerHTML=""; list.hidden=true; return; }

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
      return `
        <div class="res-item" data-id="${r.id}" data-title="${(r.code||'') + ' — ' + (r.name||'')}">
          <div class="res-head">
            <span class="res-badge">${badgeOf(type)}</span>
            <span class="res-code">${r.code||''}</span>
            <span class="res-title">${r.name||''}</span>
          </div>
          ${metaHtml}
        </div>`;
    }).join("");

    list.hidden = false;

    // انتخاب با موس
    $$(".res-item", list).forEach(it=>{
      it.addEventListener("click", ()=>{
        selectResult(box, it.getAttribute("data-id"), it.getAttribute("data-title"));
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
    const type = wrap.getAttribute("data-type") || "item";
    // support two markup styles:
    // 1) unified: .search-box > .search-input, .search-results, .search-selected-id
    // 2) legacy: .search-wrapper > input (search input) and .search-results
    let box = $(".search-box", wrap);
    let legacy = false;
    if(!box){
      // try legacy structure
      const sw = $(".search-wrapper", wrap) || $(".search-wrapper");
      if(!sw) return;
      legacy = true;
      // create a small facade object to map to expected selectors
      box = document.createElement('div');
      box.className = 'search-box legacy-box';
      // attach references for later lookup
      box._legacyWrapper = sw;
      // insert into DOM for event handling convenience
      sw.appendChild(box);
    }

    const input = legacy ? box._legacyWrapper.querySelector('input') : $(".search-input", box);
    const list = legacy ? (box._legacyWrapper.querySelector('.search-results') || document.createElement('div')) : $(".search-results", box);
    if(!input || !list) return;

    const onSearch = debounce(async ()=>{
      const q = input.value.trim();
      if(q.length<1){ list.hidden=true; list.innerHTML=""; return; }
      const rows = await fetchResults(type, q, wrap.getAttribute('data-sort'));
      renderResults(box, rows, type);
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
      const container = legacy ? box._legacyWrapper : wrap;
      if(!container.contains(ev.target)) list.hidden=true;
    });
  }

  // Override selectResult to support populating legacy hidden inputs
  const _origSelect = selectResult;
  function selectResult(box, id, title){
    // try to find a selected-id input inside the box's parent
    try{
      const parent = box.parentElement || box._legacyWrapper || document;
      const sel = parent.querySelector('.search-selected-id') || parent.querySelector('.item_id') || parent.querySelector('#person_token') || parent.querySelector('input[name="person_token"]');
      if(sel){
        // .item_id or #person_token or .search-selected-id
        if(sel.classList.contains('item_id')){
          sel.value = id;
        }else{
          sel.value = id;
        }
      } else {
        // fallback: set global #person_token if present
        const globalPerson = document.querySelector('#person_token');
        if(globalPerson) globalPerson.value = id;
      }
    }catch(e){ /* ignore */ }
    // update visible input if available
    try{ const inp = box.querySelector('.search-input') || (box._legacyWrapper && box._legacyWrapper.querySelector('input')); if(inp) inp.value = title || ""; }catch(e){}
    // hide results
    try{ const list = box.querySelector('.search-results') || (box._legacyWrapper && box._legacyWrapper.querySelector('.search-results')); if(list) list.hidden = true; }catch(e){}
    // dispatch event for modules
    try{ box.dispatchEvent(new CustomEvent("search:selected", { bubbles:true, detail:{ id, title } })); }catch(e){}
  }

  document.addEventListener("DOMContentLoaded", ()=>{
    $$(".search-wrap").forEach(initOne);
  });
})();
