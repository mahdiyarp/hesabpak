// Frontend widget: fetch live rates and update dashboard labels
(function(){
  const PREFIX = (typeof window.prefix === 'string' ? window.prefix : (window.APP_PREFIX || '')) || '';

  async function fetchRates(){
    try{
      const r = await fetch(PREFIX + '/api/rates');
      if(!r.ok) return null;
      return await r.json();
    }catch(e){ console.warn('rates fetch failed', e); return null; }
  }

  function fmt(n){
    try{ return new Intl.NumberFormat('fa-IR').format(Number(n)); }catch(e){ return n || '—'; }
  }

  async function updateRatesOnce(el){
    const data = await fetchRates();
    if(!data) return;
    const cur = data.currencies || {};
    const usd = (cur.USD && cur.USD.rate) ? cur.USD.rate : null;
    const eur = (cur.EUR && cur.EUR.rate) ? cur.EUR.rate : null;
    const gold = data.gold || {};
    const gram18 = gold.gram_18 || null;
    const coin = gold.coin_full || null;

    const usdLbl = el.querySelector('.rate-usd');
    const eurLbl = el.querySelector('.rate-eur');
    const gramLbl = el.querySelector('.rate-gram18');
    const coinLbl = el.querySelector('.rate-coin');
    const updatedLbl = el.querySelector('.rates-updated');

    if(usdLbl) usdLbl.textContent = usd ? fmt(usd) + ' تومان' : '—';
    if(eurLbl) eurLbl.textContent = eur ? fmt(eur) + ' تومان' : '—';
    if(gramLbl) gramLbl.textContent = gram18 ? fmt(gram18) + ' تومان' : '—';
    if(coinLbl) coinLbl.textContent = coin ? fmt(coin) + ' تومان' : '—';
    if(updatedLbl) updatedLbl.textContent = (data.rates && data.rates.updated_at) ? data.rates.updated_at : (data.updated_at || '—');
  }

  async function init(el){
    // initial load
    await updateRatesOnce(el);
    // poll every 30s to refresh UI (frontend polling)
    setInterval(()=>{ updateRatesOnce(el); }, 30 * 1000);
  }

  document.addEventListener('DOMContentLoaded', ()=>{
    const widgets = document.querySelectorAll('.rates-widget');
    widgets.forEach(w=>{ try{ init(w); }catch(e){console.warn(e);} });
  });
})();
