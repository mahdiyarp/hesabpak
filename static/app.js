// =============== Safe Prefix (fix undefined) ===============
(function(){
  var p = (typeof window.APP_PREFIX === "string" ? window.APP_PREFIX : (typeof window.prefix === "string" ? window.prefix : ""));
  if (typeof window.IS_ADMIN === 'undefined') window.IS_ADMIN = false;
  if(!p || p === "undefined" || p === "/undefined") p = "";
  if(p.endsWith("/")) p = p.replace(/\/+$/,"");
  window.prefix = p;
})();

// =============== Helpers ===============
function $(s, r){ return (r||document).querySelector(s); }
function $all(s, r){ return Array.from((r||document).querySelectorAll(s)); }
function show(el){ if(!el) return; el.hidden=false; el.style.display='block'; }
function hide(el){ if(!el) return; el.hidden=true; el.style.display='none'; }
function toNum(x){ if(x==null) return 0; x=(""+x).replace(/,/g,'').trim(); var f=parseFloat(x); return isNaN(f)?0:f; }
function badgeOf(t){
  if(t==='person') return 'شخص';
  if(t==='item') return 'کالا';
  if(t==='invoice') return 'فاکتور';
  if(t==='receive') return 'دریافت';
  if(t==='payment') return 'پرداخت';
  return t||'نتیجه';
}

