/* Email Boss mail client (the MAIL view).
 *
 * Talks only to /api/mail/client/* (plus /api/drafts and /api/ai/draft-reply).
 * Every request carries the same-origin credentials and the X-Requested-With
 * header; writes are JSON. Everything here is idempotent: renderBoard() calls
 * PBMail.show() on every 30 s cycle and that must not rebuild the pane or
 * lose the open message / composer.
 *
 * Bodies render inside a sandboxed iframe (no scripts, no same-origin). Remote
 * images and CSS url() are replaced by a placeholder until the person presses
 * "Load images". Anything that reaches the composer's innerHTML has been
 * sanitized on the server and is sanitized again here.
 *
 * Sending: Send asks "Send to X and N others?", then waits 10 seconds with an
 * Undo toast before the POST. Archive / Trash get an Undo toast too. The
 * composer autosaves to the board draft outbox every 20 seconds.
 */
window.PBMail = (() => {
  const S = {
    built: false, caps: null, folders: [], folder: null, rows: [], cursor: null, query: '',
    open: null, openRef: null, loading: false, timer: null, lastList: 0,
    local: [], pending: [],
  };
  const UNDO_SEND_MS = 10000;
  const AUTOSAVE_MS = 20000;
  const PTR_KEY = 'pb-mail-autosave';
  const IMG_KEY = 'pb-mail-img-senders';
  const esc = (s) => (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const escA = (s) => esc(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const $ = (id) => document.getElementById(id);
  const say = (m) => { try { toast(m); } catch (e) { console.log(m); } };
  const PLACEHOLDER = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

  function el(tag, props, children) {
    const n = document.createElement(tag);
    Object.keys(props || {}).forEach((k) => {
      if (k === 'text') n.textContent = props[k];
      else if (k === 'class') n.className = props[k];
      else if (k === 'dataset') Object.keys(props[k]).forEach((d) => { n.dataset[d] = props[k][d]; });
      else if (k === 'on') Object.keys(props[k]).forEach((ev) => n.addEventListener(ev, props[k][ev]));
      else n.setAttribute(k, props[k]);
    });
    (children || []).forEach((c) => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }
  function openSettingsTab(tab) {
    window.dispatchEvent(new CustomEvent('pb-open-settings', { detail: { tab } }));
    try { if (typeof openSettings === 'function') openSettings(tab); } catch (e) { /* board.html listens too */ }
  }

  async function api(path, opts) {
    opts = Object.assign({}, opts || {});
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({ 'X-Requested-With': 'EmailBoss' }, opts.headers || {});
    if (method !== 'GET' && method !== 'HEAD') headers['Content-Type'] = 'application/json';
    opts.headers = headers;
    opts.credentials = 'same-origin';
    const r = await fetch(path, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { j = { ok: false, error: `Something went wrong (${r.status}). Try again.` }; }
    if (!r.ok || j.ok === false) {
      const err = new Error(j.error || `Something went wrong (${r.status}). Try again.`);
      err.status = r.status; err.body = j;
      throw err;
    }
    return j;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  const put = (path, body) => api(path, { method: 'PUT', body: JSON.stringify(body || {}) });
  const del_ = (path) => api(path, { method: 'DELETE', body: '{}' });

  // -- toasts with an action (Undo)
  function toastAction(text, label, fn, ms) {
    const host = $('mail-toasts');
    if (!host) { say(text); return { dismiss() {} }; }
    const t = el('div', { class: 'mail-toast' }, [el('span', { text })]);
    let gone = false;
    const dismiss = () => { if (gone) return; gone = true; clearTimeout(timer); t.remove(); };
    if (label) t.appendChild(el('button', { class: 'mail-toast-btn', text: label, on: { click: () => { dismiss(); fn(); } } }));
    t.appendChild(el('button', { class: 'mail-toast-x', text: '\u00d7', title: 'Dismiss', on: { click: dismiss } }));
    host.appendChild(t);
    const timer = setTimeout(dismiss, ms || 8000);
    return { dismiss, node: t };
  }

  // -- client-side HTML safety (second line of defence; the server sanitizes first)
  const OK_TAGS = new Set('P BR DIV SPAN B I U EM STRONG A UL OL LI BLOCKQUOTE PRE CODE TABLE THEAD TBODY TFOOT TR TD TH IMG'.split(' '));
  const DROP_TAGS = new Set('SCRIPT STYLE IFRAME FRAME FRAMESET OBJECT EMBED APPLET FORM BASE META LINK SVG MATH NOSCRIPT TEMPLATE TITLE HEAD TEXTAREA SELECT BUTTON AUDIO VIDEO SOURCE CANVAS'.split(' '));
  const cleanUrl = (u) => String(u || '').replace(/[\u0000-\u0020\u007f-\u009f\u200b-\u200f\u2028\u2029\ufeff]+/g, '');
  const isRemote = (u) => /^(https?:|\/\/|ftp:)/i.test(cleanUrl(u));
  function sanitizeNode(parent, blockRemote) {
    Array.from(parent.childNodes).forEach((n) => {
      if (n.nodeType === 3) return;
      if (n.nodeType !== 1) { n.remove(); return; }
      if (DROP_TAGS.has(n.tagName)) { n.remove(); return; }
      sanitizeNode(n, blockRemote);
      if (!OK_TAGS.has(n.tagName)) { while (n.firstChild) n.parentNode.insertBefore(n.firstChild, n); n.remove(); return; }
      Array.from(n.attributes).forEach((a) => {
        const name = a.name.toLowerCase(); const val = a.value;
        let keep = false;
        if (name === 'href' && n.tagName === 'A') keep = /^(https?:|mailto:|tel:)/i.test(cleanUrl(val));
        else if (name === 'src' && n.tagName === 'IMG') {
          const u = cleanUrl(val);
          if (/^cid:/i.test(u) || /^data:image\/(png|jpe?g|gif|webp);base64,[a-z0-9+\/=]+$/i.test(u)) keep = true;
          else if (isRemote(u)) {
            n.removeAttribute(a.name);
            if (blockRemote) { n.setAttribute('data-pb-src', u.indexOf('//') === 0 ? 'https:' + u : u); n.setAttribute('src', PLACEHOLDER); }
            else n.setAttribute('src', u.indexOf('//') === 0 ? 'https:' + u : u);
            return;
          }
        } else if (name === 'data-pb-src' && n.tagName === 'IMG') keep = /^https?:/i.test(cleanUrl(val)) && blockRemote !== false;
        else if (name === 'style') keep = !/url\s*\(|expression|@import|behaviou?r|javascript|vbscript|\\|\/\*/i.test(val);
        else if (['alt', 'title', 'width', 'height', 'colspan', 'rowspan'].includes(name)) keep = true;
        if (!keep) n.removeAttribute(a.name);
      });
      if (n.tagName === 'A') n.setAttribute('rel', 'noopener noreferrer');
    });
  }
  function sanitizeClient(html, blockRemote) {
    const doc = new DOMParser().parseFromString('<body>' + (html || '') + '</body>', 'text/html');
    sanitizeNode(doc.body, blockRemote !== false);
    return doc.body.innerHTML;
  }
  // Put real image URLs back (used right before sending the quoted original).
  function restoreRemote(html) {
    const doc = new DOMParser().parseFromString('<body>' + (html || '') + '</body>', 'text/html');
    doc.body.querySelectorAll('img[data-pb-src]').forEach((i) => { i.setAttribute('src', i.getAttribute('data-pb-src')); i.removeAttribute('data-pb-src'); });
    return doc.body.innerHTML;
  }
  const looksHtml = (s) => /<\/?(p|div|br|span|b|i|a|ul|ol|li|table|blockquote)\b/i.test(s || '');
  const textToHtml = (s) => '<div>' + esc(s || '').replace(/\r?\n/g, '<br>') + '</div>';
  const htmlToText = (h) => { const d = new DOMParser().parseFromString('<body>' + (h || '') + '</body>', 'text/html'); return (d.body.textContent || '').replace(/\s+\n/g, '\n').trim(); };

  // -- shell
  function build() {
    const host = $('mail-view');
    if (!host || S.built) return;
    host.innerHTML = `
      <div class="mail-shell">
        <aside class="mail-folders" id="mail-folders"></aside>
        <section class="mail-list">
          <div class="mail-list-bar">
            <input type="text" id="mail-search" placeholder="Search this folder..." autocomplete="off">
            <button class="mail-btn" id="mail-refresh" title="Reload">&#8635;</button>
            <button class="mail-btn" id="mail-keys-btn" title="Keyboard shortcuts">?</button>
          </div>
          <div class="mail-rows" id="mail-rows"><div class="mail-empty">Loading...</div></div>
        </section>
        <section class="mail-read" id="mail-read"><div class="mail-read-empty">Select a message</div></section>
      </div>
      <div class="mail-compose-overlay" id="mail-compose-overlay">
        <div class="mail-compose" id="mail-compose"></div>
      </div>
      <div class="mail-keys-overlay" id="mail-keys"></div>
      <div class="mail-toasts" id="mail-toasts"></div>`;
    const search = $('mail-search');
    let t = null;
    search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { S.query = search.value.trim(); loadList(true); }, 350); });
    search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { search.value = ''; S.query = ''; loadList(true); search.blur(); } });
    $('mail-refresh').addEventListener('click', refresh);
    $('mail-keys-btn').addEventListener('click', toggleKeys);
    $('mail-keys').addEventListener('click', (e) => { if (e.target.id === 'mail-keys') toggleKeys(false); });
    if (!window.__pbMailKeys) { window.__pbMailKeys = true; document.addEventListener('keydown', onKey); }
    window.addEventListener('beforeunload', (e) => { if (S.pending.length) { e.preventDefault(); e.returnValue = ''; } });
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
  function viewVisible() { const h = $('mail-view'); return !!(h && h.style.display !== 'none' && h.offsetParent !== null); }

  function emptyWith(text, btnLabel, tab) {
    const box = el('div', { class: 'mail-empty' }, [text]);
    if (btnLabel) {
      box.appendChild(el('br')); box.appendChild(el('br'));
      box.appendChild(el('button', { class: 'mail-btn', text: btnLabel, on: { click: () => openSettingsTab(tab) } }));
    }
    const rows = $('mail-rows'); rows.textContent = ''; rows.appendChild(box);
  }

  async function loadCaps() {
    try { S.caps = await api('/api/mail/client/capabilities'); }
    catch (e) { S.caps = { client_enabled: false, provider: 'none', capabilities: {}, error: e.message }; }
    if (!S.caps.client_enabled || S.caps.provider === 'none' || S.caps.error) {
      const why = S.caps.error ? S.caps.error
        : S.caps.provider === 'none' ? 'No email account is connected yet.'
        : 'The mail client is turned off.';
      emptyWith(why, 'Open Settings > Mail', 'mail');
      $('mail-folders').innerHTML = '';
    } else if (S.caps.capabilities && !S.caps.capabilities.folders) {
      emptyWith(`Your ${S.caps.provider} account collects mail for the board but cannot be browsed here. Connect Gmail, Microsoft 365 or IMAP to read mail in Email Boss.`);
    }
  }

  // -- folders
  async function loadFolders() {
    try {
      const r = await api('/api/mail/client/folders');
      S.folders = r.folders || [];
    } catch (e) {
      $('mail-folders').innerHTML = '';
      emptyWith(e.message, e.status === 401 ? 'Sign in: Settings > Mail' : null, 'mail');
      return;
    }
    renderFolders();
    if (!S.folder) {
      const inbox = S.folders.find(f => f.kind === 'inbox') || S.folders[0];
      if (inbox) selectFolder(inbox.id);
    }
  }
  const kindIcon = { inbox: '\u2709', drafts: '\u270e', sent: '\u27a4', archive: '\u25a3', junk: '\u26a0', trash: '\ud83d\uddd1', other: '\u25b8' };
  function renderFolders() {
    const host = $('mail-folders');
    if (!host) return;
    host.textContent = '';
    const head = el('div', { class: 'mf-head' }, [el('span', { text: 'FOLDERS' }),
      el('span', { text: '\u21bb', title: 'Reload folders', style: 'cursor:pointer', on: { click: reloadFolders } })]);
    host.appendChild(head);
    const canCompose = S.caps && S.caps.capabilities && (S.caps.capabilities.draft || S.caps.capabilities.send);
    if (canCompose) host.appendChild(el('button', { class: 'mail-compose-btn', text: '+ New message', on: { click: () => compose() } }));
    S.folders.forEach((f) => {
      if (f.noselect) return;
      const unread = f.unread || 0;
      const row = el('div', {
        class: `mail-folder k-${String(f.kind || 'other').replace(/[^a-z]/g, '')} ${f.id === S.folder ? 'active' : ''} ${unread ? 'has-unread' : ''}`,
        title: f.path || f.name || '', dataset: { folder: f.id },
      }, [
        el('span', { class: 'mf-name', text: `${kindIcon[f.kind] || kindIcon.other} ${f.name}` }),
        el('span', { class: 'mf-count', text: unread ? String(unread) : '' }),
      ]);
      row.addEventListener('click', () => selectFolder(row.dataset.folder));
      host.appendChild(row);
    });
  }
  async function reloadFolders() { S.folders = []; await loadFolders(); }
  function folderKind() { return (S.folders.find(f => f.id === S.folder) || {}).kind; }
  function selectFolder(id) {
    S.folder = id; S.cursor = null; S.rows = []; S.local = []; S.query = '';
    const sb = $('mail-search'); if (sb) sb.value = '';
    renderFolders();
    loadList(true);
  }

  // -- list
  async function loadLocalDrafts() {
    if (folderKind() !== 'drafts') { S.local = []; return; }
    try { S.local = (await api('/api/drafts?status=draft')).drafts || []; } catch (e) { S.local = []; }
  }
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
      if (reset || quiet) await loadLocalDrafts();
      renderRows();
    } catch (e) {
      // The board outbox still shows in Drafts even when the mailbox is unreachable.
      if (folderKind() === 'drafts') { await loadLocalDrafts(); S.rows = []; renderRows(e.message); }
      else emptyWith(e.message);
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
  function renderRows(errText) {
    const host = $('mail-rows');
    if (!host) return;
    host.textContent = '';
    const isSent = folderKind() === 'sent';
    if (errText) host.appendChild(el('div', { class: 'mail-empty', text: errText }));
    if (!S.rows.length && !S.local.length && !errText) {
      host.appendChild(el('div', { class: 'mail-empty', text: S.query ? 'No matches.' : 'Nothing here.' }));
      return;
    }
    S.rows.forEach((m) => {
      const who = isSent ? ('To: ' + (m.to || '')) : (m.from || m.from_email || '');
      const row = el('div', { class: `mail-row ${m.is_read ? '' : 'unread'} ${m.ref === S.openRef ? 'active' : ''}`, dataset: { ref: m.ref } }, [
        el('div', { class: 'mr-from', text: who }),
        el('div', { class: 'mr-date', text: fmtDate(m.date) }),
        el('div', { class: 'mr-subj', text: m.subject || '(no subject)' }),
        el('div', { class: 'mr-icons', text: `${m.flagged ? '\u2691 ' : ''}${m.has_attachments ? '\ud83d\udcce' : ''}` }),
      ]);
      if (m.preview) row.appendChild(el('div', { class: 'mr-prev', text: m.preview }));
      row.addEventListener('click', () => openMessage(row.dataset.ref));
      host.appendChild(row);
    });
    if (S.cursor) host.appendChild(el('div', { class: 'mail-more' }, [el('button', { class: 'mail-btn', text: 'Load more', on: { click: more } })]));
    if (S.local.length) {
      host.appendChild(el('div', { class: 'mail-group', text: 'On this computer' }));
      S.local.forEach((d) => {
        const row = el('div', { class: 'mail-row local', dataset: { id: d.id } }, [
          el('div', { class: 'mr-from', text: 'To: ' + (d.to || '(no recipient)') }),
          el('div', { class: 'mr-date', text: fmtDate(d.updated_at || d.created_at) }),
          el('div', { class: 'mr-subj', text: d.subject || '(no subject)' }),
          el('div', { class: 'mr-actions' }, [
            el('button', { class: 'mail-btn primary', text: 'Open', on: { click: (ev) => { ev.stopPropagation(); openLocalDraft(d.id); } } }),
            el('button', { class: 'mail-btn danger', text: 'Delete', on: { click: (ev) => { ev.stopPropagation(); deleteLocalDraft(d.id); } } }),
          ]),
        ]);
        row.addEventListener('click', () => openLocalDraft(d.id));
        host.appendChild(row);
      });
    }
  }
  function more() { loadList(false); }
  function refresh() { S.cursor = null; loadList(true); reloadFolders(); }

  async function openLocalDraft(id) {
    const d = S.local.find(x => x.id === id);
    if (!d) return;
    const b = d.body || '';
    await compose({ to: d.to || '', cc: d.cc || '', subject: d.subject || '', project_id: d.project_id || '',
      body_html: looksHtml(b) ? b : textToHtml(b), draftId: d.id, fromLocal: true });
  }
  async function deleteLocalDraft(id) {
    if (!confirm('Delete this draft from this computer?')) return;
    try { await del_('/api/drafts/' + encodeURIComponent(id)); S.local = S.local.filter(x => x.id !== id); renderRows(); say('Draft deleted'); }
    catch (e) { say(e.message); }
  }

  // -- reading pane
  async function openMessage(ref) {
    S.openRef = ref;
    renderRows();
    const pane = $('mail-read');
    pane.innerHTML = '<div class="mail-read-empty">Loading...</div>';
    try {
      S.open = await api('/api/mail/client/message?ref=' + encodeURIComponent(ref) + '&mark_read=1');
    } catch (e) {
      pane.innerHTML = '';
      pane.appendChild(el('div', { class: 'mail-read-empty', text: e.message }));
      return;
    }
    const row = S.rows.find(r => r.ref === ref);
    if (row && !row.is_read) { row.is_read = true; bumpUnread(-1); renderRows(); }
    S.open._loadImages = senderTrusted(S.open);
    renderOpen();
    const active = document.querySelector('.mail-row.active');
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
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
  function senderKey(m) { return String(m.from_email || '').trim().toLowerCase(); }
  function senderTrusted(m) {
    try { return !!senderKey(m) && (JSON.parse(localStorage.getItem(IMG_KEY) || '[]')).includes(senderKey(m)); } catch (e) { return false; }
  }
  function trustSender(m) {
    try {
      const l = JSON.parse(localStorage.getItem(IMG_KEY) || '[]');
      if (senderKey(m) && !l.includes(senderKey(m))) { l.push(senderKey(m)); localStorage.setItem(IMG_KEY, JSON.stringify(l.slice(-500))); }
    } catch (e) { /* storage off: just this message */ }
  }

  // Build the sandboxed document: active content stripped, remote content held back.
  function prepareBody(m, cidMap, loadImages) {
    let html = m.is_html ? (m.body || '') : ('<pre style="white-space:pre-wrap;font-family:inherit">' + esc(m.body || '') + '</pre>');
    const doc = new DOMParser().parseFromString(html, 'text/html');
    doc.querySelectorAll('script,iframe,frame,frameset,object,embed,applet,form,base,meta,link,noscript,audio,video,source,template,input,button,textarea,select').forEach(n => n.remove());
    let blocked = 0;
    doc.querySelectorAll('*').forEach((n) => {
      Array.from(n.attributes).forEach((a) => {
        const nm = a.name.toLowerCase();
        if (nm.startsWith('on')) n.removeAttribute(a.name);
        else if ((nm === 'href' || nm === 'src' || nm === 'action' || nm === 'xlink:href') && /^(javascript|vbscript):/i.test(cleanUrl(a.value))) n.removeAttribute(a.name);
      });
      if (n.tagName === 'A') { n.setAttribute('target', '_blank'); n.setAttribute('rel', 'noopener noreferrer'); }
      if (n.tagName === 'IMG') {
        const src = n.getAttribute('src') || '';
        const cid = /^cid:/i.test(cleanUrl(src)) ? cleanUrl(src).slice(4) : null;
        if (cid && cidMap[cid]) { n.setAttribute('src', cidMap[cid]); }
        else if (isRemote(src)) {
          if (!loadImages) { n.setAttribute('data-pb-src', src); n.setAttribute('src', PLACEHOLDER); blocked++; }
        } else if (!/^data:image\//i.test(cleanUrl(src))) { n.removeAttribute('src'); }
        if (!loadImages && n.hasAttribute('srcset')) { n.removeAttribute('srcset'); blocked++; }
      }
      if (!loadImages) {
        if (n.hasAttribute('background')) { n.removeAttribute('background'); blocked++; }
        const st = n.getAttribute('style');
        if (st && /url\s*\(/i.test(st)) { n.setAttribute('style', st.replace(/url\s*\([^)]*\)/gi, 'none')); blocked++; }
      }
    });
    if (!loadImages) {
      doc.querySelectorAll('style').forEach((s) => {
        const t = s.textContent || '';
        if (/url\s*\(|@import/i.test(t)) { s.textContent = t.replace(/@import[^;]*;?/gi, '').replace(/url\s*\([^)]*\)/gi, 'none'); blocked++; }
      });
    }
    return { html: doc.body ? doc.body.innerHTML : '', head: Array.from(doc.querySelectorAll('head style')).map(s => s.outerHTML).join(''), blocked };
  }
  function bodyDoc(prep) {
    return `<!doctype html><html><head><meta charset="utf-8"><base target="_blank">
      <style>body{font-family:-apple-system,'IBM Plex Sans',Segoe UI,sans-serif;font-size:14px;line-height:1.45;color:#111;margin:16px;word-break:break-word}
      img{max-width:100%;height:auto} blockquote{border-left:2px solid #bbb;margin:8px 0;padding-left:10px;color:#555} pre{white-space:pre-wrap}</style>${prep.head}</head>
      <body>${prep.html}</body></html>`;
  }
  async function inlineImages(m) {
    const map = {};
    const jobs = (m.attachments || []).filter(a => a.content_id && a.url && /^image\/(png|jpe?g|gif|webp)$/i.test(a.content_type || '') && (+a.size || 0) < 3 * 1048576)
      .map(async (a) => {
        try {
          const r = await fetch(a.url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'EmailBoss' } });
          if (!r.ok) return;
          const blob = await r.blob();
          map[a.content_id] = await new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result)); fr.onerror = () => res(''); fr.readAsDataURL(blob); });
        } catch (e) { /* the placeholder stays */ }
      });
    await Promise.all(jobs);
    return map;
  }
  async function drawBody() {
    const m = S.open; if (!m) return;
    const ref = m.ref;
    const map = await inlineImages(m);
    if (!S.open || S.open.ref !== ref) return;
    const prep = prepareBody(m, map, !!m._loadImages);
    const bar = $('mail-img-bar');
    if (bar) {
      bar.textContent = '';
      if (prep.blocked && !m._loadImages) {
        bar.style.display = 'flex';
        bar.appendChild(el('span', { text: 'Images are hidden to protect your privacy.' }));
        bar.appendChild(el('button', { class: 'mail-btn', text: 'Load images', on: { click: () => { m._loadImages = true; drawBody(); } } }));
        if (senderKey(m)) bar.appendChild(el('button', { class: 'mail-btn', text: 'Always load from this sender', on: { click: () => { trustSender(m); m._loadImages = true; drawBody(); } } }));
      } else bar.style.display = 'none';
    }
    const box = $('mail-body'); if (!box) return;
    box.textContent = '';
    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', '');
    iframe.setAttribute('referrerpolicy', 'no-referrer');
    iframe.srcdoc = bodyDoc(prep);
    box.appendChild(iframe);
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
        <div class="mail-actions" id="mail-actions">
          <button class="mail-btn primary" data-act="reply" ${c.draft || c.send ? '' : 'disabled'}>Reply</button>
          <button class="mail-btn" data-act="reply_all" ${c.draft || c.send ? '' : 'disabled'}>Reply all</button>
          <button class="mail-btn" data-act="forward" ${c.draft || c.send ? '' : 'disabled'}>Forward</button>
          ${c.flags ? `<button class="mail-btn" data-act="flag">${m.flagged ? 'Unflag' : 'Flag'}</button>
          <button class="mail-btn" data-act="unread">Unread</button>` : ''}
          ${c.move ? `<button class="mail-btn" data-act="archive">Archive</button>` : ''}
          ${c.delete || c.move ? `<button class="mail-btn danger" data-act="trash">Delete</button>` : ''}
          <span class="sep" style="margin-left:auto"></span>
          <select class="mail-file-select" id="mail-file-proj"><option value="">File to ${esc((window.ent ? ent('project') : 'project')).toLowerCase()}...</option>${projectOptions()}</select>
          <button class="mail-btn" data-act="file">File</button>
          <button class="mail-btn" data-act="stage" title="Put this message on the triage panel">Triage</button>
        </div>
      </div>
      <div class="mail-img-bar" id="mail-img-bar" style="display:none"></div>
      ${files.length ? `<div class="mail-attachments" id="mail-atts"></div>` : ''}
      <div class="mail-body" id="mail-body"></div>
      <div class="mail-note" id="mail-note"></div>`;
    const acts = {
      reply: () => reply('reply'), reply_all: () => reply('reply_all'), forward: () => reply('forward'),
      flag: () => flag(!m.flagged), unread: () => markRead(false), archive: () => move('archive'),
      trash: () => trash(), file: fileTo, stage,
    };
    $('mail-actions').addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-act]'); if (b && acts[b.dataset.act]) acts[b.dataset.act]();
    });
    const at = $('mail-atts');
    if (at) files.forEach((a) => {
      const link = el('a', { class: 'mail-att', href: a.url, target: '_blank', rel: 'noopener' }, ['\ud83d\udcce ' + (a.name || 'attachment') + ' ', el('small', { text: fmtSize(a.size) })]);
      at.appendChild(link);
    });
    drawBody();
  }
  function fmtSize(n) { n = +n || 0; return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? Math.round(n / 1024) + ' KB' : n + ' B'; }
  function note(t) { const e = $('mail-note'); if (e) e.textContent = t || ''; }

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
  function backTarget() { const k = folderKind(); return k === 'inbox' ? 'inbox' : S.folder; }
  // Move with an Undo toast: the move is real, Undo moves it back from its new ref.
  async function moveWithUndo(dest, doneText) {
    if (!S.open) return;
    const ref = S.open.ref; const back = backTarget();
    try {
      const r = await post('/api/mail/client/move', { ref, folder: dest });
      dropOpen();
      const newRef = r.new_ref || null;
      if (!newRef) { say(doneText); return; }
      toastAction(doneText, 'Undo', async () => {
        try { await post('/api/mail/client/move', { ref: newRef, folder: back }); say('Put back'); loadList(true, true); reloadFolders(); }
        catch (e) { say(e.message); }
      }, 10000);
    } catch (e) { say(e.message); }
  }
  function move(folder) { return moveWithUndo(folder, folder === 'archive' ? 'Archived' : 'Moved'); }
  async function trash() {
    if (!S.open) return;
    const c = (S.caps && S.caps.capabilities) || {};
    if (c.move) return moveWithUndo('trash', 'Moved to Trash');
    try { await post('/api/mail/client/delete', { ref: S.open.ref }); say('Moved to Trash'); dropOpen(); }
    catch (e) { say(e.message); }
  }
  const del = trash;
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

  // -- keyboard
  const KEYS = [['j / k', 'Next / previous message'], ['r', 'Reply'], ['e', 'Archive'], ['c', 'New message'],
    ['Esc', 'Close this window, composer or selection'], ['?', 'Show this list']];
  function toggleKeys(force) {
    const box = $('mail-keys'); if (!box) return;
    const on = typeof force === 'boolean' ? force : !box.classList.contains('open');
    box.classList.toggle('open', on);
    if (on) {
      box.textContent = '';
      const card = el('div', { class: 'mail-keys-card' }, [el('h3', { text: 'Keyboard shortcuts' })]);
      KEYS.forEach(([k, d]) => card.appendChild(el('div', { class: 'mail-keys-row' }, [el('kbd', { text: k }), el('span', { text: d })])));
      box.appendChild(card);
    }
  }
  function step(dir) {
    if (!S.rows.length) return;
    const i = S.rows.findIndex(r => r.ref === S.openRef);
    const n = i < 0 ? (dir > 0 ? 0 : S.rows.length - 1) : Math.min(S.rows.length - 1, Math.max(0, i + dir));
    if (S.rows[n]) openMessage(S.rows[n].ref);
  }
  function onKey(e) {
    if (!viewVisible()) return;
    const keysOpen = $('mail-keys') && $('mail-keys').classList.contains('open');
    if (e.key === 'Escape') {
      if (keysOpen) { toggleKeys(false); e.preventDefault(); return; }
      if (C.open) { closeCompose(); e.preventDefault(); return; }
      if (S.openRef) { S.openRef = null; S.open = null; renderRows(); renderOpen(); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (C.open) return;
    const c = (S.caps && S.caps.capabilities) || {};
    switch (e.key) {
      case '?': toggleKeys(); break;
      case 'j': step(1); break;
      case 'k': step(-1); break;
      case 'e': if (S.open && c.move) move('archive'); break;
      case 'r': if (S.open && (c.draft || c.send)) reply('reply'); break;
      case 'c': if (c.draft || c.send) compose(); break;
      default: return;
    }
    e.preventDefault();
  }

  // -- composer
  const C = { open: false, reply_ref: null, atts: [], project_id: '', dirty: false, mode: '', draftId: null, last: '',
    timer: null, alt: '', fromDisplay: '', useRt: false, ctx: null };
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
    renderImgNotice(!!init.remote_blocked || !!body.querySelector('img[data-pb-src]'));
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
      $('mc-body').querySelectorAll('img[data-pb-src]').forEach((i) => { const u = i.getAttribute('data-pb-src'); if (/^https?:/i.test(u)) { i.setAttribute('src', u); i.removeAttribute('data-pb-src'); } });
      n.style.display = 'none';
    } } }));
  }

  // autosave to the board outbox
  function startAutosave() { stopAutosave(); C.timer = setInterval(autosaveNow, AUTOSAVE_MS); }
  function stopAutosave() { if (C.timer) { clearInterval(C.timer); C.timer = null; } }
  function composerSnapshot() {
    return { to: $('mc-to').value.trim(), cc: $('mc-cc').value.trim(), bcc: $('mc-bcc').value.trim(),
      subject: $('mc-subject').value.trim(), body: $('mc-body').innerHTML, project_id: $('mc-project').value || '' };
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

  return { show, hide, selectFolder, reloadFolders, openMessage, more, refresh, flag, markRead, move, del, trash, fileTo, stage,
           compose, closeCompose, toggleCc, attach, removeAtt, reply, send, saveDraft, aiDraft, toggleKeys, state: S,
           _sanitizeClient: sanitizeClient,
           // Settings flipped a mail flag: re-read capabilities (send gate, provider) without losing the open folder.
           resetCaps: async () => { S.caps = null; if (S.built) { await loadCaps(); renderFolders(); } } };
})();
