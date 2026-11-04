/* Email Boss mail client: the composer, autosave, send with undo, Write with AI. */
(() => {
  const M = window.EBMailCore;
  const $ = (...a) => M.$(...a);
  const AUTOSAVE_MS = M.AUTOSAVE_MS;
  const C = M.C;
  const IMG_PROXY = M.IMG_PROXY;
  const PTR_KEY = M.PTR_KEY;
  const S = M.S;
  const UNDO_SEND_MS = M.UNDO_SEND_MS;
  const api = (...a) => M.api(...a);
  const del_ = (...a) => M.del_(...a);
  const el = (...a) => M.el(...a);
  const esc = (...a) => M.esc(...a);
  const escA = (...a) => M.escA(...a);
  const fmtSize = (...a) => M.fmtSize(...a);
  const folderKind = (...a) => M.folderKind(...a);
  const htmlToText = (...a) => M.htmlToText(...a);
  const loadCaps = (...a) => M.loadCaps(...a);
  const loadList = (...a) => M.loadList(...a);
  const looksHtml = (...a) => M.looksHtml(...a);
  const normRemote = (...a) => M.normRemote(...a);
  const note = (...a) => M.note(...a);
  const openSettingsTab = (...a) => M.openSettingsTab(...a);
  const post = (...a) => M.post(...a);
  const projectOptions = (...a) => M.projectOptions(...a);
  const put = (...a) => M.put(...a);
  const restoreRemote = (...a) => M.restoreRemote(...a);
  const sanitizeClient = (...a) => M.sanitizeClient(...a);
  const say = (...a) => M.say(...a);
  const show = (...a) => M.show(...a);
  const textToHtml = (...a) => M.textToHtml(...a);
  const toastAction = (...a) => M.toastAction(...a);

  function composerOpen() { return C.open; }
  function readPtr() { try { return JSON.parse(localStorage.getItem(PTR_KEY) || 'null'); } catch (e) { return null; } }
  function writePtr(v) { try { if (v) localStorage.setItem(PTR_KEY, JSON.stringify(v)); else localStorage.removeItem(PTR_KEY); } catch (e) { /* ignore */ } }

  async function compose(init) {
    init = init || {};
    if (!S.caps) await loadCaps();
    if (C.open && !confirm('You already have a message open. Replace it?')) return;
    stopAutosave();
    C.open = true; C.reply_ref = init.reply_ref || null; C.atts = (init.attachments || []).slice(); C.project_id = init.project_id || '';
    C.dirty = false; C.mode = init.mode || ''; C.draftId = init.draftId || null; C.last = '';
    C.alt = init.reply_to_alt || ''; C.fromDisplay = init.from_display || ''; C.useRt = !!init.use_reply_to;
    C.ctx = (init.reply_ref && S.open && S.open.ref === init.reply_ref) ? S.open : null;
    const c = (S.caps && S.caps.capabilities) || {};
    const canSend = !!(S.caps && S.caps.send_enabled && c.send);
    const box = $('mail-compose');
    box.innerHTML = `
      <div class="mc-head"><span>${esc(init.mode === 'forward' ? 'Forward' : init.reply_ref ? 'Reply' : 'New message')}</span>
        <button class="mail-btn" id="mc-close" title="Close">&#10005;</button></div>
      <div class="mc-rt" id="mc-rt" style="display:none"></div>
      <div class="mc-row"><label>To</label><input id="mc-to" value="${escA(init.to || '')}" placeholder="name@example.com, ...">
        <button class="mc-link" id="mc-cc-toggle">Cc/Bcc</button></div>
      <div class="mc-row" id="mc-cc-row" style="display:${init.cc ? 'flex' : 'none'}"><label>Cc</label><input id="mc-cc" value="${escA(init.cc || '')}"></div>
      <div class="mc-row" id="mc-bcc-row" style="display:${init.bcc ? 'flex' : 'none'}"><label>Bcc</label><input id="mc-bcc" value="${escA(init.bcc || '')}"></div>
      <div class="mc-row"><label>Subject</label><input id="mc-subject" value="${escA(init.subject || '')}"></div>
      <div class="mc-row"><label>${esc((window.ent ? ent('project') : 'Project'))}</label>
        <select id="mc-project"><option value="">(not linked)</option>${projectOptions()}</select></div>
      <div class="mc-notice" id="mc-restore" style="display:none"></div>
      <div class="mc-notice" id="mc-imgs" style="display:none"></div>
      <div class="mc-notice" id="mc-ai-note" style="display:none"></div>
      <div class="mc-body" id="mc-body" contenteditable="true"></div>
      <div class="mc-atts" id="mc-atts"></div>
      <div class="mc-foot">
        ${canSend ? `<button class="mail-btn primary" id="mc-send">Send</button>` : ''}
        <button class="mail-btn ${canSend ? '' : 'primary'}" id="mc-save" ${c.draft ? '' : 'title="Saved on this computer"'}>${canSend ? 'Save draft' : 'Save as draft'}</button>
        ${canSend ? '' : '<button class="mc-link" id="mc-turn-on">Turn on sending</button>'}
        <label class="mail-btn" style="cursor:pointer">Attach<input type="file" multiple style="display:none" id="mc-file"></label>
        <button class="mail-btn" id="mc-ai" title="Let Email Boss AI write a first draft">Write with AI</button>
        <span class="spacer"></span>
        <span class="mc-hint" id="mc-hint">${canSend ? '' : 'Sending is off, so this is saved as a draft.'}</span>
        <button class="mail-btn danger" id="mc-discard">Discard</button>
      </div>`;
    $('mc-close').addEventListener('click', () => closeCompose());
    $('mc-cc-toggle').addEventListener('click', toggleCc);
    $('mc-save').addEventListener('click', saveDraft);
    $('mc-ai').addEventListener('click', aiDraft);
    $('mc-discard').addEventListener('click', () => closeCompose(true));
    $('mc-file').addEventListener('change', (ev) => attach(ev.target));
    if ($('mc-send')) $('mc-send').addEventListener('click', send);
    if ($('mc-turn-on')) $('mc-turn-on').addEventListener('click', () => openSettingsTab('mail'));
    const body = $('mc-body');
    const quote = init.body_html ? sanitizeClient(init.body_html, true) : '';
    body.innerHTML = '<div><br></div>' + quote;
    if (C.project_id) $('mc-project').value = C.project_id;
    ['mc-to', 'mc-cc', 'mc-bcc', 'mc-subject'].forEach(id => $(id).addEventListener('input', () => C.dirty = true));
    body.addEventListener('input', () => C.dirty = true);
    renderReplyTo();
    renderImgNotice(!!init.remote_blocked || !!body.querySelector('img[data-pb-src]:not([data-pb-loaded])'));
    renderAtts();
    $('mail-compose-overlay').classList.add('open');
    setTimeout(() => { (init.to ? body : $('mc-to')).focus(); }, 30);
    startAutosave();
    if (!init.fromLocal && !init.restored) offerRestore();
  }

  function renderReplyTo() {
    const b = $('mc-rt'); if (!b) return;
    if (!C.alt) { b.style.display = 'none'; return; }
    b.style.display = 'flex'; b.textContent = '';
    if (C.useRt) {
      b.appendChild(el('span', { text: `Replies go to ${C.alt}, not the sender.` }));
      b.appendChild(el('button', { class: 'mail-btn', text: 'Reply to the sender instead', on: { click: () => { C.useRt = false; $('mc-to').value = C.fromDisplay; C.dirty = true; renderReplyTo(); } } }));
    } else {
      b.appendChild(el('span', { text: `The sender asked for replies to go to ${C.alt}. This reply goes to the sender (${C.fromDisplay}).` }));
      b.appendChild(el('button', { class: 'mail-btn', text: 'Send to ' + C.alt + ' instead', on: { click: () => { C.useRt = true; $('mc-to').value = C.alt; C.dirty = true; renderReplyTo(); } } }));
    }
  }
  function renderImgNotice(show) {
    const n = $('mc-imgs'); if (!n) return;
    if (!show) { n.style.display = 'none'; return; }
    n.style.display = 'flex'; n.textContent = '';
    n.appendChild(el('span', { text: 'Images in the quoted message are hidden.' }));
    n.appendChild(el('button', { class: 'mail-btn', text: 'Load images', on: { click: () => {
      // Through the server proxy (https only); data-pb-src keeps the real URL for sending.
      $('mc-body').querySelectorAll('img[data-pb-src]').forEach((i) => {
        const u = normRemote(i.getAttribute('data-pb-src'));
        if (/^https:/i.test(u)) { i.setAttribute('src', IMG_PROXY + encodeURIComponent(u)); i.setAttribute('data-pb-loaded', '1'); }
      });
      n.style.display = 'none';
    } } }));
  }

  // autosave to the board outbox
  function startAutosave() { stopAutosave(); C.timer = setInterval(autosaveNow, AUTOSAVE_MS); }
  function stopAutosave() { if (C.timer) { clearInterval(C.timer); C.timer = null; } }
  function composerSnapshot() {
    return { to: $('mc-to').value.trim(), cc: $('mc-cc').value.trim(), bcc: $('mc-bcc').value.trim(),
      subject: $('mc-subject').value.trim(), body: restoreRemote($('mc-body').innerHTML), project_id: $('mc-project').value || '' };
  }
  async function autosaveNow() {
    if (!C.open || !C.dirty || !$('mc-body')) return;
    const s = composerSnapshot();
    const sig = JSON.stringify(s);
    if (sig === C.last) return;
    const text = htmlToText(s.body);
    if (!s.to && !s.subject && !text) return;
    try {
      const fields = { to: s.to, cc: s.cc, subject: s.subject, body: s.body, project_id: s.project_id };
      if (C.draftId) await put('/api/drafts/' + encodeURIComponent(C.draftId), fields);
      else {
        const r = await post('/api/drafts', Object.assign({ created_by: 'user' }, fields));
        C.draftId = r.id;
      }
      C.last = sig;
      writePtr({ id: C.draftId, reply_ref: C.reply_ref, bcc: s.bcc, mode: C.mode, ts: Date.now() });
      const h = $('mc-hint'); if (h) h.textContent = 'Draft saved ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    } catch (e) { /* autosave is best effort; Save draft shows real errors */ }
  }
  async function dropAutosave() {
    const id = C.draftId; C.draftId = null; writePtr(null);
    if (id) { try { await del_('/api/drafts/' + encodeURIComponent(id)); } catch (e) { /* already gone */ } }
  }
  async function offerRestore() {
    const p = readPtr();
    if (!p || !p.id || (p.reply_ref || null) !== (C.reply_ref || null) || p.id === C.draftId) return;
    let d = null;
    try { d = (await api('/api/drafts/' + encodeURIComponent(p.id))).draft; } catch (e) { writePtr(null); return; }
    if (!d || d.status !== 'draft') { writePtr(null); return; }
    const box = $('mc-restore'); if (!box || !C.open) return;
    box.style.display = 'flex'; box.textContent = '';
    const when = d.updated_at ? new Date(d.updated_at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'earlier';
    box.appendChild(el('span', { text: `You have an unsent message from ${when}.` }));
    box.appendChild(el('button', { class: 'mail-btn primary', text: 'Restore it', on: { click: () => {
      $('mc-to').value = d.to || ''; $('mc-cc').value = d.cc || ''; $('mc-bcc').value = p.bcc || '';
      if (d.cc || p.bcc) { $('mc-cc-row').style.display = d.cc ? 'flex' : 'none'; $('mc-bcc-row').style.display = p.bcc ? 'flex' : 'none'; }
      $('mc-subject').value = d.subject || '';
      const b = d.body || ''; $('mc-body').innerHTML = sanitizeClient(looksHtml(b) ? b : textToHtml(b), true);
      if (d.project_id) $('mc-project').value = d.project_id;
      C.draftId = d.id; C.dirty = true; box.style.display = 'none';
      renderImgNotice(!!$('mc-body').querySelector('img[data-pb-src]'));
    } } }));
    box.appendChild(el('button', { class: 'mail-btn', text: 'Throw it away', on: { click: async () => {
      box.style.display = 'none'; try { await del_('/api/drafts/' + encodeURIComponent(p.id)); } catch (e) { /* gone */ } writePtr(null);
    } } }));
  }

  function toggleCc() { ['mc-cc-row', 'mc-bcc-row'].forEach(id => { const r = $(id); r.style.display = r.style.display === 'none' ? 'flex' : 'none'; }); }
  async function closeCompose(discard) {
    if (!C.open) return;
    if (discard) {
      if (C.dirty && !confirm('Discard this message?')) return;
      stopAutosave(); await dropAutosave();
    } else {
      if (C.dirty) {
        if (!confirm('Close this message? What you wrote is kept as a draft on this computer.')) return;
        await autosaveNow();
      }
      stopAutosave();
    }
    C.open = false; C.dirty = false;
    $('mail-compose-overlay').classList.remove('open');
  }
  function renderAtts() {
    const host = $('mc-atts'); if (!host) return;
    host.textContent = '';
    C.atts.forEach((a, i) => {
      host.appendChild(el('span', { class: 'mail-att' }, [
        '\ud83d\udcce ' + (a.name || 'attachment') + ' ', el('small', { text: fmtSize(a.size) }),
        el('span', { class: 'mc-att-x', text: '\u2715', on: { click: () => removeAtt(i) } })]));
    });
  }
  function removeAtt(i) { C.atts.splice(i, 1); C.dirty = true; renderAtts(); }
  function attach(input) {
    Array.from(input.files || []).forEach(f => {
      const rd = new FileReader();
      rd.onload = () => {
        const b64 = String(rd.result).split(',')[1] || '';
        C.atts.push({ name: f.name, content_type: f.type || 'application/octet-stream', size: f.size, content_b64: b64 });
        C.dirty = true; renderAtts();
      };
      rd.readAsDataURL(f);
    });
    input.value = '';
  }
  function payload() {
    return {
      to: $('mc-to').value.trim(), cc: $('mc-cc').value.trim(), bcc: $('mc-bcc').value.trim(),
      subject: $('mc-subject').value.trim(), body_html: restoreRemote($('mc-body').innerHTML),
      reply_ref: C.reply_ref, attachments: C.atts, project_id: $('mc-project').value || '',
    };
  }
  function addresses(p) {
    const seen = new Set(); const out = [];
    [p.to, p.cc, p.bcc].forEach((s) => (String(s || '').match(/[^\s<>",;]+@[^\s<>",;]+/g) || []).forEach((a) => {
      const k = a.toLowerCase(); if (!seen.has(k)) { seen.add(k); out.push(a); }
    }));
    return out;
  }
  function confirmText(p, mode) {
    const list = addresses(p);
    if (mode === 'reply_all' && list.length > 3) return `Reply to all ${list.length} people?\n\n${list.join('\n')}\n\nSend now?`;
    if (list.length <= 1) return `Send to ${list[0] || 'this recipient'}?`;
    const n = list.length - 1;
    return `Send to ${list[0]} and ${n} other${n === 1 ? '' : 's'}?`;
  }
  async function reply(mode) {
    if (!S.open) return;
    try {
      const r = await post('/api/mail/client/compose', { ref: S.open.ref, mode });
      await compose(Object.assign({ mode }, r));
    } catch (e) { say(e.message); }
  }

  // Send: confirm, close, wait 10 seconds with Undo, then POST.
  async function send() {
    const p = payload();
    if (!addresses(p).length) { say('Add someone to send this to'); return; }
    if (!p.subject && !confirm('Send with no subject?')) return;
    if (!confirm(confirmText(p, C.mode))) return;
    const snap = { to: p.to, cc: p.cc, bcc: p.bcc, subject: p.subject, body_html: p.body_html, project_id: p.project_id,
      reply_ref: p.reply_ref, attachments: p.attachments, mode: C.mode, draftId: C.draftId, restored: true };
    stopAutosave();
    C.open = false; C.dirty = false;
    $('mail-compose-overlay').classList.remove('open');
    const item = { p, snap, done: false };
    item.timer = setTimeout(() => finishSend(item), UNDO_SEND_MS);
    item.toast = toastAction('Sending in 10 seconds...', 'Undo', () => undoSend(item), UNDO_SEND_MS);
    S.pending.push(item);
  }
  function undoSend(item) {
    if (item.done) return;
    item.done = true; clearTimeout(item.timer);
    S.pending = S.pending.filter(x => x !== item);
    compose(Object.assign({}, item.snap, { body_html: item.snap.body_html })).then(() => { C.dirty = true; say('Not sent. Your message is back.'); });
  }
  async function finishSend(item) {
    if (item.done) return;
    item.done = true;
    S.pending = S.pending.filter(x => x !== item);
    try {
      const r = await post('/api/mail/client/send', item.p);
      say('Sent' + (r.refused && Object.keys(r.refused).length ? ' (some recipients refused)' : ''));
      if (item.snap.draftId) { try { await del_('/api/drafts/' + encodeURIComponent(item.snap.draftId)); } catch (e) { /* gone */ } }
      const ptr = readPtr(); if (ptr && ptr.id === item.snap.draftId) writePtr(null);
      if (item.p.project_id && window.loadData) loadData();
    } catch (e) {
      say('Not sent: ' + e.message);
      if (!C.open) { await compose(item.snap); C.dirty = true; }
    }
  }
  async function saveDraft() {
    const p = payload();
    try {
      const r = await post('/api/mail/client/draft', p);
      say(r.where === 'provider' ? 'Saved to Drafts' : 'Saved on this computer');
      stopAutosave(); await dropAutosave();
      C.dirty = false; C.open = false;
      $('mail-compose-overlay').classList.remove('open');
      if (folderKind() === 'drafts') loadList(true, true);
    } catch (e) { say(e.message); }
  }

  // Write with AI
  function aiNotice(text, linkLabel) {
    const n = $('mc-ai-note'); if (!n) return;
    n.style.display = 'flex'; n.textContent = '';
    n.appendChild(el('span', { text }));
    if (linkLabel) n.appendChild(el('button', { class: 'mc-link', text: linkLabel, on: { click: () => openSettingsTab('ai') } }));
  }
  async function aiDraft() {
    const btn = $('mc-ai'); if (!btn || btn.disabled) return;
    const m = C.ctx;
    const mine = (() => { const d = $('mc-body').cloneNode(true); d.querySelectorAll('.pb-quote,.pb-forward').forEach(x => x.remove()); return (d.textContent || '').trim(); })();
    const req = m ? {
      subject: m.subject || '', body: htmlToText(m.is_html ? m.body : textToHtml(m.body)).slice(0, 6000),
      from_name: m.from_name || String(m.from || '').replace(/<.*$/, '').trim(), from_email: m.from_email || '',
      project_id: $('mc-project').value || undefined, direction_note: mine ? 'Points to cover: ' + mine : '',
    } : {
      subject: $('mc-subject').value.trim(), body: mine, from_name: '', from_email: '',
      project_id: $('mc-project').value || undefined, direction_note: 'Write a short email to the recipient from these notes.',
    };
    if (!m && !mine && !req.subject) { aiNotice('Type a few words about what you want to say, then press Write with AI.'); return; }
    btn.disabled = true; const old = btn.textContent; btn.textContent = 'Writing...';
    $('mc-ai-note').style.display = 'none';
    try {
      const r = await post('/api/ai/draft-reply', req);
      const txt = String(r.draft || '').trim();
      if (!txt) { aiNotice('Email Boss AI did not come back with anything. Try again.'); return; }
      const body = $('mc-body');
      const first = body.firstElementChild;
      const blk = document.createElement('div'); blk.innerHTML = esc(txt).replace(/\r?\n/g, '<br>');
      if (first && !first.textContent.trim() && !first.querySelector('img')) body.replaceChild(blk, first); else body.insertBefore(blk, body.firstChild);
      C.dirty = true;
    } catch (e) {
      const b = (e.body && e.body.error) || e.message || '';
      if (e.status === 503 || e.status === 402 || e.status === 403 || /offline|credit|turned off|not enabled/i.test(b)) {
        aiNotice('Email Boss AI is off or out of credits.', 'Open Settings > AI');
      } else aiNotice(e.message);
    } finally { btn.disabled = false; btn.textContent = old; }
  }

  Object.assign(M, { addresses, aiDraft, aiNotice, attach, autosaveNow, closeCompose, compose, composerOpen, composerSnapshot, confirmText, dropAutosave, finishSend, offerRestore, payload, readPtr, removeAtt, renderAtts, renderImgNotice, renderReplyTo, reply, saveDraft, send, startAutosave, stopAutosave, toggleCc, undoSend, writePtr });
})();