// =============== Global Search ===============
(function(){
  const input   = $('#global-search');
  const panel   = $('#global-results') || $('#search-results');
  const actions = $('#global-actions') || $('#search-actions');
  const kindSel = $('#global-kind');
  const sortSel = $('#global-sort');
  const userPerms = Array.isArray(window.USER_PERMISSIONS) ? window.USER_PERMISSIONS : [];
  const isAdmin = (window.IS_ADMIN === true || window.IS_ADMIN === 'true');
  const allow = (perm)=>{ if(!perm) return true; return isAdmin || userPerms.indexOf(perm) !== -1; };

  if(!input || !panel) return;

  const showActions = ()=>{
    if(!actions) return;
    actions.hidden = false;
    actions.style.display = 'flex';
  };
  const hideActions = ()=>{
    if(!actions) return;
    actions.hidden = true;
    actions.style.display = 'none';
  };

  if(actions){
    const quickLinks = [
      {href:`${window.prefix}/reports`, label:'📊 گزارشات', perm:'reports'},
      {href:`${window.prefix}/sales`,   label:'🧾 فاکتور فروش جدید', perm:'sales'},
      {href:`${window.prefix}/entities?kind=item`,   label:'📚 لیست کالاها', perm:'entities'},
      {href:`${window.prefix}/entities?kind=person`, label:'📚 لیست اشخاص', perm:'entities'}
    ].filter(link => allow(link.perm));
    if(quickLinks.length){
      actions.innerHTML = quickLinks.map(x=>`<a class="act" href="${x.href}">${x.label}</a>`).join('');
      showActions();
    } else {
      actions.innerHTML = '<span class="muted">مجوزی برای میانبرها ندارید.</span>';
      showActions();
    }
  }

  let tmr = null;
  input.addEventListener('input', function(){
    const q = input.value.trim();
    if(tmr) clearTimeout(tmr);
    if(q.length === 0){
      panel.innerHTML = '';
      hide(panel);
      showActions();
      return;
    }
    hideActions();
    tmr = setTimeout(()=> runSearch(q), 220);
  });

  function rerunIfNeeded(){
    const q = input.value.trim();
    if(!q){
      hide(panel);
      showActions();
      return;
    }
    if(tmr) clearTimeout(tmr);
    runSearch(q);
  }

  if(kindSel){
    kindSel.addEventListener('change', function(){
      const q = input.value.trim();
      if(!q){
        hide(panel);
        showActions();
        return;
      }
      rerunIfNeeded();
    });
  }

  if(sortSel){
    sortSel.addEventListener('change', function(){
      if(document && document.body){
        document.body.setAttribute('data-search-sort', sortSel.value || '');
      }
      rerunIfNeeded();
    });
  }

  input.addEventListener('keydown', function(ev){
    if(ev.key === 'Escape'){
      input.value = '';
      panel.innerHTML='';
      hide(panel);
      showActions();
    }
  });

  function runSearch(q){
    const kind = kindSel ? (kindSel.value || '').trim() : '';
    const sort = sortSel ? (sortSel.value || '').trim() : (document.body?.dataset?.searchSort || '');
    const url  = `${window.prefix}/api/search?q=${encodeURIComponent(q)}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}${sort ? `&sort=${encodeURIComponent(sort)}` : ''}`;
    fetch(url, {credentials:'same-origin'})
      .then(r=>r.json())
      .then(data=>{
        if(!Array.isArray(data) || data.length===0){
          panel.innerHTML = '<div class="empty">موردی یافت نشد.</div>';
          show(panel);
          return;
        }
        panel.innerHTML = data.map(m => {
          const pieces = (m.meta || '').split('•').map(part => part.trim()).filter(Boolean);
          if(m.stock){ pieces.push(`موجودی: ${m.stock}`); }
          if(m.price){ pieces.push(`قیمت: ${m.price}`); }
          if(m.balance && (!m.type || m.type === 'person')){ pieces.push(`مانده: ${m.balance}`); }
          const meta = pieces.length ? `<div class="res-meta">${pieces.map(p=>`<span class="res-sub">${p}</span>`).join('')}</div>` : '';
          const badge = `<span class="res-badge">${badgeOf(m.type)}</span>`;
          const code = m.code ? `<span class="res-code">${m.code}</span>` : '';
          return `<a class="res" href="#" data-id="${m.id}" data-type="${m.type}" data-code="${m.code || ''}">
                    <div class="res-head">${badge}${code}<span class="res-title">${m.name || ''}</span></div>
                    ${meta}
                  </a>`;
        }).join('');
        show(panel);
      })
      .catch(err=>{
        console.error('search failed', err);
        panel.innerHTML = '<div class="empty">خطا در جستجو</div>';
        show(panel);
      });
  }

  panel.addEventListener('click', function(ev){
    const a = ev.target.closest('a.res');
    if(!a) return;
    ev.preventDefault();

    const typ  = a.dataset.type;
    const id   = a.dataset.id;
    const code = a.dataset.code;

    let acts = [];
    if(typ === 'invoice'){
      if(allow('reports') || allow('sales') || allow('purchase')){
        acts.push({href:`${window.prefix}/invoice/${id}`, label:'مشاهده فاکتور'});
      }
      if(isAdmin){
        acts.push({href:`${window.prefix}/invoice/${id}/edit`, label:'ویرایش فاکتور'});
      }
    }else if(typ === 'receive' || typ === 'payment'){
      if(allow('reports') || allow(typ)){
        acts.push({href:`${window.prefix}/cash/${id}`, label:'مشاهده سند'});
      }
      if(isAdmin){
        acts.push({href:`${window.prefix}/cash/${id}/edit`, label:'ویرایش سند'});
      }
    }else if(typ === 'person'){
      if(allow('entities')){
        acts.push({href:`${window.prefix}/entities?kind=person&q=${encodeURIComponent(code)}`, label:'نمایه شخص'});
      }
      if(allow('reports')){
        acts.push({href:`${window.prefix}/reports?person_id=${id}`, label:'گزارشات این مشتری'});
      }
    }else if(typ === 'item'){
      if(allow('entities')){
        acts.push({href:`${window.prefix}/entities?kind=item&q=${encodeURIComponent(code)}`, label:'نمایه کالا'});
      }
      if(allow('reports')){
        acts.push({href:`${window.prefix}/reports?item_id=${id}`, label:'گزارشات فروش این کالا'});
      }
    }

    if(actions){
      if(acts.length){
        actions.innerHTML = acts.map(l=>`<a class="act" href="${l.href}">${l.label}</a>`).join('');
      }else{
        actions.innerHTML = '<span class="muted">مجوزی برای این نتیجه ندارید.</span>';
      }
      showActions();
    }
    hide(panel);
  });

  document.addEventListener('click', function(ev){
    if(ev.target.closest('.global-search, .search-wrap')) return;
    hide(panel);
    if(!input.value.trim()) showActions();
  });
})();

