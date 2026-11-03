/* Email Boss mail client (the MAIL view).
 *
 * Talks only to /api/mail/client/*. The board's own fetch wrapper adds the
 * bearer token. Everything here is idempotent: renderBoard() calls
 * PBMail.show() on every 30 s cycle and that must not rebuild the pane or
 * lose the open message / composer.
 *
 * Bodies render inside a sandboxed iframe (no scripts, no same-origin), with
 * cid: images rewritten to the attachment endpoint. Sending is a 403 from the
 * server until mail.send_enabled is on; the composer reads that from
 * /api/mail/client/capabilities and shows Save draft only.
 */
window.PBMail = (() => {
  const S = {
    built: false, caps: null, folders: [], folder: null, rows: [], cursor: null, query: '',
    open: null, openRef: null, loading: false, timer: null, lastList: 0,
  };
  const esc = (s) => (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escA = (s) => esc(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const $ = (id) => document.getElementById(id);
  const say = (m) => { try { toast(m); } catch (e) { console.log(m); } };

  async function api(path, opts) {
    const r = await fetch(path, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { j = { ok: false, error: `HTTP ${r.status}` }; }
    if (!r.ok || j.ok === false) {
      const err = new Error(j.error || `HTTP ${r.status}`);
      err.status = r.status; err.body = j;
      throw err;
    }
    return j;
  }
  const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  // ── shell ──
  function build() {
    const host = $('mail-view');
    if (!host || S.built) return;
    host.innerHTML = `
      <div class="mail-shell">
        <aside class="mail-folders" id="mail-folders"></aside>
        <section class="mail-list">
          <div class="mail-list-bar">
            <input type="text" id="mail-search" placeholder="Search this folder..." autocomplete="off">
            <button class="mail-btn" onclick="PBMail.refresh()" title="Reload">&#8635;</button>
          </div>
          <div class="mail-rows" id="mail-rows"><div class="mail-empty">Loading...</div></div>
        </section>
        <section class="mail-read" id="mail-read"><div class="mail-read-empty">Select a message</div></section>
      </div>
      <div class="mail-compose-overlay" id="mail-compose-overlay">
        <div class="mail-compose" id="mail-compose"></div>
      </div>`;
    const search = $('mail-search');
    let t = null;
    search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { S.query = search.value.trim(); loadList(true); }, 350); });
    search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { search.value = ''; S.query = ''; loadList(true); } });
    $('mail-compose-overlay').addEventListener('click', (e) => { if (e.target.id === 'mail-compose-overlay') { /* click outside keeps the draft */ } });
    S.built = true;
  }

  async function show() {
    build();
    const host = $('mail-view');
    if (!host) return;
    host.style.display = 'flex';
    if (!S.caps) await loadCaps();
    if (S.caps && S.caps.client_enabled && S.caps.provider !== 'none' && !S.folders.length) await loadFolders();
    else if (Date.now() - S.lastList > 120000 && S.folder) loadList(false, true);
    if (!S.timer) S.timer = setInterval(() => { if (host.style.display !== 'none' && S.folder && !composerOpen()) loadList(false, true); }, 90000);
  }
  function hide() { const host = $('mail-view'); if (host) host.style.display = 'none'; }

  async function loadCaps() {
    try { S.caps = await api('/api/mail/client/capabilities'); }
    catch (e) { S.caps = { client_enabled: false, provider: 'none', capabilities: {}, error: e.message }; }
    if (!S.caps.client_enabled || S.caps.provider === 'none' || S.caps.error) {
      const why = S.caps.error ? esc(S.caps.error)
        : S.caps.provider === 'none' ? 'No mail provider is configured.'
        : 'The mail client is turned off.';
      $('mail-rows').innerHTML = `<div class="mail-empty">${why}<br><br><button class="mail-btn" onclick="openSettings('mail')">Open Settings &rsaquo; Mail</button></div>`;
      $('mail-folders').innerHTML = '';
    } else if (S.caps.capabilities && !S.caps.capabilities.folders) {
      $('mail-rows').innerHTML = `<div class="mail-empty">The ${esc(S.caps.provider)} provider captures mail for triage but cannot browse a mailbox.<br>Use IMAP, Gmail or Microsoft 365 for the mail client.</div>`;
    }
  }

  // ── folders ──
  async function loadFolders() {
    try {
      const r = await api('/api/mail/client/folders');
      S.folders = r.folders || [];
    } catch (e) {
      $('mail-folders').innerHTML = '';
      $('mail-rows').innerHTML = `<div class="mail-empty">${esc(e.message)}${e.status === 401 ? '<br><br><button class="mail-btn" onclick="openSettings(\'mail\')">Sign in: Settings &rsaquo; Mail</button>' : ''}</div>`;
      return;
    }
    renderFolders();
    if (!S.folder) {
      const inbox = S.folders.find(f => f.kind === 'inbox') || S.folders[0];
      if (inbox) selectFolder(inbox.id);
    }
  }
  const kindIcon = { inbox: '&#9993;', drafts: '&#9998;', sent: '&#10148;', archive: '&#9635;', junk: '&#9888;', trash: '&#128465;', other: '&#9656;' };
  function renderFolders() {
    const el = $('mail-folders');
    if (!el) return;
    let h = `<div class="mf-head"><span>FOLDERS</span><span title="Reload folders" style="cursor:pointer" onclick="PBMail.reloadFolders()">&#8635;</span></div>`;
    const canCompose = S.caps && S.caps.capabilities && (S.caps.capabilities.draft || S.caps.capabilities.send);
    if (canCompose) h += `<button class="mail-compose-btn" onclick="PBMail.compose()">+ New message</button>`;
    S.folders.forEach(f => {
      if (f.noselect) return;
      const unread = f.unread || 0;
      h += `<div class="mail-folder k-${f.kind} ${f.id === S.folder ? 'active' : ''} ${unread ? 'has-unread' : ''}" title="${escA(f.path || f.name)}" onclick="PBMail.selectFolder('${escA(f.id)}')">
        <span class="mf-name">${kindIcon[f.kind] || kindIcon.other} ${esc(f.name)}</span>
        <span class="mf-count">${unread ? unread : (f.total != null && f.kind !== 'inbox' ? '' : '')}</span></div>`;
    });
    el.innerHTML = h;
  }
  async function reloadFolders() { S.folders = []; await loadFolders(); }
  function selectFolder(id) {
    S.folder = id; S.cursor = null; S.rows = []; S.query = '';
    const sb = $('mail-search'); if (sb) sb.value = '';
    renderFolders();
    loadList(true);
  }

  // ── list ──
  async function loadList(reset, quiet) {
    if (!S.folder || S.loading) return;
    S.loading = true;
    if (reset && !quiet) $('mail-rows').innerHTML = '<div class="mail-empty">Loading...</div>';
    try {
      const q = new URLSearchParams({ folder: S.folder, limit: '50' });
      if (S.query) q.set('q', S.query);
      if (!reset && S.cursor) q.set('cursor', S.cursor);
      const r = await api('/api/mail/client/messages?' + q.toString());
      S.rows = (reset || !S.cursor) ? (r.messages || []) : S.rows.concat(r.messages || []);
      S.cursor = r.next_cursor || null;
      S.lastList = Date.now();
      renderRows();
    } catch (e) {
      $('mail-rows').innerHTML = `<div class="mail-empty">${esc(e.message)}</div>`;
    } finally { S.loading = false; }
  }
  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return iso;
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return d.toLocaleDateString([], { year: '2-digit', month: 'short', day: 'numeric' });
  }
  function renderRows() {
    const el = $('mail-rows');
    if (!el) return;
    if (!S.rows.length) { el.innerHTML = `<div class="mail-empty">${S.query ? 'No matches.' : 'Nothing here.'}</div>`; return; }
    const isSent = (S.folders.find(f => f.id === S.folder) || {}).kind === 'sent';
    let h = '';
    S.rows.forEach(m => {
      const who = isSent ? ('To: ' + (m.to || '')) : (m.from || m.from_email || '');
      h += `<div class="mail-row ${m.is_read ? '' : 'unread'} ${m.ref === S.openRef ? 'active' : ''}" data-ref="${escA(m.ref)}" onclick="PBMail.openMessage(this.dataset.ref)">
        <div class="mr-from">${esc(who)}</div><div class="mr-date">${esc(fmtDate(m.date))}</div>
        <div class="mr-subj">${esc(m.subject || '(no subject)')}</div>
        <div class="mr-icons">${m.flagged ? '<span class="flag">&#9873;</span> ' : ''}${m.has_attachments ? '&#128206;' : ''}</div>
        ${m.preview ? `<div class="mr-prev">${esc(m.preview)}</div>` : ''}
      </div>`;
    });
    if (S.cursor) h += `<div class="mail-more"><button class="mail-btn" onclick="PBMail.more()">Load more</button></div>`;
    el.innerHTML = h;
  }
  function more() { loadList(false); }
  function refresh() { S.cursor = null; loadList(true); reloadFolders(); }

  // ── reading pane ──
  async function openMessage(ref) {
    S.openRef = ref;
    renderRows();
    const pane = $('mail-read');
    pane.innerHTML = '<div class="mail-read-empty">Loading...</div>';
    try {
      S.open = await api('/api/mail/client/message?ref=' + encodeURIComponent(ref) + '&mark_read=1');
    } catch (e) {
      pane.innerHTML = `<div class="mail-read-empty">${esc(e.message)}</div>`;
      return;
    }
    const row = S.rows.find(r => r.ref === ref);
    if (row && !row.is_read) { row.is_read = true; bumpUnread(-1); renderRows(); }
    renderOpen();
  }
  function bumpUnread(n) {
    const f = S.folders.find(x => x.id === S.folder);
    if (f && f.unread != null) { f.unread = Math.max(0, f.unread + n); renderFolders(); }
  }
  function projectOptions() {
    // DATA is a top-level `let` in board.html (not a window property), so test by name.
    const ps = (typeof DATA !== 'undefined' && DATA && DATA.projects) ? DATA.projects.slice() : [];
    ps.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    return ps.map(p => `<option value="${escA(p.id)}">${esc(p.name || p.id)}</option>`).join('');
  }
  function bodyDoc(m) {
    let html = m.is_html ? (m.body || '') : ('<pre style="white-space:pre-wrap;font-family:inherit">' + esc(m.body || '') + '</pre>');
    (m.attachments || []).forEach(a => {
      if (a.content_id && a.url) html = html.split('cid:' + a.content_id).join(a.url);
    });
    return `<!doctype html><html><head><meta charset="utf-8"><base target="_blank">
      <style>body{font-family:-apple-system,'IBM Plex Sans',Segoe UI,sans-serif;font-size:14px;line-height:1.45;color:#111;margin:16px;word-break:break-word}
      img{max-width:100%;height:auto} blockquote{border-left:2px solid #bbb;margin:8px 0;padding-left:10px;color:#555} pre{white-space:pre-wrap}</style></head>
      <body>${html}</body></html>`;
  }
  function renderOpen() {
    const m = S.open; const pane = $('mail-read');
    if (!m) { pane.innerHTML = '<div class="mail-read-empty">Select a message</div>'; return; }
    const c = (S.caps && S.caps.capabilities) || {};
    const files = (m.attachments || []).filter(a => !a.is_inline);
    const when = m.date ? new Date(m.date).toLocaleString() : '';
    pane.innerHTML = `
      <div class="mail-read-head">
        <h2 class="mail-subject">${esc(m.subject || '(no subject)')}</h2>
        <div class="mail-meta"><b>${esc(m.from || m.from_email)}</b> &nbsp;<span style="color:#6b6966">${esc(when)}</span><br>
          To: ${esc(m.to || '')}${m.cc ? '<br>Cc: ' + esc(m.cc) : ''}</div>
        <div class="mail-actions">
          <button class="mail-btn primary" onclick="PBMail.reply('reply')" ${c.draft || c.send ? '' : 'disabled'}>Reply</button>
          <button class="mail-btn" onclick="PBMail.reply('reply_all')" ${c.draft || c.send ? '' : 'disabled'}>Reply all</button>
          <button class="mail-btn" onclick="PBMail.reply('forward')" ${c.draft || c.send ? '' : 'disabled'}>Forward</button>
          ${c.flags ? `<button class="mail-btn" onclick="PBMail.flag(${m.flagged ? 'false' : 'true'})">${m.flagged ? 'Unflag' : 'Flag'}</button>
          <button class="mail-btn" onclick="PBMail.markRead(false)">Unread</button>` : ''}
          ${c.move ? `<button class="mail-btn" onclick="PBMail.move('archive')">Archive</button>` : ''}
          ${c.delete ? `<button class="mail-btn danger" onclick="PBMail.del()">Delete</button>` : ''}
          <span class="sep" style="margin-left:auto"></span>
          <select class="mail-file-select" id="mail-file-proj"><option value="">File to ${esc((window.ent ? ent('project') : 'project')).toLowerCase()}...</option>${projectOptions()}</select>
          <button class="mail-btn" onclick="PBMail.fileTo()">File</button>
          <button class="mail-btn" onclick="PBMail.stage()" title="Put this message on the triage panel">Triage</button>
        </div>
      </div>
      ${files.length ? `<div class="mail-attachments">${files.map(a => `<a class="mail-att" href="${escA(a.url)}" target="_blank" rel="noopener">&#128206; ${esc(a.name)} <small>${fmtSize(a.size)}</small></a>`).join('')}</div>` : ''}
      <div class="mail-body" id="mail-body"></div>
      <div class="mail-note" id="mail-note"></div>`;
    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', '');
    iframe.setAttribute('referrerpolicy', 'no-referrer');
    iframe.srcdoc = bodyDoc(m);
    $('mail-body').appendChild(iframe);
  }
  function fmtSize(n) { n = +n || 0; return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? Math.round(n / 1024) + ' KB' : n + ' B'; }
  function note(t) { const el = $('mail-note'); if (el) el.textContent = t || ''; }

  async function flag(on) {
    if (!S.open) return;
    try { await post('/api/mail/client/flags', { ref: S.open.ref, flagged: on }); S.open.flagged = on;
      const row = S.rows.find(r => r.ref === S.open.ref); if (row) row.flagged = on; renderRows(); renderOpen(); }
    catch (e) { say(e.message); }
  }
  async function markRead(on) {
    if (!S.open) return;
    try { await post('/api/mail/client/flags', { ref: S.open.ref, read: on });
      const row = S.rows.find(r => r.ref === S.open.ref); if (row && row.is_read !== on) { row.is_read = on; bumpUnread(on ? -1 : 1); }
      renderRows(); say(on ? 'Marked read' : 'Marked unread'); }
    catch (e) { say(e.message); }
  }
  function dropOpen() {
    S.rows = S.rows.filter(r => r.ref !== S.openRef);
    S.open = null; S.openRef = null;
    renderRows(); renderOpen();
  }
  async function move(folder) {
    if (!S.open) return;
    try { await post('/api/mail/client/move', { ref: S.open.ref, folder }); say('Moved'); dropOpen(); }
    catch (e) { say(e.message); }
  }
  async function del() {
    if (!S.open) return;
    try { await post('/api/mail/client/delete', { ref: S.open.ref }); say('Moved to Trash'); dropOpen(); }
    catch (e) { say(e.message); }
  }
  async function fileTo() {
    const sel = $('mail-file-proj'); const pid = sel && sel.value;
    if (!S.open || !pid) { say('Pick a project first'); return; }
    try { const r = await post('/api/mail/client/file', { ref: S.open.ref, project_id: pid });
      say('Filed'); note('Filed to ' + (sel.options[sel.selectedIndex].text) + ' (correspondence ' + (r.correspondence_id || '') + ')');
      if (window.loadData) loadData(); }
    catch (e) { say(e.message); }
  }
  async function stage() {
    if (!S.open) return;
    try { const r = await post('/api/mail/client/stage', { ref: S.open.ref });
      say(r.already ? 'Already on the board' : (r.auto_committed ? 'Auto-filed to its project' : 'Staged on the triage panel'));
      if (typeof loadTriageItems === 'function') loadTriageItems(); if (window.loadData) loadData(); }
    catch (e) { say(e.message); }
  }

  // ── composer ──
  const C = { open: false, reply_ref: null, atts: [], project_id: '', dirty: false };
  function composerOpen() { return C.open; }
  async function compose(init) {
    init = init || {};
    if (!S.caps) await loadCaps();
    C.open = true; C.reply_ref = init.reply_ref || null; C.atts = (init.attachments || []).slice(); C.project_id = init.project_id || ''; C.dirty = false;
    const c = (S.caps && S.caps.capabilities) || {};
    const canSend = !!(S.caps && S.caps.send_enabled && c.send);
    const box = $('mail-compose');
    box.innerHTML = `
      <div class="mc-head"><span>${esc(init.mode === 'forward' ? 'Forward' : init.reply_ref ? 'Reply' : 'New message')}</span>
        <button class="mail-btn" onclick="PBMail.closeCompose()" title="Close (keeps nothing)">&#10005;</button></div>
      <div class="mc-row"><label>To</label><input id="mc-to" value="${escA(init.to || '')}" placeholder="name@example.com, ...">
        <button class="mc-link" onclick="PBMail.toggleCc()">Cc/Bcc</button></div>
      <div class="mc-row" id="mc-cc-row" style="display:${init.cc ? 'flex' : 'none'}"><label>Cc</label><input id="mc-cc" value="${escA(init.cc || '')}"></div>
      <div class="mc-row" id="mc-bcc-row" style="display:none"><label>Bcc</label><input id="mc-bcc" value=""></div>
      <div class="mc-row"><label>Subject</label><input id="mc-subject" value="${escA(init.subject || '')}"></div>
      <div class="mc-row"><label>${esc((window.ent ? ent('project') : 'Project'))}</label>
        <select id="mc-project"><option value="">(not linked)</option>${projectOptions()}</select></div>
      <div class="mc-body" id="mc-body" contenteditable="true"></div>
      <div class="mc-atts" id="mc-atts"></div>
      <div class="mc-foot">
        ${canSend ? `<button class="mail-btn primary" onclick="PBMail.send()">Send</button>` : ''}
        <button class="mail-btn ${canSend ? '' : 'primary'}" onclick="PBMail.saveDraft()" ${c.draft ? '' : 'title="Saved to the board draft outbox"'}>Save draft</button>
        <label class="mail-btn" style="cursor:pointer">Attach<input type="file" multiple style="display:none" onchange="PBMail.attach(this)"></label>
        <span class="spacer"></span>
        <span class="mc-hint">${canSend ? 'Send goes through ' + esc(S.caps.provider) : 'Sending is off (Settings &rsaquo; Mail &rsaquo; Allow sending)'}</span>
        <button class="mail-btn danger" onclick="PBMail.closeCompose(true)">Discard</button>
      </div>`;
    const body = $('mc-body');
    body.innerHTML = (init.body_html ? '<div><br></div>' + init.body_html : '<div><br></div>');
    if (C.project_id) $('mc-project').value = C.project_id;
    ['mc-to', 'mc-cc', 'mc-bcc', 'mc-subject'].forEach(id => $(id).addEventListener('input', () => C.dirty = true));
    body.addEventListener('input', () => C.dirty = true);
    renderAtts();
    $('mail-compose-overlay').classList.add('open');
    setTimeout(() => { (init.to ? body : $('mc-to')).focus(); }, 30);
  }
  function toggleCc() { ['mc-cc-row', 'mc-bcc-row'].forEach(id => { const r = $(id); r.style.display = r.style.display === 'none' ? 'flex' : 'none'; }); }
  function closeCompose(discard) {
    if (C.dirty && !discard && !confirm('Close without saving? The text is lost.')) return;
    C.open = false; C.dirty = false;
    $('mail-compose-overlay').classList.remove('open');
  }
  function renderAtts() {
    const el = $('mc-atts'); if (!el) return;
    el.innerHTML = C.atts.map((a, i) => `<span class="mail-att">&#128206; ${esc(a.name)} <small>${fmtSize(a.size)}</small><span class="mc-att-x" onclick="PBMail.removeAtt(${i})">&#10005;</span></span>`).join('');
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
      subject: $('mc-subject').value.trim(), body_html: $('mc-body').innerHTML,
      reply_ref: C.reply_ref, attachments: C.atts, project_id: $('mc-project').value || '',
    };
  }
  async function reply(mode) {
    if (!S.open) return;
    try {
      const r = await post('/api/mail/client/compose', { ref: S.open.ref, mode });
      await compose(Object.assign({ mode }, r));
    } catch (e) { say(e.message); }
  }
  async function send() {
    const p = payload();
    if (!p.to && !p.cc && !p.bcc) { say('Add a recipient'); return; }
    if (!p.subject && !confirm('Send with no subject?')) return;
    const btn = document.querySelector('#mail-compose .mail-btn.primary'); if (btn) btn.disabled = true;
    try {
      const r = await post('/api/mail/client/send', p);
      say('Sent' + (r.refused && Object.keys(r.refused).length ? ' (some recipients refused)' : ''));
      C.dirty = false; closeCompose(true);
      if (p.project_id && window.loadData) loadData();
    } catch (e) {
      say(e.message); if (btn) btn.disabled = false;
      if (e.status === 403) { const h = document.querySelector('#mail-compose .mc-hint'); if (h) h.textContent = e.message; }
    }
  }
  async function saveDraft() {
    const p = payload();
    try {
      const r = await post('/api/mail/client/draft', p);
      say(r.where === 'provider' ? 'Saved to Drafts' : 'Saved to the board draft outbox');
      C.dirty = false; closeCompose(true);
    } catch (e) { say(e.message); }
  }

  return { show, hide, selectFolder, reloadFolders, openMessage, more, refresh, flag, markRead, move, del, fileTo, stage,
           compose, closeCompose, toggleCc, attach, removeAtt, reply, send, saveDraft, state: S,
           // Settings flipped a mail flag: re-read capabilities (send gate, provider) without losing the open folder.
           resetCaps: async () => { S.caps = null; if (S.built) { await loadCaps(); renderFolders(); } } };
})();
