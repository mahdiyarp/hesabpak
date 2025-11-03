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
    actions.innerHTML = [
      {href:`${window.prefix}/reports`, label:'📊 گزارشات'},
      {href:`${window.prefix}/sales`,   label:'🧾 فاکتور فروش جدید'},
      {href:`${window.prefix}/entities?kind=item`,   label:'📚 لیست کالاها'},
      {href:`${window.prefix}/entities?kind=person`, label:'📚 لیست اشخاص'}
    ].map(x=>`<a class="act" href="${x.href}">${x.label}</a>`).join('');
    showActions();
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

  if(kindSel){
    kindSel.addEventListener('change', function(){
      const q = input.value.trim();
      if(!q){
        hide(panel);
        showActions();
        return;
      }
      if(tmr) clearTimeout(tmr);
      runSearch(q);
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
    const url  = `${window.prefix}/api/search?q=${encodeURIComponent(q)}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`;
    fetch(url, {credentials:'same-origin'})
      .then(r=>r.json())
      .then(data=>{
        if(!Array.isArray(data) || data.length===0){
          panel.innerHTML = '<div class="empty">موردی یافت نشد.</div>';
          show(panel);
          return;
        }
        panel.innerHTML = data.map(m => {
          const meta = m.meta ? ` <span class="meta">| ${m.meta}</span>` : '';
          return `<a class="res" href="#" data-id="${m.id}" data-type="${m.type}" data-code="${m.code}">
                    <b>${badgeOf(m.type)}</b> — <code>${m.code}</code> — ${m.name}${meta}
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
      acts.push({href:`${window.prefix}/invoice/${id}`, label:'مشاهده فاکتور'});
      if(window.IS_ADMIN === true || window.IS_ADMIN === 'true'){
        acts.push({href:`${window.prefix}/invoice/${id}/edit`, label:'ویرایش فاکتور'});
      }
    }else if(typ === 'receive' || typ === 'payment'){
      acts.push({href:`${window.prefix}/cash/${id}`, label:'مشاهده سند'});
      if(window.IS_ADMIN === true || window.IS_ADMIN === 'true'){
        acts.push({href:`${window.prefix}/cash/${id}/edit`, label:'ویرایش سند'});
      }
    }else if(typ === 'person'){
      acts.push({href:`${window.prefix}/entities?kind=person&q=${encodeURIComponent(code)}`, label:'نمایه شخص'});
      acts.push({href:`${window.prefix}/reports?person_id=${id}`, label:'گزارشات این مشتری'});
    }else if(typ === 'item'){
      acts.push({href:`${window.prefix}/entities?kind=item&q=${encodeURIComponent(code)}`, label:'نمایه کالا'});
      acts.push({href:`${window.prefix}/reports?item_id=${id}`, label:'گزارشات فروش این کالا'});
    }

    if(actions){
      actions.innerHTML = acts.map(l=>`<a class="act" href="${l.href}">${l.label}</a>`).join('');
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

