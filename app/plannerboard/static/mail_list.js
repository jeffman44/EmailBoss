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


  function bumpUnread(n) {
    const f = S.folders.find(x => x.id === S.folder);
    if (f && f.unread != null) { f.unread = Math.max(0, f.unread + n); renderFolders(); }
  }

  // The search fallback row: the same words, asked of the server in this folder.
  function searchServer(q) { S.query = (q || '').trim(); S.cursor = null; loadList(true); }

  Object.assign(M, { build, bumpUnread, deleteLocalDraft, emptyWith, folderKind, hide, kindIcon, loadCaps, loadFolders, loadList, loadLocalDrafts, more, openLocalDraft, refresh, reloadFolders, renderFolders, renderRows, searchServer, selectFolder, show });
})();
