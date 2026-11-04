/* Email Boss mail client: shared state, HTTP, sanitizer, small helpers (mail_*.js read these through window.EBMailCore). */
(() => {
  const M = window.EBMailCore = window.EBMailCore || {};

  const S = {
    built: false, caps: null, folders: [], folder: null, rows: [], cursor: null, query: '',
    open: null, openRef: null, loading: false, timer: null, lastList: 0,
    local: [], pending: [],
    // 2.0 terminal: store-backed list, keyboard cursor, selection, density
    source: 'store', total: 0, cursorRef: null, sel: new Set(), paintedFirst: -1, ctxOpen: false,
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
  // Same policy as the server sanitizer: a style that can fetch, escape or script is dropped.
  const BAD_CSS = /url\s*\(|image-set|expression|@import|behaviou?r|javascript|vbscript|-moz-binding|\\|\/\*/i;
  const IMG_PROXY = '/api/mail/client/image?url=';
  const MAX_PROXY_IMAGES = 40;
  const MAX_PROXY_BYTES = 5 * 1048576;
  const normRemote = (u) => { const c = cleanUrl(u); return c.indexOf('//') === 0 ? 'https:' + c : c; };
  async function fetchProxied(u) {
    try {
      const r = await fetch(IMG_PROXY + encodeURIComponent(u), { credentials: 'same-origin', headers: { 'X-Requested-With': 'EmailBoss' } });
      if (!r.ok) return '';
      const blob = await r.blob();
      if (!/^image\//i.test(blob.type || '') || blob.size > MAX_PROXY_BYTES) return '';
      return await new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result)); fr.onerror = () => res(''); fr.readAsDataURL(blob); });
    } catch (e) { return ''; }
  }
  // url -> data: URL for the https images in a message body (best effort, a few at a time).
  async function proxyImages(html) {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    const urls = [];
    doc.querySelectorAll('img[src]').forEach((i) => {
      const u = normRemote(i.getAttribute('src'));
      if (/^https:/i.test(u) && !urls.includes(u) && urls.length < MAX_PROXY_IMAGES) urls.push(u);
    });
    const map = {};
    let next = 0;
    const worker = async () => { while (next < urls.length) { const u = urls[next++]; const d = await fetchProxied(u); if (d) map[u] = d; } };
    await Promise.all([worker(), worker(), worker(), worker()]);
    return map;
  }
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
        else if (name === 'style') keep = !BAD_CSS.test(val);
        else if (['alt', 'title', 'width', 'height', 'colspan', 'rowspan'].includes(name)) keep = true;
        if (!keep) n.removeAttribute(a.name);
      });
      if (n.tagName === 'A') n.setAttribute('rel', 'noopener noreferrer');
      if (n.tagName === 'IMG' && blockRemote !== false && !n.hasAttribute('src') && n.hasAttribute('data-pb-src')) n.setAttribute('src', PLACEHOLDER);
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
    doc.body.querySelectorAll('img[data-pb-src]').forEach((i) => { i.setAttribute('src', i.getAttribute('data-pb-src')); i.removeAttribute('data-pb-src'); i.removeAttribute('data-pb-loaded'); });
    return doc.body.innerHTML;
  }
  const looksHtml = (s) => /<\/?(p|div|br|span|b|i|a|ul|ol|li|table|blockquote)\b/i.test(s || '');
  const textToHtml = (s) => '<div>' + esc(s || '').replace(/\r?\n/g, '<br>') + '</div>';
  const htmlToText = (h) => { const d = new DOMParser().parseFromString('<body>' + (h || '') + '</body>', 'text/html'); return (d.body.textContent || '').replace(/\s+\n/g, '\n').trim(); };


  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return iso;
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return d.toLocaleDateString([], { year: '2-digit', month: 'short', day: 'numeric' });
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


  function fmtSize(n) { n = +n || 0; return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? Math.round(n / 1024) + ' KB' : n + ' B'; }

  // -- composer
  const C = { open: false, reply_ref: null, atts: [], project_id: '', dirty: false, mode: '', draftId: null, last: '',
    timer: null, alt: '', fromDisplay: '', useRt: false, ctx: null };

  function viewVisible() { const h = $('mail-view'); return !!(h && h.style.display !== 'none' && h.offsetParent !== null); }

  // -- density (compact 32 px, comfortable 44 px; large text adds 8 px)
  const DENSITY_KEY = 'eb-mail-density';
  const DENSITIES = { compact: 32, comfortable: 44 };
  function density() { try { return localStorage.getItem(DENSITY_KEY) || 'comfortable'; } catch (e) { return 'comfortable'; } }
  function largeText() { return document.body && document.body.classList.contains('large-text'); }
  function rowHeight() { return (DENSITIES[density()] || 44) + (largeText() ? 8 : 0); }
  function applyDensity() {
    const host = document.getElementById('mail-view');
    if (host) { host.dataset.density = density(); host.style.setProperty('--mail-row-h', rowHeight() + 'px'); }
  }
  function cycleDensity() {
    const next = density() === 'compact' ? 'comfortable' : 'compact';
    try { localStorage.setItem(DENSITY_KEY, next); } catch (e) { /* this session only */ }
    applyDensity();
    if (M.renderRows) M.renderRows();
    say(next === 'compact' ? 'Compact rows' : 'Comfortable rows');
  }
  function toggleSelect(ref) {
    if (!ref) return;
    if (S.sel.has(ref)) S.sel.delete(ref); else S.sel.add(ref);
    if (M.updateRowStates) M.updateRowStates();
  }

  // -- timing hook: the bench reads window.__eb_timing on the Windows runner
  window.__eb_timing = window.__eb_timing || { first_list_ms: null, open_message_ms: [], search_ms: [] };
  function timed(kind, ms) {
    const t = window.__eb_timing;
    if (Array.isArray(t[kind])) { t[kind].push(Math.round(ms * 10) / 10); if (t[kind].length > 200) t[kind].shift(); }
  }

  Object.assign(M, { density, rowHeight, applyDensity, cycleDensity, toggleSelect, timed, $, AUTOSAVE_MS, BAD_CSS, C, DROP_TAGS, IMG_KEY, IMG_PROXY, MAX_PROXY_BYTES, MAX_PROXY_IMAGES, OK_TAGS, PLACEHOLDER, PTR_KEY, S, UNDO_SEND_MS, api, cleanUrl, del_, el, esc, escA, fetchProxied, fmtDate, fmtSize, htmlToText, isRemote, looksHtml, normRemote, openSettingsTab, post, projectOptions, proxyImages, put, restoreRemote, sanitizeClient, sanitizeNode, say, senderKey, senderTrusted, textToHtml, toastAction, trustSender, viewVisible });
})();