// =============== Form persistence (per-page, sessionStorage) ===============
(function(){
  const forms = $all('form[data-persist="true"]');
  if(!forms.length) return;
  if(typeof window.sessionStorage === 'undefined') return;

  function storageKey(form){
    const custom = form.getAttribute('data-persist-key');
    return `hp:persist:${custom || window.location.pathname}`;
  }

  function collect(form){
    const data = {};
    Array.from(form.elements).forEach(el => {
      if(!el.name || el.disabled) return;
      if(el.type === 'password') return;
      if(el.name.endsWith('[]')) return;
      if(el.type === 'checkbox'){
        data[el.name] = el.checked;
      }else if(el.type === 'radio'){
        if(el.checked) data[el.name] = el.value;
      }else{
        data[el.name] = el.value;
      }
    });
    return data;
  }

  function restore(form, data){
    if(!data) return;
    Array.from(form.elements).forEach(el => {
      if(!el.name || !(el.name in data)) return;
      const val = data[el.name];
      if(el.type === 'checkbox'){
        el.checked = !!val;
      }else if(el.type === 'radio'){
        el.checked = (el.value === val);
      }else{
        el.value = val;
      }
    });
  }

  forms.forEach(form => {
    const key = storageKey(form);
    try {
      const saved = sessionStorage.getItem(key);
      if(saved){ restore(form, JSON.parse(saved)); }
    } catch(err){ console.warn('restore form failed', err); }

    const handler = () => {
      try {
        const snapshot = collect(form);
        sessionStorage.setItem(key, JSON.stringify(snapshot));
      } catch(err){ console.warn('persist form failed', err); }
    };
    form.addEventListener('input', handler);
    form.addEventListener('change', handler);
    form.addEventListener('submit', () => {
      try { sessionStorage.removeItem(key); } catch(err){ /* ignore */ }
    });
  });
})();

// =============== Live Clocks ===============
(function(){
  const nodes = document.querySelectorAll('[data-live-clock]');
  if(nodes.length === 0) return;

  let jalaliFormatter = null;
  try {
    jalaliFormatter = new Intl.DateTimeFormat('fa-IR-u-ca-persian', {year:'numeric', month:'2-digit', day:'2-digit'});
  } catch(err){
    jalaliFormatter = null;
  }
  const timeFormatter = new Intl.DateTimeFormat('fa-IR', {hour:'2-digit', minute:'2-digit', second:'2-digit'});

  function tick(){
    const now = new Date();
    nodes.forEach(node => {
      const timeEl = node.querySelector('[data-role="time"]');
      if(timeEl){
        try {
          timeEl.textContent = timeFormatter.format(now);
        } catch(err){
          timeEl.textContent = now.toLocaleTimeString('fa-IR');
        }
      }
      const dateEl = node.querySelector('[data-role="date"]');
      if(dateEl){
        if(jalaliFormatter){
          try {
            dateEl.textContent = jalaliFormatter.format(now);
            return;
          } catch(err){ /* fallback below */ }
        }
        if(node.dataset.jalali){
          dateEl.textContent = node.dataset.jalali;
        }
      }
    });
  }

  tick();
  setInterval(tick, 1000);
})();

