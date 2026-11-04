/* Email Boss mail client: shell, folders and the message list. */
(() => {
  const M = window.EBMailCore;
  const $ = (...a) => M.$(...a);
  const S = M.S;
  const api = (...a) => M.api(...a);
  const compose = (...a) => M.compose(...a);
  const composerOpen = (...a) => M.composerOpen(...a);
  const del_ = (...a) => M.del_(...a);
  const el = (...a) => M.el(...a);
  const fmtDate = (...a) => M.fmtDate(...a);
  const looksHtml = (...a) => M.looksHtml(...a);
  const onKey = (...a) => M.onKey(...a);
  const openMessage = (...a) => M.openMessage(...a);
  const openSettingsTab = (...a) => M.openSettingsTab(...a);
  const say = (...a) => M.say(...a);
  const textToHtml = (...a) => M.textToHtml(...a);
  const toggleKeys = (...a) => M.toggleKeys(...a);
  const trash = (...a) => M.trash(...a);

  // -- shell
  function build() {
    const host = $('mail-view');
    if (!host || S.built) return;
    host.innerHTML = `
      <div class="mail-shell">
        <aside class="mail-folders" id="mail-folders"></aside>
        <section class="mail-list">
          <div class="mail-list-bar">
            <input type="text" id="mail-search" placeholder="Search all mail (/ or Ctrl+K)" autocomplete="off" aria-label="Search mail">
            <button class="mail-btn" id="mail-refresh" title="Check the server for new mail">&#8635;</button>
            <button class="mail-btn" id="mail-density" title="Row size: compact, comfortable, large">&#8801;</button>
            <button class="mail-btn" id="mail-keys-btn" title="Keyboard shortcuts (?)">?</button>
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
    // Local store search first (mail_search.js); the folder's server search is
    // its fallback row. Without mail_search.js the box searches the server.
    search.addEventListener('input', () => {
      if (window.PBMailSearch) { S.query = ''; PBMailSearch.onInput(search.value); return; }
      clearTimeout(t); t = setTimeout(() => { S.query = search.value.trim(); loadList(true); }, 350);
    });
    search.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      search.value = ''; search.blur();
      if (window.PBMailSearch && PBMailSearch.active()) { PBMailSearch.leave(); return; }
      S.query = ''; loadList(true);
    });
    $('mail-refresh').addEventListener('click', refresh);
    $('mail-density').addEventListener('click', () => M.cycleDensity());
    $('mail-rows').addEventListener('scroll', (ev) => { S._scrollTop = ev.target.scrollTop; if (M.paintRows) M.paintRows(); });
    window.addEventListener('resize', () => { S._viewH = null; });
    $('mail-rows').addEventListener('click', (ev) => {
      const r = ev.target.closest('.mail-row[data-ref]');
      if (!r) return;
      if (ev.target.closest('.mr-check')) { M.toggleSelect(r.dataset.ref); return; }
      S.cursorRef = r.dataset.ref;
      openMessage(r.dataset.ref);
    });
    M.applyDensity();
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
    if (!S.timer) S.timer = setInterval(() => {
      const searching = window.PBMailSearch && PBMailSearch.active();
      if (host.style.display !== 'none' && S.folder && !composerOpen() && !searching) loadList(false, true);
    }, 90000);
  }
  function hide() { const host = $('mail-view'); if (host) host.style.display = 'none'; }


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
  // 2.0: the list reads from the store on this computer. The server is asked
  // only when the store cannot answer for this folder, for a server search
  // (the fallback row) and when the person presses Refresh.
  // mode: 'reset' (new folder or query), 'more' (next page) or 'fresh' (the
  // periodic refresh: re-read from the top so new mail shows, keeping what is
  // loaded, the cursor and the open message).
  async function loadStore(mode, seq) {
    const f = S.folders.find(x => x.id === S.folder) || {};
    const offset = mode === 'more' ? S.rows.length : 0;
    const limit = mode === 'fresh' ? Math.max(S.rows.length, 200) : 200;
    const q = new URLSearchParams({ folder_id: S.folder, folder_kind: f.kind || 'other', offset: String(offset), limit: String(limit) });
    const r = await api('/api/mail/store/list?' + q.toString());
    if (seq !== S.loadSeq) return 'stale';
    if (!r.covered) return false;
    S.source = 'store';
    S.rows = mode === 'more' ? S.rows.concat(r.messages || []) : (r.messages || []);
    S.total = r.total || S.rows.length;
    S.cursor = S.rows.length < S.total ? 'store' : null;
    return true;
  }
  // Every load carries a sequence number: a folder switch or a new query
  // starts a new one at once (never blocked by a page still loading) and a
  // reply for an older one is dropped instead of painting the old folder.
  async function loadList(reset, quiet) {
    if (!S.folder) return;
    if (!reset && S.loading) return;
    const mode = reset ? 'reset' : (quiet ? 'fresh' : 'more');
    const seq = S.loadSeq = (S.loadSeq || 0) + 1;
    S.loading = true;
    if (reset && !quiet) { S.rows = []; $('mail-rows').innerHTML = '<div class="mail-empty">Loading...</div>'; }
    try {
      const fromStore = !S.query && (reset || S.source === 'store') ? await loadStore(mode, seq) : false;
      if (fromStore === 'stale') return;
      if (fromStore) {
        S.lastList = Date.now();
        if (mode !== 'more') await loadLocalDrafts();
        if (seq !== S.loadSeq) return;
        renderRows();
        return;
      }
      S.source = 'server';
      const q = new URLSearchParams({ folder: S.folder, limit: '50' });
      if (S.query) q.set('q', S.query);
      if (mode === 'more' && S.cursor) q.set('cursor', S.cursor);
      const r = await api('/api/mail/client/messages?' + q.toString());
      if (seq !== S.loadSeq) return;
      S.rows = (mode === 'more' && S.cursor) ? S.rows.concat(r.messages || []) : (r.messages || []);
      S.cursor = r.next_cursor || null;
      S.lastList = Date.now();
      if (mode !== 'more') await loadLocalDrafts();
      if (seq !== S.loadSeq) return;
      renderRows();
    } catch (e) {
      if (seq !== S.loadSeq) return;
      // The board outbox still shows in Drafts even when the mailbox is unreachable.
      if (folderKind() === 'drafts') { await loadLocalDrafts(); S.rows = []; renderRows(e.message); }
      else emptyWith(e.message);
    } finally { if (seq === S.loadSeq) S.loading = false; }
  }

  // -- the list: virtualized, fixed row height per density
  function rowH() { return M.rowHeight(); }
  function renderRows(errText) {
    const host = $('mail-rows');
    if (!host) return;
    host.textContent = '';
    S.paintedFirst = -1; S._byRef = null; S._scrollTop = null;
    if (errText) host.appendChild(el('div', { class: 'mail-empty', text: errText }));
    if (!S.rows.length && !S.local.length && !errText) {
      host.appendChild(el('div', { class: 'mail-empty', text: S.query ? 'No matches.' : 'Nothing here.' }));
      return;
    }
    const spacer = el('div', { class: 'mail-virt', id: 'mail-virt' });
    spacer.style.height = (S.rows.length * rowH()) + 'px';
    host.appendChild(spacer);
    paintRows(true);
    if (S.cursor && S.source !== 'store') host.appendChild(el('div', { class: 'mail-more' }, [el('button', { class: 'mail-btn', text: 'Load more', on: { click: more } })]));
    if (S.source === 'store' && S.cursor) host.appendChild(el('div', { class: 'mail-more', text: `${S.rows.length.toLocaleString()} of ${S.total.toLocaleString()}` }));
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
    if (window.__eb_timing && window.__eb_timing.first_list_ms == null && S.rows.length) {
      window.__eb_timing.first_list_ms = Math.round(performance.now());
    }
  }
  // Only the rows on screen (plus a margin) exist in the DOM.
  function paintRows(force) {
    const host = $('mail-rows'); const spacer = $('mail-virt');
    if (!host || !spacer) return;
    const h = rowH();
    const first = Math.max(0, Math.floor(host.scrollTop / h) - 10);
    const count = Math.ceil((host.clientHeight || 800) / h) + 20;
    if (!force && first === S.paintedFirst) { updateRowStates(); return; }
    S.paintedFirst = first;
    spacer.textContent = '';
    const isSent = folderKind() === 'sent';
    S.rows.slice(first, first + count).forEach((m, i) => {
      const who = isSent ? ('To: ' + (m.to || '')) : (m.from || m.from_email || '');
      const cls = rowClass(m);
      const row = el('div', { class: cls, dataset: { ref: m.ref }, role: 'row' }, [
        el('span', { class: 'mr-check', text: S.sel.has(m.ref) ? '\u2611' : '\u2610', title: 'Select (x)' }),
        el('span', { class: 'mr-from', text: who }),
        el('span', { class: 'mr-subj', text: m.subject || '(no subject)' }),
        el('span', { class: 'mr-prev', text: m.preview ? ' \u2013 ' + m.preview : '' }),
        el('span', { class: 'mr-icons', text: `${m.flagged ? '\u2691 ' : ''}${m.has_attachments ? '\ud83d\udcce' : ''}` }),
        el('span', { class: 'mr-date', text: fmtDate(m.date) }),
      ]);
      row.style.top = ((first + i) * h) + 'px';
      row.style.height = h + 'px';
      spacer.appendChild(row);
    });
    // Near the end of what is loaded: fetch the next store page.
    if (S.source === 'store' && S.cursor && !S.loading && first + count >= S.rows.length - 20) loadList(false);
  }
  // Cursor, selection, read and star changes touch only the rows already on
  // screen (no rebuild, no layout read): this is what keeps j / k cheap on a
  // page as large as the board.
  function rowClass(m) {
    return ['mail-row', m.is_read ? '' : 'unread', m.ref === S.openRef ? 'active' : '',
      m.ref === S.cursorRef ? 'cursor' : '', S.sel.has(m.ref) ? 'selected' : ''].filter(Boolean).join(' ');
  }
  function updateRowStates() {
    const spacer = $('mail-virt'); if (!spacer) return;
    const byRef = S._byRef && S._byRefLen === S.rows.length ? S._byRef : null;
    const map = byRef || new Map(S.rows.map(r => [r.ref, r]));
    if (!byRef) { S._byRef = map; S._byRefLen = S.rows.length; }
    spacer.querySelectorAll('.mail-row').forEach((n) => {
      const m = map.get(n.dataset.ref); if (!m) return;
      const cls = rowClass(m);
      if (n.className !== cls) n.className = cls;
      const ck = n.firstChild; const glyph = S.sel.has(m.ref) ? '\u2611' : '\u2610';
      if (ck && ck.textContent !== glyph) ck.textContent = glyph;
      const ic = n.querySelector('.mr-icons'); const icons = `${m.flagged ? '\u2691 ' : ''}${m.has_attachments ? '\ud83d\udcce' : ''}`;
      if (ic && ic.textContent !== icons) ic.textContent = icons;
    });
  }
  function scrollToRef(ref) {
    const host = $('mail-rows'); const i = S.rows.findIndex(r => r.ref === ref);
    if (!host || i < 0) return;
    const h = rowH(); const top = i * h;
    const view = S._viewH || (S._viewH = host.clientHeight || 600);
    const st = S._scrollTop != null ? S._scrollTop : host.scrollTop;
    let next = st;
    if (top < st) next = top;
    else if (top + h > st + view) next = top + h - view;
    if (next !== st) { host.scrollTop = next; S._scrollTop = next; paintRows(false); }
    else updateRowStates();
  }
  function more() { loadList(false); }
  // Refresh = ask the server now: one capture cycle into the store, then re-read.
  async function refresh() {
    try { await M.post('/api/mail/run', {}); } catch (e) { /* the store still answers */ }
    S.cursor = null; loadList(true); reloadFolders();
  }

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


  function bumpUnread(n) {
    const f = S.folders.find(x => x.id === S.folder);
    if (f && f.unread != null) { f.unread = Math.max(0, f.unread + n); renderFolders(); }
  }

  // The search fallback row: the same words, asked of the server in this folder.
  function searchServer(q) { S.query = (q || '').trim(); S.cursor = null; loadList(true); }

  Object.assign(M, { updateRowStates, paintRows, scrollToRef, loadStore, build, bumpUnread, deleteLocalDraft, emptyWith, folderKind, hide, kindIcon, loadCaps, loadFolders, loadList, loadLocalDrafts, more, openLocalDraft, refresh, reloadFolders, renderFolders, renderRows, searchServer, selectFolder, show });
})();
