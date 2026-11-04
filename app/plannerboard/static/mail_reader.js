/* Email Boss mail client: the reading pane and message actions. */
(() => {
  const M = window.EBMailCore;
  const $ = (...a) => M.$(...a);
  const BAD_CSS = M.BAD_CSS;
  const PLACEHOLDER = M.PLACEHOLDER;
  const S = M.S;
  const api = (...a) => M.api(...a);
  const bumpUnread = (...a) => M.bumpUnread(...a);
  const cleanUrl = (...a) => M.cleanUrl(...a);
  const el = (...a) => M.el(...a);
  const esc = (...a) => M.esc(...a);
  const fmtSize = (...a) => M.fmtSize(...a);
  const folderKind = (...a) => M.folderKind(...a);
  const isRemote = (...a) => M.isRemote(...a);
  const loadList = (...a) => M.loadList(...a);
  const normRemote = (...a) => M.normRemote(...a);
  const post = (...a) => M.post(...a);
  const projectOptions = (...a) => M.projectOptions(...a);
  const proxyImages = (...a) => M.proxyImages(...a);
  const reloadFolders = (...a) => M.reloadFolders(...a);
  const renderRows = (...a) => M.renderRows(...a);
  const reply = (...a) => M.reply(...a);
  const say = (...a) => M.say(...a);
  const senderKey = (...a) => M.senderKey(...a);
  const senderTrusted = (...a) => M.senderTrusted(...a);
  const toastAction = (...a) => M.toastAction(...a);
  const trustSender = (...a) => M.trustSender(...a);

  // -- reading pane
  // 2.0: the reading pane reads from the store (the body is on this computer
  // inside the body window); the server answers only for older mail.
  async function openMessage(ref) {
    const t0 = performance.now();
    S.openRef = ref; S.cursorRef = ref; S._openT0 = t0;
    if (M.scrollToRef) M.scrollToRef(ref); else renderRows();
    const pane = $('mail-read');
    pane.innerHTML = '<div class="mail-read-empty">Loading...</div>';
    const fromStore = S.source === 'store';
    try {
      S.open = fromStore
        ? await api('/api/mail/store/message?ref=' + encodeURIComponent(ref))
        : await api('/api/mail/client/message?ref=' + encodeURIComponent(ref) + '&mark_read=1');
    } catch (e) {
      if (S.openRef !== ref) return;
      S.open = null;                                  // nothing stale left to reply to
      pane.innerHTML = '';
      pane.appendChild(el('div', { class: 'mail-read-empty', text: e.message }));
      return;
    }
    if (S.openRef !== ref) return;                    // a newer j / k won
    const row = S.rows.find(r => r.ref === ref);
    if (fromStore && S.open && !S.open.is_read) {
      // Opened from the list, search, the palette or the timeline: read on the server too.
      post('/api/mail/client/flags', { ref, read: true }).catch(() => {});
      S.open.is_read = true;
      if (window.PBMailSearch && PBMailSearch.update) PBMailSearch.update(ref, { is_read: true });
    }
    if (row && !row.is_read) {
      row.is_read = true; bumpUnread(-1); if (M.updateRowStates) M.updateRowStates();
    }
    S.open._loadImages = senderTrusted(S.open);
    renderOpen();
  }

  // Build the sandboxed document: active content stripped, remote content held back.
  function prepareBody(m, cidMap, loadImages, remoteMap) {
    let html = m.is_html ? (m.body || '') : ('<pre style="white-space:pre-wrap;font-family:inherit">' + esc(m.body || '') + '</pre>');
    const doc = new DOMParser().parseFromString(html, 'text/html');
    doc.querySelectorAll('script,iframe,frame,frameset,object,embed,applet,form,base,meta,link,noscript,audio,video,source,template,input,button,textarea,select,svg,math').forEach(n => n.remove());
    remoteMap = remoteMap || {};
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
          const real = normRemote(src);
          if (loadImages && remoteMap[real]) n.setAttribute('src', remoteMap[real]);
          else { n.setAttribute('data-pb-src', real); n.setAttribute('src', PLACEHOLDER); if (!loadImages) blocked++; }
        } else if (!/^data:image\//i.test(cleanUrl(src))) { n.removeAttribute('src'); }
        if (n.hasAttribute('srcset')) { n.removeAttribute('srcset'); if (!loadImages) blocked++; }
      }
      if (n.hasAttribute('background')) { n.removeAttribute('background'); if (!loadImages) blocked++; }
      const st = n.getAttribute('style');
      if (st && BAD_CSS.test(st)) {
        // Only a plain url() is "an image held back"; escapes, image-set and the rest are simply dropped.
        const plain = st.replace(/url\s*\([^)]*\)/gi, 'none');
        if (/url\s*\(/i.test(st) && !loadImages) blocked++;
        if (BAD_CSS.test(plain)) n.removeAttribute('style'); else n.setAttribute('style', plain);
      }
    });
    doc.querySelectorAll('style').forEach((s) => {
      const t = s.textContent || '';
      if (!BAD_CSS.test(t)) return;
      const plain = t.replace(/@import[^;]*;?/gi, '').replace(/url\s*\([^)]*\)/gi, 'none');
      if (/url\s*\(|@import/i.test(t) && !loadImages) blocked++;
      if (BAD_CSS.test(plain)) s.remove(); else s.textContent = plain;
    });
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
    const remote = m._loadImages && m.is_html ? await proxyImages(m.body || '') : {};
    if (!S.open || S.open.ref !== ref) return;
    const prep = prepareBody(m, map, !!m._loadImages, remote);
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
    if (S._openT0 != null && S.open && S.open.ref === ref) { M.timed('open_message_ms', performance.now() - S._openT0); S._openT0 = null; }
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
          <button class="mail-btn" data-act="stage" title="Put this message in New mail to sort so it can go on the board">Add to board</button>
        </div>
      </div>
      <div class="mail-img-bar" id="mail-img-bar" style="display:none"></div>
      ${files.length ? `<div class="mail-attachments" id="mail-atts"></div>` : ''}
      <div class="mail-read-split">
        <div class="mail-body" id="mail-body"></div>
        <aside class="mail-ctx" id="mail-ctx" aria-label="Context" style="display:none"></aside>
      </div>
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
    if (S.ctxOpen) loadContext();
  }

  // -- context pane ("]"): project, money, people, thread timeline
  function toggleContext(force) {
    S.ctxOpen = typeof force === 'boolean' ? force : !S.ctxOpen;
    const box = $('mail-ctx');
    if (!box) return;
    if (!S.ctxOpen) { box.style.display = 'none'; return; }
    loadContext();
  }
  async function loadContext() {
    const box = $('mail-ctx'); const m = S.open;
    if (!box || !m) return;
    box.style.display = 'block';
    box.textContent = 'Loading...';
    let c;
    try { c = await api('/api/mail/store/context?ref=' + encodeURIComponent(m.ref)); }
    catch (e) { box.textContent = e.message; return; }
    if (!S.open || S.open.ref !== m.ref) return;
    box.textContent = '';
    const sec = (title) => { const h = el('div', { class: 'ctx-sec' }, [el('h4', { text: title })]); box.appendChild(h); return h; };
    const p = c.project;
    const ps = sec((window.ent ? ent('project') : 'Project'));
    ps.appendChild(el('div', { text: p ? `${p.name} (${p.status || 'no status'})` : 'Not filed yet' }));
    if (p && (p.quote_amount != null || p.approved_amount != null)) {
      const ms = sec('Money');
      if (p.quote_amount != null) ms.appendChild(el('div', { text: 'Quoted: $' + Number(p.quote_amount).toLocaleString() }));
      if (p.approved_amount != null) ms.appendChild(el('div', { text: 'Approved: $' + Number(p.approved_amount).toLocaleString() }));
    }
    const pe = sec('People');
    (c.people || []).forEach((x) => pe.appendChild(el('div', { class: 'ctx-person',
      text: `${x.name || x.email}  (you wrote ${x.sent}, they wrote ${x.received})` })));
    if (!(c.people || []).length) pe.appendChild(el('div', { text: 'No history yet' }));
    const tl = sec('Timeline');
    (c.thread || []).forEach((t) => {
      const row = el('div', { class: 'ctx-thread' + (t.ref === m.ref ? ' here' : ''), role: 'button', tabindex: '0',
        text: `${t.date ? new Date(t.date).toLocaleDateString() : ''}  ${t.from || ''}: ${t.subject || ''}` });
      row.addEventListener('click', () => openMessage(t.ref));
      tl.appendChild(row);
    });
    if (!(c.thread || []).length) tl.appendChild(el('div', { text: 'Only this message' }));
  }

  function note(t) { const e = $('mail-note'); if (e) e.textContent = t || ''; }

  // Actions on the open message go through the undo layer (mail_undo.js).
  function flag(on) { if (S.open) return M.listAction('star', [S.open.ref], { on }); }
  function markRead(on) { if (S.open) return M.listAction('read', [S.open.ref], { on }); }
  function dropOpen() {
    S.rows = S.rows.filter(r => r.ref !== S.openRef);
    S.open = null; S.openRef = null;
    renderRows(); renderOpen();
  }
  function backTarget() { const k = folderKind(); return k === 'inbox' ? 'inbox' : S.folder; }
  function moveWithUndo(dest) { if (S.open) return M.listAction(dest === 'archive' ? 'archive' : dest === 'trash' ? 'trash' : 'move', [S.open.ref], { folder: dest }); }
  function move(folder) { return moveWithUndo(folder); }
  function trash() { return moveWithUndo('trash'); }
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
      say(r.already ? 'Already on the board' : (r.auto_committed ? 'Filed to its ' + ((window.ent ? ent('project') : 'project').toLowerCase()) : 'Added to New mail to sort'));
      if (typeof loadTriageItems === 'function') loadTriageItems(); if (window.loadData) loadData(); }
    catch (e) { say(e.message); }
  }

  Object.assign(M, { toggleContext, loadContext, backTarget, bodyDoc, del, drawBody, dropOpen, fileTo, flag, inlineImages, markRead, move, moveWithUndo, note, openMessage, prepareBody, renderOpen, stage, trash });
})();
