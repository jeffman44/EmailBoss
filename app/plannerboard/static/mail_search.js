/* Email Boss mail search (2.0 Phase 1).
 *
 * The MAIL view's search box searches the local mail store first
 * (/api/mail/search): results as you type, an inline hint for anything the
 * query language did not understand, a coverage footer that says what was
 * and was not searched, and always a last row that hands the same words to
 * the mail server (the 1.x folder search, now the fallback).
 *
 * Keys: "/" and Ctrl+K focus the box while the mail view is showing.
 * Escape clears it and returns to the folder list.
 *
 * No inline handlers (CSP script-src 'self'): every listener is attached here.
 */
window.PBMailSearch = (() => {
  const S = { active: false, q: '', seq: 0, res: null, timer: null };
  const $ = (id) => document.getElementById(id);

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

  async function call(path, opts) {
    opts = Object.assign({}, opts || {});
    const method = (opts.method || 'GET').toUpperCase();
    opts.headers = Object.assign({ 'X-Requested-With': 'EmailBoss' }, opts.headers || {});
    if (method !== 'GET') opts.headers['Content-Type'] = 'application/json';
    opts.credentials = 'same-origin';
    const r = await fetch(path, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { j = { ok: false, error: `Search did not answer (${r.status}).` }; }
    if (!r.ok || j.ok === false) throw new Error(j.error || `Search did not answer (${r.status}).`);
    return j;
  }

  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return d.toLocaleDateString([], { year: '2-digit', month: 'short', day: 'numeric' });
  }

  function onInput(value) {
    const q = (value || '').trim();
    clearTimeout(S.timer);
    if (!q) { leave(); return; }
    S.timer = setTimeout(() => run(q), 180);
  }

  async function run(q) {
    S.active = true; S.q = q;
    const mine = ++S.seq;
    const host = $('mail-rows');
    if (host && !S.res) host.innerHTML = '<div class="mail-empty">Searching...</div>';
    try {
      const res = await call('/api/mail/search?' + new URLSearchParams({ q, limit: '100' }).toString());
      if (mine !== S.seq || !S.active) return;       // a newer keystroke won
      S.res = res;
      render();
    } catch (e) {
      if (mine !== S.seq) return;
      S.res = null;
      renderError(e.message);
    }
  }

  function serverRow(q, label) {
    return el('div', { class: 'ms-server', role: 'button', tabindex: '0', text: label || `Search the server for "${q}"`,
      on: { click: () => toServer(q), keydown: (e) => { if (e.key === 'Enter') toServer(q); } } });
  }

  function renderError(text) {
    const host = $('mail-rows');
    if (!host) return;
    host.textContent = '';
    host.appendChild(el('div', { class: 'mail-empty', text }));
    host.appendChild(serverRow(S.q));
  }

  function render() {
    const host = $('mail-rows');
    const r = S.res;
    if (!host || !r) return;
    host.textContent = '';
    (r.hints || []).forEach((h) => host.appendChild(el('div', { class: 'ms-hint', text: h })));
    if ((r.people || []).length) {
      host.appendChild(el('div', { class: 'mail-group', text: 'People' }));
      r.people.forEach((p) => host.appendChild(el('div', { class: 'ms-person', role: 'button', tabindex: '0',
        text: p.name ? `${p.name} <${p.email}>` : p.email,
        on: { click: () => setQuery(`from:${p.email}`) } })));
    }
    if ((r.messages || []).length) {
      host.appendChild(el('div', { class: 'mail-group', text: `Messages (${r.total}${r.truncated ? '+' : ''})` }));
      r.messages.forEach((m) => {
        const row = el('div', { class: `mail-row ms-row ${m.is_read ? '' : 'unread'}`, dataset: { ref: m.ref, account: m.account } }, [
          el('div', { class: 'mr-from', text: m.folder === 'sent' ? ('To: ' + (m.to || '')) : (m.from || m.from_email || '') }),
          el('div', { class: 'mr-date', text: fmtDate(m.date) }),
          el('div', { class: 'mr-subj', text: m.subject || '(no subject)' }),
          el('div', { class: 'mr-icons', text: `${m.starred ? '⚑ ' : ''}${m.has_attachments ? '📎' : ''}` }),
        ]);
        const sub = m.body_searched ? m.snippet : 'Headers only: older than the body window';
        if (sub) row.appendChild(el('div', { class: 'mr-prev', text: sub }));
        row.addEventListener('click', () => open(m));
        host.appendChild(row);
      });
    }
    if ((r.attachments || []).length) {
      host.appendChild(el('div', { class: 'mail-group', text: 'Attachments' }));
      r.attachments.forEach((a) => host.appendChild(el('div', { class: 'ms-att', role: 'button', tabindex: '0',
        text: `${a.name}  (${a.subject || 'no subject'})`, on: { click: () => open({ ref: a.ref, account: a.account }) } })));
    }
    host.appendChild(serverRow(S.q, r.server_fallback && r.server_fallback.label));
    const cov = r.coverage || {};
    const foot = el('div', { class: 'ms-footer' }, [
      el('span', { class: 'ms-cov', text: cov.sentence || '' }),
      el('button', { class: 'mail-btn', text: 'Re-index', title: 'Rebuild the search index on this computer',
        on: { click: reindex } }),
    ]);
    host.appendChild(foot);
  }

  function open(m) {
    if (window.PBMail && PBMail.openMessage) PBMail.openMessage(m.ref);
  }

  function setQuery(q) {
    const box = $('mail-search');
    if (box) { box.value = q; box.focus(); }
    run(q);
  }

  function toServer(q) {
    leave(true);
    if (window.PBMail && PBMail.searchServer) PBMail.searchServer(q);
  }

  async function reindex(ev) {
    const btn = ev && ev.target;
    if (btn) { btn.disabled = true; btn.textContent = 'Re-indexing...'; }
    try { await call('/api/mail/store/reindex', { method: 'POST', body: '{}' }); if (S.q) await run(S.q); }
    catch (e) { if (btn) btn.textContent = e.message; return; }
    if (btn) { btn.disabled = false; btn.textContent = 'Re-index'; }
  }

  function leave(keepBox) {
    const was = S.active;
    S.active = false; S.q = ''; S.res = null; S.seq++;
    clearTimeout(S.timer);
    if (!keepBox) { const box = $('mail-search'); if (box) box.value = ''; }
    if (was && !keepBox && window.PBMail && PBMail.refresh) PBMail.refresh();
  }

  function mailVisible() {
    const h = $('mail-view');
    return !!(h && h.style.display !== 'none' && h.offsetParent !== null);
  }

  // Capture phase on window: runs before the board's own "/" handler, and
  // only takes the key while the mail view is on screen.
  window.addEventListener('keydown', (e) => {
    if (!mailVisible()) return;
    const box = $('mail-search');
    if (!box) return;
    const t = e.target;
    const typing = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
    const ctrlK = (e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K');
    if (ctrlK || (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey)) {
      e.preventDefault();
      e.stopPropagation();
      box.focus();
      box.select();
    }
  }, true);

  return { onInput, run, leave, active: () => S.active, state: S };
})();
