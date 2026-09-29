(function(){
  const prefix = window.APP_PREFIX || '';
  const form = document.getElementById('assistant-form');
  if(!form){ return; }

  // Prevent double-initialization if the script is loaded more than once
  try{
    if(form.dataset.hspAssistantInitialized === '1'){
      // already initialized
      console.debug('assistant: form already initialized, skipping second init');
      return;
    }
    form.dataset.hspAssistantInitialized = '1';
  }catch(e){ /* ignore */ }

  const input = document.getElementById('assistant-input');
  const fileInput = document.getElementById('assistant-file');
  const messagesEl = document.getElementById('assistant-messages');
  const previewCard = document.getElementById('assistant-preview');
  const previewTableBody = previewCard ? previewCard.querySelector('tbody') : null;
  const confirmBtn = document.getElementById('assistant-confirm');
  const cancelBtn = document.getElementById('assistant-cancel');
  const missingEl = document.getElementById('assistant-missing');
  const errorEl = document.getElementById('assistant-error');
  const actionPreviewEl = document.getElementById('assistant-action-preview');
  const actionListEl = document.getElementById('assistant-action-list');
  const previewTitleEl = previewCard ? previewCard.querySelector('[data-preview-title]') : null;

  let history = [];
  let pendingTicket = null;
  let isSending = false;
  // guard to avoid concurrent send calls from duplicate listeners
  let lastSendNonce = null;

  function setEmptyState(){
    if(!messagesEl){ return; }
    const hasChildren = messagesEl.querySelector('.assistant-message');
    messagesEl.classList.toggle('is-empty', !hasChildren);
  }

  function appendMessage(role, text){
    if(!messagesEl){ return; }
    const item = document.createElement('div');
    item.className = 'assistant-message ' + (role === 'user' ? 'user' : 'assistant');
    const bubble = document.createElement('div');
    bubble.className = 'assistant-bubble';
    bubble.textContent = text || '';
    item.appendChild(bubble);
    messagesEl.appendChild(item);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    setEmptyState();
  }

  function setLoading(state){
    isSending = state;
    form.querySelector('button[type="submit"]').disabled = state;
    if(state){
      form.classList.add('loading');
    }else{
      form.classList.remove('loading');
    }
  }

  function clearPreview(){
    if(!previewCard) return;
    previewCard.hidden = true;
    pendingTicket = null;
    if(previewTableBody){ previewTableBody.replaceChildren(); }
    if(actionListEl){ actionListEl.replaceChildren(); }
    if(actionPreviewEl){ actionPreviewEl.hidden = true; }
    if(previewTitleEl){ previewTitleEl.textContent = 'پیش‌نمایش فاکتور'; }
    previewCard.querySelectorAll('[data-preview-section]').forEach(el => { el.hidden = false; });
    ['kind','partner','date','number','total'].forEach(field=>{
      const el = previewCard.querySelector(`[data-field="${field}"]`);
      if(el){ el.textContent = '—'; }
    });
    if(missingEl){ missingEl.hidden = true; missingEl.replaceChildren(); }
    if(errorEl){ errorEl.hidden = true; errorEl.textContent = ''; }
    if(confirmBtn){ confirmBtn.textContent = 'تأیید و ثبت در سیستم'; }
  }

  function formatNumber(val){
    try{
      return Number(val || 0).toLocaleString('fa-IR', {maximumFractionDigits:2});
    }catch(e){ return val; }
  }

  function renderPreview(data, applyError){
    if(!previewCard || !data) return;
    previewCard.hidden = false;
    if(actionPreviewEl){ actionPreviewEl.hidden = true; }
    if(previewTitleEl){ previewTitleEl.textContent = 'پیش‌نمایش فاکتور'; }
    previewCard.querySelectorAll('[data-preview-section]').forEach(el => { el.hidden = false; });
    if(previewTableBody){ previewTableBody.replaceChildren(); }

    const kindEl = previewCard.querySelector('[data-field="kind"]');
    const partnerEl = previewCard.querySelector('[data-field="partner"]');
    const dateEl = previewCard.querySelector('[data-field="date"]');
    const numberEl = previewCard.querySelector('[data-field="number"]');
    const totalEl = previewCard.querySelector('[data-field="total"]');

    if(kindEl) kindEl.textContent = data.kind === 'purchase' ? 'خرید' : 'فروش';
    if(partnerEl){
      let label = data.partner && data.partner.name ? String(data.partner.name) : '—';
      if(data.partner && data.partner.exists === false) label += ' (جدید)';
      partnerEl.textContent = label;
    }
    if(dateEl) dateEl.textContent = data.date ? String(data.date) : '—';
    if(numberEl) numberEl.textContent = data.number ? String(data.number) : '—';
    if(totalEl) totalEl.textContent = formatNumber(data.total || 0);

    if(previewTableBody && Array.isArray(data.items)){
      data.items.forEach(item=>{
        const tr=document.createElement('tr');
        const vals=[item.name || '—',formatNumber(item.qty || 0),formatNumber(item.unit_price || 0),formatNumber(item.line_total || 0),item.exists ? 'ثبت شده' : 'جدید'];
        vals.forEach((v,i)=>{
          const td=document.createElement('td');
          td.textContent=String(v);
          if(i===4) td.className=item.exists ? 'ok' : 'new';
          tr.appendChild(td);
        });
        previewTableBody.appendChild(tr);
      });
    }

    if(missingEl){
      const warnings=[];
      if(data.missing_partner) warnings.push('مشتری/تأمین‌کننده جدید: ' + (data.missing_partner.name || 'بدون نام'));
      if(Array.isArray(data.missing_items) && data.missing_items.length) warnings.push(String(data.missing_items.length) + ' قلم جدید ایجاد می‌شود.');
      missingEl.replaceChildren();
      if(warnings.length){
        missingEl.hidden=false;
        warnings.forEach(w=>{ const row=document.createElement('div'); row.textContent='⚠️ ' + w; missingEl.appendChild(row); });
      }else missingEl.hidden=true;
    }

    if(errorEl){
      errorEl.hidden = !applyError;
      errorEl.textContent = applyError ? String(applyError) : '';
    }
    if(confirmBtn){ confirmBtn.textContent='تأیید و ثبت در سیستم'; }
  }

  function renderActionPreview(actions){
    if(!previewCard || !actionPreviewEl || !actionListEl) return;
    previewCard.hidden=false;
    if(previewTitleEl) previewTitleEl.textContent='پیش‌نمایش تغییرات پروژه';
    previewCard.querySelectorAll('[data-preview-section="invoice"]').forEach(el=>{ el.hidden=true; });
    if(missingEl){ missingEl.hidden=true; missingEl.replaceChildren(); }
    if(errorEl){ errorEl.hidden=true; errorEl.textContent=''; }
    actionListEl.replaceChildren();
    (Array.isArray(actions) ? actions : []).forEach(action=>{
      const row=document.createElement('div');
      row.className='assistant-action-card';
      const op=document.createElement('strong');
      op.textContent=String(action.operation || action.type || 'operation');
      const path=document.createElement('code');
      path.textContent=String(action.path || '—');
      const desc=document.createElement('div');
      desc.className='muted';
      desc.textContent=String(action.description || 'بدون توضیح');
      row.append(op,path,desc);
      actionListEl.appendChild(row);
    });
    actionPreviewEl.hidden=false;
    if(confirmBtn){
      confirmBtn.textContent='تأیید و اعمال تغییرات';
      confirmBtn.disabled=!pendingTicket;
    }
  }

  function readFile(file){
    return new Promise((resolve, reject)=>{
      const reader = new FileReader();
      reader.onload = ()=>{
        const base64 = (reader.result || '').toString().split(',').pop();
        resolve({
          type: 'image',
          name: file.name,
          mime_type: file.type || 'image/png',
          data: base64
        });
      };
      reader.onerror = ()=> reject(reader.error || new Error('خواندن فایل ناموفق بود'));
      reader.readAsDataURL(file);
    });
  }

  async function sendMessage(){
    if(isSending){ return; }
    const text = (input.value || '').trim();
    const file = fileInput && fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
    if(!text && !file){ return; }

    // Prevent accidental duplicate sends: generate a nonce and ignore repeated calls
    const nonce = Date.now() + '-' + Math.random().toString(36).slice(2,8);
    if(lastSendNonce === nonce){ return; }
    lastSendNonce = nonce;

    const message = { role: 'user', text: text, attachments: [] };
    appendMessage('user', text || 'در حال ارسال تصویر...');

    if(file){
      if(file.size > 2 * 1024 * 1024){
        appendMessage('assistant', 'حجم فایل بیش از ۲ مگابایت است. لطفاً فایل کوچک‌تری ارسال کنید.');
        return;
      }
      try{
        const attachment = await readFile(file);
        message.attachments.push(attachment);
      }catch(err){
        appendMessage('assistant', 'خواندن فایل ناموفق بود.');
        return;
      }
    }

  history.push(message);
    setLoading(true);
    clearPreview();

    try{
      const response = await fetch(`${prefix}/assistant/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history })
      });
      // Reset lastSendNonce on success/failure so new sends are permitted
      lastSendNonce = null;
      const data = await response.json();
      if(!response.ok || data.status !== 'ok'){
        throw new Error(data.message || 'پاسخ نامعتبر از سرور');
      }
      const reply = data.reply || 'پاسخی دریافت نشد.';
      appendMessage('assistant', reply);

      if(data.applied){
        history.push({ role: 'assistant', text: reply, attachments: [] });
      }else{
        history.push({ role: 'assistant', text: reply, attachments: [] });
      }

      if(data.invoice_preview && !data.applied){
        pendingTicket = data.ticket || null;
        renderPreview(data.invoice_preview, data.apply_error);
        if(confirmBtn){ confirmBtn.disabled = !pendingTicket; }
      }else if(data.actions_preview && !data.actions_applied){
        pendingTicket = data.ticket || null;
        renderActionPreview(data.actions_preview);
        if(confirmBtn){ confirmBtn.disabled = !pendingTicket; }
      }

      if(data.applied){
        clearPreview();
      }

      if(data.apply_error && !data.invoice_preview){
        appendMessage('assistant', `خطا در ثبت خودکار: ${data.apply_error}`);
      }

      if(data.follow_up){
        appendMessage('assistant', data.follow_up);
        history.push({ role: 'assistant', text: data.follow_up, attachments: [] });
      }

      if(data.actions_summary){
        const summary = data.actions_summary;
        if(Array.isArray(summary.messages)){
          summary.messages.forEach(msg => {
            appendMessage('assistant', msg);
            history.push({ role: 'assistant', text: msg, attachments: [] });
          });
        }
        if(Array.isArray(summary.errors)){
          summary.errors.forEach(err => {
            const text = `خطا در اعمال تغییرات: ${err}`;
            appendMessage('assistant', text);
            history.push({ role: 'assistant', text, attachments: [] });
          });
        }
      }

    }catch(err){
      appendMessage('assistant', err.message || 'خطا در ارتباط با سرور');
      lastSendNonce = null;
    }finally{
      setLoading(false);
      input.value = '';
      if(fileInput){ fileInput.value = ''; }
    }
  }

  form.addEventListener('submit', function(ev){
    ev.preventDefault();
    sendMessage();
  });

  if(cancelBtn){
    cancelBtn.addEventListener('click', function(){
      clearPreview();
    });
  }

  if(confirmBtn){
    confirmBtn.addEventListener('click', async function(){
      if(!pendingTicket){ return; }
      confirmBtn.disabled = true;
      try{
        const response = await fetch(`${prefix}/assistant/api/apply`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticket: pendingTicket })
        });
        const data = await response.json();
        if(!response.ok || data.status !== 'ok'){
          throw new Error(data.message || 'امکان ثبت فاکتور نبود.');
        }
        if(data.actions_summary){
          const msg='تغییرات پروژه با موفقیت اعمال شد.';
          appendMessage('assistant', msg);
          history.push({ role:'assistant', text:msg, attachments:[] });
          if(Array.isArray(data.actions_summary.messages)){
            data.actions_summary.messages.forEach(item=>{
              const text=String(item);
              appendMessage('assistant', text);
              history.push({ role:'assistant', text, attachments:[] });
            });
          }
        }else{
          const msg=`فاکتور «${data.invoice_number}» با موفقیت ثبت شد.`;
          appendMessage('assistant', msg);
          history.push({ role:'assistant', text:msg, attachments:[] });
        }
        clearPreview();
      }catch(err){
        if(errorEl){
          errorEl.hidden = false;
          errorEl.textContent = err.message || 'خطایی رخ داد.';
        }
      }finally{
        confirmBtn.disabled = false;
      }
    });
  }

  setEmptyState();
})();