// =============== Jalali Date Inputs ===============
(function(){
  const inputs = document.querySelectorAll('[data-jalali-input]');
  if(inputs.length === 0) return;

  const faDigits = '۰۱۲۳۴۵۶۷۸۹'.split('');
  const enDigits = '0123456789'.split('');

  const toFaDigits = (value)=> String(value || '').replace(/\d/g, d => faDigits[Number(d)]);
  const toEnDigits = (value)=> String(value || '').replace(/[۰-۹]/g, ch => enDigits[faDigits.indexOf(ch)]);

  const pad = (n)=> n.toString().padStart(2, '0');

  function jalaliToGregorian(jy, jm, jd){
    jy = parseInt(jy, 10);
    jm = parseInt(jm, 10);
    jd = parseInt(jd, 10);
    if(isNaN(jy) || isNaN(jm) || isNaN(jd)) return null;
    jy += 1595;
    let days = -355668 + (365 * jy) + Math.floor(jy / 33) * 8 + Math.floor(((jy % 33) + 3) / 4);
    days += jd + (jm <= 6 ? (31 * (jm - 1)) : ((jm - 7) * 30) + 186);
    let gy = 400 * Math.floor(days / 146097);
    days %= 146097;
    if(days > 36524){
      gy += 100 * Math.floor((days - 1) / 36524);
      days = (days - 1) % 36524;
      if(days >= 365){
        days += 1;
      }
    }
    gy += 4 * Math.floor(days / 1461);
    days %= 1461;
    if(days > 365){
      gy += Math.floor((days - 1) / 365);
      days = (days - 1) % 365;
    }
    let gd = days + 1;
    let gm;
    if(days < 186){
      gm = 1 + Math.floor(days / 31);
      gd = 1 + (days % 31);
    } else {
      gm = 7 + Math.floor((days - 186) / 30);
      gd = 1 + ((days - 186) % 30);
    }
    return [gy, gm, gd];
  }

  function gregorianToJalali(gy, gm, gd){
    gy = parseInt(gy, 10);
    gm = parseInt(gm, 10);
    gd = parseInt(gd, 10);
    if(isNaN(gy) || isNaN(gm) || isNaN(gd)) return null;
    const g_d_m = [0,31,59,90,120,151,181,212,243,273,304,334];
    let jy = (gy > 1600) ? 979 : 0;
    gy -= (gy > 1600) ? 1600 : 621;
    const gy2 = gm > 2 ? gy + 1 : gy;
    let days = (365 * gy) + Math.floor((gy2 + 3) / 4) - Math.floor((gy2 + 99) / 100) + Math.floor((gy2 + 399) / 400);
    days += gd + g_d_m[gm - 1] - 80;
    jy += 33 * Math.floor(days / 12053);
    days %= 12053;
    jy += 4 * Math.floor(days / 1461);
    days %= 1461;
    if(days > 365){
      jy += Math.floor((days - 1) / 365);
      days = (days - 1) % 365;
    }
    const jm = (days < 186) ? 1 + Math.floor(days / 31) : 7 + Math.floor((days - 186) / 30);
    const jd = (days < 186) ? 1 + (days % 31) : 1 + ((days - 186) % 30);
    return [jy, jm, jd];
  }

  function parseJalali(raw){
    if(!raw) return null;
    const clean = toEnDigits(String(raw)).replace(/\//g, '-').trim();
    const parts = clean.split('-');
    if(parts.length !== 3) return null;
    const jy = parseInt(parts[0], 10);
    const jm = parseInt(parts[1], 10);
    const jd = parseInt(parts[2], 10);
    if(!jy || !jm || !jd) return null;
    if(jm < 1 || jm > 12 || jd < 1 || jd > 31) return null;
    return {jy, jm, jd};
  }

  inputs.forEach(inp => {
    const targetId = inp.dataset.jalaliTarget;
    if(!targetId) return;
    const hidden = document.getElementById(targetId) || document.querySelector(`[name="${targetId}"]`);
    if(!hidden) return;

    const syncFromHidden = ()=>{
      const raw = hidden.value || '';
      if(!raw) return;
      const parts = raw.split('-');
      if(parts.length !== 3) return;
      const g = parts.map(p=>parseInt(p,10));
      const j = gregorianToJalali(g[0], g[1], g[2]);
      if(j){
        inp.value = toFaDigits(`${j[0]}-${pad(j[1])}-${pad(j[2])}`);
      }
    };

    const syncHidden = ()=>{
      const parsed = parseJalali(inp.value);
      if(!parsed){
        hidden.value = '';
        inp.setCustomValidity(inp.value.trim() ? 'تاریخ جلالی نامعتبر است.' : '');
        return;
      }
      const g = jalaliToGregorian(parsed.jy, parsed.jm, parsed.jd);
      if(!g){
        hidden.value = '';
        inp.setCustomValidity('تاریخ جلالی نامعتبر است.');
        return;
      }
      hidden.value = `${g[0]}-${pad(g[1])}-${pad(g[2])}`;
      inp.value = toFaDigits(`${parsed.jy}-${pad(parsed.jm)}-${pad(parsed.jd)}`);
      inp.setCustomValidity('');
    };

    inp.addEventListener('input', ()=>{
      inp.value = toFaDigits(inp.value.replace(/[^0-9۰-۹\-\/]/g, ''));
    });
    inp.addEventListener('blur', syncHidden);
    inp.addEventListener('change', syncHidden);

    if(hidden.value){
      syncFromHidden();
    } else if(inp.value){
      syncHidden();
    }
  });
})();

