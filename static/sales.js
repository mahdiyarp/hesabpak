(function(){
  const prefix = (function(){
    let p = typeof window.APP_PREFIX === 'string' ? window.APP_PREFIX : (typeof window.prefix === 'string' ? window.prefix : '');
    if(!p || p === 'undefined' || p === '/undefined') p = '';
    if(p.endsWith('/')) p = p.replace(/\/+$/, '');
    window.prefix = p;
    return p;
  })();

  const overlay = document.querySelector('.sales-module .overlay-shield');

  function activateOverlay(){
    if(overlay){ overlay.classList.add('active'); }
  }
  function deactivateOverlay(){
    if(overlay){ overlay.classList.remove('active'); }
  }

  function show(el){ if(!el) return; el.style.display = 'block'; el.hidden = false; activateOverlay(); }
  function hide(el){ if(!el) return; el.style.display = 'none'; el.hidden = true; deactivateOverlay(); }

  function doubleConfirm(msg1, msg2){
    if(!window.confirm(msg1)) return false;
    return window.confirm(msg2);
  }

  function auditLog(action, payload){
    try{
      const body = JSON.stringify({
        context: 'sales',
        action,
        payload: payload || {},
        ts: new Date().toISOString()
      });
      const url = `${prefix}/api/audit/log`;
      if(navigator.sendBeacon){
        const blob = new Blob([body], {type:'application/json'});
        navigator.sendBeacon(url, blob);
      }else{
        fetch(url, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          credentials: 'same-origin',
          body
        }).catch(()=>{});
      }
    }catch(err){
      console.warn('audit log skipped', err);
    }
  }

  function fmt3(n){
    const s = n == null ? '' : String(n);
    if(s === '') return '';
    const num = parseFloat(s.replace(/,/g, ''));
    if(Number.isNaN(num)) return s;
    return num.toLocaleString('en-US', {maximumFractionDigits: 2, useGrouping: true});
  }

  function toNum(v){
    if(v == null) return 0;
    const num = parseFloat(String(v).replace(/,/g, ''));
    return Number.isNaN(num) ? 0 : num;
  }

  const invDateFa = document.querySelector('#inv_date_fa');
  const invDateGreg = document.querySelector('#inv_date_greg');
  if(invDateFa && invDateGreg && invDateFa.dataset.greg){
    invDateGreg.value = invDateFa.dataset.greg;
  }

  const personWrapper = document.querySelector('.sales-module .customer-lock');
  const personSearch = document.querySelector('#person_search');
  const personBox = document.querySelector('#person_results');
  const personToken = document.querySelector('#person_token');
  const personHint = document.querySelector('#person_hint');
  const personUnlockBtn = document.querySelector('#person_unlock');

  let personTimer = null;

  function renderPerson(results){
    if(!Array.isArray(results) || results.length === 0){
      hide(personBox);
      personBox.innerHTML = '';
      return;
    }
    personBox.innerHTML = results.map(r => `<a class="res" href="#" data-id="${r.id}" data-name="${r.name}" data-code="${r.code}">${r.code} — ${r.name}</a>`).join('');
    show(personBox);
  }

  function searchPerson(q){
    fetch(`${prefix}/api/search?q=${encodeURIComponent(q)}&kind=person&limit=12`, {credentials:'same-origin'})
      .then(r => r.ok ? r.json() : [])
      .then(renderPerson)
      .catch(()=>{ hide(personBox); personBox.innerHTML=''; });
  }

  function lockCustomer(data){
    if(!personWrapper) return;
    personWrapper.classList.add('locked');
    if(personSearch){
      personSearch.value = `${data.code}`;
      personSearch.readOnly = true;
      personSearch.classList.add('locked');
    }
    if(personHint){
      personHint.textContent = `${data.name} انتخاب شد`;
      personHint.classList.add('selected');
    }
    if(personUnlockBtn){
      personUnlockBtn.hidden = false;
    }
    const indicator = personWrapper.querySelector('.lock-indicator');
    if(indicator){
      indicator.innerHTML = `<span>🔒 مشتری قفل شد</span>`;
    }
  }

  function unlockCustomer(){
    if(!personWrapper) return;
    personWrapper.classList.remove('locked');
    if(personSearch){
      personSearch.readOnly = false;
      personSearch.classList.remove('locked');
      personSearch.focus();
    }
    if(personUnlockBtn){
      personUnlockBtn.hidden = true;
    }
    const indicator = personWrapper.querySelector('.lock-indicator');
    if(indicator){
      indicator.textContent = '';
    }
  }

  if(personSearch){
    personSearch.addEventListener('input', function(){
      if(personSearch.readOnly) return;
      const q = (personSearch.value || '').trim();
      clearTimeout(personTimer);
      if(!q){
        hide(personBox);
        personBox.innerHTML='';
        return;
      }
      personTimer = setTimeout(()=>searchPerson(q), 160);
    });
  }

  if(personBox){
    personBox.addEventListener('click', function(ev){
      const a = ev.target.closest('a.res');
      if(!a) return;
      ev.preventDefault();
      const data = { id:a.dataset.id, name:a.dataset.name, code:a.dataset.code };
      personToken.value = data.id;
      lockCustomer(data);
      hide(personBox);
      auditLog('person-selected', data);
    });
  }

  if(personUnlockBtn){
    personUnlockBtn.addEventListener('click', function(){
      if(!doubleConfirm('آیا از تغییر مشتری اطمینان دارید؟', 'برای تغییر مشتری باید دوباره انتخاب کنید. ادامه می‌دهید؟')){
        return;
      }
      auditLog('person-unlocked', { person_id: personToken.value || null });
      personToken.value = '';
      unlockCustomer();
      personHint.textContent = 'مشتری انتخاب نشده است';
      personHint.classList.remove('selected');
    });
  }

  document.addEventListener('click', function(ev){
    if(personBox && !personBox.contains(ev.target) && ev.target !== personSearch){
      hide(personBox);
    }
  });

  const tbody = document.querySelector('#lines_tbody');
  const tpl = document.querySelector('#row_tpl');
  const rowsInput = document.querySelector('#rows');
  const grandTotal = document.querySelector('#grand_total');
  const grandWords = document.querySelector('#grand_total_words');

  function calcGrand(){
    let sum = 0;
    tbody.querySelectorAll('tr').forEach(tr => {
      const val = toNum(tr.querySelector('.line_total')?.value);
      sum += val;
    });
    if(grandTotal){ grandTotal.value = fmt3(sum); }
    if(sum > 0 && grandWords){
      fetch(`${prefix}/api/num2words?amount=${encodeURIComponent(sum.toFixed(0))}`, {credentials:'same-origin'})
        .then(r => r.ok ? r.json() : null)
        .then(res => {
          grandWords.textContent = (res && res.ok) ? res.text : '—';
        })
        .catch(()=> grandWords.textContent = '—');
    }else if(grandWords){
      grandWords.textContent = '—';
    }
  }

  function ensureNextRow(focus){
    const current = tbody.querySelectorAll('tr').length;
    if(current >= 15) return;
    if(current > 0){
      const last = tbody.querySelectorAll('tr')[current - 1];
      const tok = last.querySelector('.item_id');
      const qty = last.querySelector('.qty');
      if(!(tok && tok.value && toNum(qty?.value) > 0)) return;
    }
    addRow(focus);
  }

  function bindRow(tr){
    const searchInput = tr.querySelector('.item_search');
    const resultsBox = tr.querySelector('.item_results');
    const itemToken = tr.querySelector('.item_id');
    const unitPrice = tr.querySelector('.unit_price');
    const qty = tr.querySelector('.qty');
    const total = tr.querySelector('.line_total');
    const clearBtn = tr.querySelector('.btn_clear');
    const meta = tr.querySelector('.line-meta');

    let timer = null;

    function renderItems(rows){
      if(!Array.isArray(rows) || rows.length === 0){
        hide(resultsBox);
        resultsBox.innerHTML = '';
        return;
      }
      resultsBox.innerHTML = rows.map(r => `<a class="res" href="#" data-id="${r.id}" data-code="${r.code}" data-name="${r.name}">${r.code} — ${r.name}</a>`).join('');
      show(resultsBox);
    }

    function searchItem(q){
      fetch(`${prefix}/api/search?q=${encodeURIComponent(q)}&kind=item&limit=15`, {credentials:'same-origin'})
        .then(r => r.ok ? r.json() : [])
        .then(renderItems)
        .catch(()=>{ hide(resultsBox); resultsBox.innerHTML=''; });
    }

    if(searchInput){
      searchInput.addEventListener('input', function(){
        const q = (searchInput.value || '').trim();
        clearTimeout(timer);
        if(!q){
          hide(resultsBox);
          resultsBox.innerHTML='';
          return;
        }
        timer = setTimeout(()=>searchItem(q), 160);
      });
    }

    if(resultsBox){
      resultsBox.addEventListener('click', function(ev){
        const a = ev.target.closest('a.res');
        if(!a) return;
        ev.preventDefault();
        itemToken.value = a.dataset.id;
        searchInput.value = a.dataset.code;
        hide(resultsBox);
        auditLog('line-item-selected', { item_id: a.dataset.id, code: a.dataset.code, row: tr.dataset.rowIndex });
        if(qty && !qty.value){ qty.focus(); }
        if(meta){
          meta.textContent = `${a.dataset.name}`;
        }
      });
    }

    document.addEventListener('click', function(ev){
      if(resultsBox && !resultsBox.contains(ev.target) && ev.target !== searchInput){
        hide(resultsBox);
      }
    });

    [unitPrice, qty].forEach(inp => {
      if(!inp) return;
      inp.addEventListener('input', function(){
        const raw = inp.value.replace(/,/g,'');
        inp.value = fmt3(raw);
        const subtotal = toNum(unitPrice?.value) * toNum(qty?.value);
        if(total){ total.value = fmt3(subtotal); }
        calcGrand();
        auditLog('line-updated', {
          row: tr.dataset.rowIndex,
          item_id: itemToken.value || null,
          qty: toNum(qty?.value),
          unit_price: toNum(unitPrice?.value),
          subtotal
        });
        if(itemToken.value && toNum(qty?.value) > 0){
          ensureNextRow(false);
        }
      });
    });

    if(clearBtn){
      clearBtn.addEventListener('click', function(){
        const hasData = (itemToken?.value) || toNum(unitPrice?.value) > 0 || toNum(qty?.value) > 0;
        if(hasData){
          const ok = doubleConfirm('حذف این ردیف انجام شود؟', 'برای حذف ردیف باید تایید نهایی کنید. حذف شود؟');
          if(!ok) return;
        }
        auditLog('line-cleared', {
          row: tr.dataset.rowIndex,
          item_id: itemToken.value || null
        });
        itemToken.value='';
        if(searchInput) searchInput.value='';
        if(unitPrice) unitPrice.value='';
        if(qty) qty.value='';
        if(total) total.value='';
        if(meta) meta.textContent='';
        calcGrand();
      });
    }

    if(qty){
      qty.addEventListener('keydown', function(ev){
        if(ev.key === 'Enter'){
          ev.preventDefault();
          if(itemToken.value && toNum(qty.value) > 0){
            ensureNextRow(true);
          }
        }
      });
    }
  }

  function addRow(focus){
    if(!tpl) return;
    const node = tpl.content.firstElementChild.cloneNode(true);
    const rowIndex = tbody.querySelectorAll('tr').length + 1;
    node.dataset.rowIndex = rowIndex;
    tbody.appendChild(node);
    rowsInput.value = tbody.querySelectorAll('tr').length;
    bindRow(node);
    if(focus){
      const inp = node.querySelector('.item_search');
      if(inp) inp.focus();
    }
  }

  if(tbody && tpl){
    addRow(false);
  }

  const saleForm = document.querySelector('#sale-form');
  if(saleForm){
    saleForm.addEventListener('submit', function(){
      auditLog('invoice-submit', {
        person_id: personToken.value || null,
        row_count: tbody.querySelectorAll('tr').length,
        total: toNum(grandTotal?.value)
      });
    });
  }
})();
