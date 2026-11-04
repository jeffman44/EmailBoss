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
      say(r.already ? 'Already on the board' : (r.auto_committed ? 'Filed to its ' + ((window.ent ? ent('project') : 'project').toLowerCase()) : 'Added to New mail to sort'));
      if (typeof loadTriageItems === 'function') loadTriageItems(); if (window.loadData) loadData(); }
    catch (e) { say(e.message); }
  }

  Object.assign(M, { backTarget, bodyDoc, del, drawBody, dropOpen, fileTo, flag, inlineImages, markRead, move, moveWithUndo, note, openMessage, prepareBody, renderOpen, stage, trash });
})();
