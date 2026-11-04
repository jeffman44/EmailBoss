/* Email Boss command palette (Ctrl+K), 2.0 Phase 2.
 *
 * One box, grouped results: Commands, Projects, People, Messages,
 * Attachments, and always a last row that searches the mail server.
 * Prefixes narrow it: ">" commands, "@" people, "#" projects, "?" ask AI
 * (a later version; the row says so). Arrow keys move, Enter runs, Escape
 * closes. Works from any view; picking mail switches to the MAIL view.
 */
window.PBPalette = (() => {
  const P = { open: false, items: [], idx: 0, seq: 0, timer: null };
  const $ = (id) => document.getElementById(id);
  const M = () => window.EBMailCore || {};

  function el(tag, props, children) {
    const n = document.createElement(tag);
    Object.keys(props || {}).forEach((k) => {
      if (k === 'text') n.textContent = props[k];
      else if (k === 'class') n.className = props[k];
      else if (k === 'on') Object.keys(props[k]).forEach((ev) => n.addEventListener(ev, props[k][ev]));
      else n.setAttribute(k, props[k]);
    });
    (children || []).forEach((c) => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }
  const view = (v) => { if (typeof setView === 'function') setView(v); };
  const mailThen = (fn) => { view('mail'); setTimeout(fn, 150); };

  const COMMANDS = [
    { label: 'New message', hint: 'c', run: () => mailThen(() => window.PBMail && PBMail.compose()) },
    { label: 'Go to Inbox', hint: 'g i', run: () => mailThen(() => { const m = M(); const f = (m.S && m.S.folders || []).find(x => x.kind === 'inbox'); if (f && m.selectFolder) m.selectFolder(f.id); }) },
    { label: 'Go to Today', hint: 'g t', run: () => view('today') },
    { label: 'Go to the board', hint: 'g b', run: () => view('kanban') },
    { label: 'Go to the project list', hint: 'g p', run: () => view('list') },
    { label: 'Check for new mail now', run: () => mailThen(() => window.PBMail && PBMail.refresh()) },
    { label: 'Switch row size (compact / comfortable)', run: () => mailThen(() => M().cycleDensity && M().cycleDensity()) },
    { label: 'Keyboard shortcuts', hint: '?', run: () => mailThen(() => window.PBMail && PBMail.toggleKeys(true)) },
    { label: 'Re-index search on this computer', run: () => fetch('/api/mail/store/reindex', { method: 'POST', credentials: 'same-origin',
      headers: { 'X-Requested-With': 'EmailBoss', 'Content-Type': 'application/json' }, body: '{}' }) },
  ];

  function build() {
    if ($('eb-palette')) return;
    const box = el('div', { class: 'eb-palette', id: 'eb-palette', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette' }, [
      el('div', { class: 'eb-pal-card' }, [
        el('input', { id: 'eb-pal-input', type: 'text', autocomplete: 'off', placeholder: 'Search mail, projects, people, or type > for commands', 'aria-label': 'Command palette' }),
        el('div', { class: 'eb-pal-list', id: 'eb-pal-list', role: 'listbox' }),
      ]),
    ]);
    document.body.appendChild(box);
    box.addEventListener('click', (e) => { if (e.target.id === 'eb-palette') close(); });
    const input = $('eb-pal-input');
    input.addEventListener('input', () => { clearTimeout(P.timer); P.timer = setTimeout(() => refresh(input.value), 120); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return; }
      if (e.key === 'Enter') { e.preventDefault(); runIdx(P.idx); }
    });
  }

  function open() {
    build();
    P.open = true;
    $('eb-palette').classList.add('open');
    const input = $('eb-pal-input');
    input.value = '';
    refresh('');
    input.focus();
  }
  function close() {
    P.open = false;
    const b = $('eb-palette');
    if (b) b.classList.remove('open');
    // The board skips re-rendering while a text box has focus: let go of it.
    const i = $('eb-pal-input');
    if (i && document.activeElement === i) i.blur();
  }

  function projects() {
    try { return (typeof DATA !== 'undefined' && DATA && DATA.projects) ? DATA.projects : []; } catch (e) { return []; }
  }

  async function refresh(raw) {
    const mine = ++P.seq;
    const text = String(raw || '');
    const prefix = /^[>@#?]/.test(text) ? text[0] : '';
    const q = (prefix ? text.slice(1) : text).trim();
    const groups = [];
    const lc = q.toLowerCase();
    if (prefix === '?') {
      groups.push(['Ask AI', [{ label: 'Asking AI over your mail arrives in a later version.', disabled: true }]]);
      return draw(groups);
    }
    if (!prefix || prefix === '>') {
      const cmds = COMMANDS.filter(c => !lc || c.label.toLowerCase().includes(lc));
      if (cmds.length) groups.push(['Commands', cmds]);
    }
    if (!prefix || prefix === '#') {
      const ps = projects().filter(p => !lc || String(p.name || '').toLowerCase().includes(lc) || String(p.id || '').toLowerCase() === lc)
        .slice(0, 6).map(p => ({ label: p.name || p.id, hint: p.status || '', run: () => { close(); if (typeof openPanel === 'function') openPanel(p.id); } }));
      if (ps.length) groups.push([(typeof pluralize === 'function' && window.ent ? pluralize(ent('project')) : 'Projects'), ps]);
    }
    draw(groups);                       // commands and projects show at once
    if (prefix === '>' || prefix === '#' || !q) return;
    const t0 = performance.now();
    let res = null;
    try {
      const r = await fetch('/api/mail/search?' + new URLSearchParams({ q: prefix === '@' ? q : text.trim(), limit: '8' }),
        { credentials: 'same-origin', headers: { 'X-Requested-With': 'EmailBoss' } });
      res = await r.json();
    } catch (e) { res = null; }
    if (mine !== P.seq || !P.open) return;
    if (res && res.ok) {
      const people = (res.people || []).map(p => ({ label: p.name ? `${p.name} <${p.email}>` : p.email, hint: 'from:',
        run: () => { close(); mailThen(() => { const b = $('mail-search'); if (b) { b.value = 'from:' + p.email; } if (window.PBMailSearch) PBMailSearch.run('from:' + p.email); }); } }));
      if (people.length) groups.push(['People', people]);
      if (prefix !== '@') {
        const msgs = (res.messages || []).map(m => ({ label: m.subject || '(no subject)', hint: m.from || '',
          run: () => { close(); mailThen(() => window.PBMail && PBMail.openMessage(m.ref)); } }));
        if (msgs.length) groups.push(['Messages', msgs]);
        const atts = (res.attachments || []).map(a => ({ label: a.name, hint: a.subject || '',
          run: () => { close(); mailThen(() => window.PBMail && PBMail.openMessage(a.ref)); } }));
        if (atts.length) groups.push(['Attachments', atts]);
        (res.hints || []).slice(0, 2).forEach(h => groups.push(['Hint', [{ label: h, disabled: true }]]));
      }
      if (M().timed) M().timed('search_ms', performance.now() - t0);
    }
    groups.push(['', [{ label: `Search the server for "${q}"`, server: true,
      run: () => { close(); mailThen(() => window.PBMail && PBMail.searchServer(q)); } }]]);
    draw(groups);
  }

  function draw(groups) {
    const list = $('eb-pal-list');
    if (!list) return;
    list.textContent = '';
    P.items = [];
    groups.forEach(([title, items]) => {
      if (title) list.appendChild(el('div', { class: 'eb-pal-group', text: title }));
      items.forEach((it) => {
        const i = P.items.length;
        const row = el('div', { class: 'eb-pal-item' + (it.disabled ? ' disabled' : '') + (it.server ? ' server' : ''), role: 'option' }, [
          el('span', { class: 'eb-pal-label', text: it.label }),
          el('span', { class: 'eb-pal-hint', text: it.hint || '' }),
        ]);
        row.addEventListener('mousemove', () => select(i));
        row.addEventListener('click', () => runIdx(i));
        list.appendChild(row);
        P.items.push({ it, row });
      });
    });
    P.idx = Math.max(0, P.items.findIndex(x => !x.it.disabled));
    select(P.idx);
  }
  function select(i) {
    P.items.forEach((x, j) => x.row.classList.toggle('active', j === i));
    P.idx = i;
    const r = P.items[i] && P.items[i].row;
    if (r && r.scrollIntoView) r.scrollIntoView({ block: 'nearest' });
  }
  function move(d) {
    if (!P.items.length) return;
    let i = P.idx;
    for (let n = 0; n < P.items.length; n++) {
      i = (i + d + P.items.length) % P.items.length;
      if (!P.items[i].it.disabled) break;
    }
    select(i);
  }
  function runIdx(i) {
    const x = P.items[i];
    if (!x || x.it.disabled || !x.it.run) return;
    close();
    x.it.run();
  }

  // Ctrl+K / Cmd+K anywhere opens it (capture phase: before the board's keys).
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault(); e.stopPropagation();
      if (P.open) close(); else open();
    }
  }, true);

  return { open, close, state: P };
})();
