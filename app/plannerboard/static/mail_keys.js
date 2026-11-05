/* Email Boss mail client: keyboard shortcuts (2.0 Phase 2, Gmail-compatible).
 *
 * Single-letter keys are inert in inputs, text areas, selects, anything
 * contenteditable and while the composer is open (focus guards). Two-key
 * sequences: "g" then i / t / b / p, and "*" then a.
 */
(() => {
  const M = window.EBMailCore;
  const S = M.S;
  const C = M.C;
  const $ = (id) => document.getElementById(id);

  const KEYS = [
    ['j / k', 'Next / previous message'], ['o or Enter', 'Open'], ['u', 'Back to the list'],
    ['e', 'Archive'], ['#', 'Move to Trash'], ['s', 'Star / unstar'], ['x', 'Select'], ['* then a', 'Select all'],
    ['r', 'Reply'], ['a', 'Reply all'], ['f', 'Forward'], ['c', 'New message'],
    ['/', 'Search'], ['Ctrl+K', 'Command palette'], [']', 'Context pane (project, people, timeline)'],
    ['z', 'Snooze the thread'], ['w', 'Waiting on a reply (nudge me)'], ['m', 'Mute / unmute the thread'],
    ['Ctrl+Z', 'Undo the last action, including rule runs'], ['g then i', 'Go to Inbox'], ['g then t', 'Go to Today'],
    ['g then b', 'Go to the board'], ['g then p', 'Go to the project list'],
    ['Esc', 'Close this window, composer or selection'], ['?', 'Show this list'],
  ];
  let pending = null;            // 'g' or '*' waiting for its second key
  let pendingTimer = null;

  function toggleKeys(force) {
    const box = $('mail-keys'); if (!box) return;
    const on = typeof force === 'boolean' ? force : !box.classList.contains('open');
    box.classList.toggle('open', on);
    if (on) {
      box.textContent = '';
      const card = M.el('div', { class: 'mail-keys-card' }, [M.el('h3', { text: 'Keyboard shortcuts' })]);
      KEYS.forEach(([k, d]) => card.appendChild(M.el('div', { class: 'mail-keys-row' }, [M.el('kbd', { text: k }), M.el('span', { text: d })])));
      box.appendChild(card);
    }
  }

  function cursorIndex(rows) {
    const ref = S.cursorRef || S.openRef;
    return rows.findIndex(r => r.ref === ref);
  }
  function moveCursor(dir, open) {
    const rows = M.viewRows();
    if (!rows.length) return;
    const i = cursorIndex(rows);
    const n = i < 0 ? (dir > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, i + dir));
    const row = rows[n];
    if (!row) return;
    S.cursorRef = row.ref;
    if (open) M.openMessage(row.ref);
    else if (M.searchShowing()) PBMailSearch.refreshStates();
    else if (M.scrollToRef) M.scrollToRef(row.ref);
  }
  // j / k: when a message is open, the next one opens (reading flow);
  // otherwise only the cursor moves.
  function step(dir) { moveCursor(dir, !!S.openRef); }

  function targets() {
    if (S.sel.size) return Array.from(S.sel);
    const ref = S.openRef || S.cursorRef;
    return ref ? [ref] : [];
  }
  // The open message only when it is the one the pane shows (a failed or
  // still-loading open must not leave a reply aimed at the previous one).
  function openMsg() { return S.open && S.open.ref === S.openRef ? S.open : null; }
  function backToList() {
    S.open = null; S.openRef = null;
    if (M.renderOpen) M.renderOpen();
    if (M.updateRowStates) M.updateRowStates();
  }
  function go(where) {
    if (where === 'i') {
      const inbox = S.folders.find(f => f.kind === 'inbox');
      if (typeof setView === 'function') setView('mail');
      if (inbox && M.selectFolder) M.selectFolder(inbox.id);
      return;
    }
    const view = { t: 'today', b: 'kanban', p: 'list' }[where];
    if (view && typeof setView === 'function') setView(view);
  }

  function onKey(e) {
    if (!M.viewVisible() || M.overlayOpen()) return;
    const keysOpen = $('mail-keys') && $('mail-keys').classList.contains('open');
    const t0 = e.target;
    const inField = t0 && (/^(INPUT|TEXTAREA|SELECT)$/.test(t0.tagName) || t0.isContentEditable);
    if (e.key === 'Escape') {
      if (inField && !C.open && !keysOpen) return;       // a field's own Escape (search box, settings)
      if (keysOpen) { toggleKeys(false); e.preventDefault(); return; }
      if (C.open) { M.closeCompose(); e.preventDefault(); return; }
      if (S.sel.size) { S.sel.clear(); if (M.updateRowStates) M.updateRowStates(); return; }
      if (S.openRef) backToList();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (C.open) return;
    if (e.key === 'Enter' && t && /^(BUTTON|A|SUMMARY)$/.test(t.tagName)) return;
    const c = (S.caps && S.caps.capabilities) || {};
    const canWrite = c.draft || c.send;
    if (pending) {
      const p = pending; pending = null; clearTimeout(pendingTimer);
      if (p === 'g' && 'itbp'.includes(e.key)) { go(e.key); e.preventDefault(); return; }
      if (p === '*' && e.key === 'a') { M.viewRows().forEach(r => S.sel.add(r.ref)); if (M.updateRowStates) M.updateRowStates(); M.say(`${S.sel.size} selected`); e.preventDefault(); return; }
    }
    switch (e.key) {
      case '?': toggleKeys(); break;
      case 'j': step(1); break;
      case 'k': step(-1); break;
      case 'o': case 'Enter': if (S.cursorRef) M.openMessage(S.cursorRef); else moveCursor(1, true); break;
      case 'u': backToList(); break;
      case 'x': if (S.cursorRef || S.openRef) M.toggleSelect(S.cursorRef || S.openRef); break;
      case 'e': if (c.move) M.listAction('archive', targets()); break;
      case '#': if (c.move || c.delete) M.listAction('trash', targets()); break;
      case 's': if (c.flags) {
        const refs = targets();
        const first = M.viewRows().find(r => r.ref === refs[0]) || openMsg() || {};
        M.listAction('star', refs, { on: !first.flagged });
      } break;
      case 'r': if (openMsg() && canWrite) M.reply('reply'); break;
      case 'a': if (openMsg() && canWrite) M.reply('reply_all'); break;
      case 'f': if (openMsg() && canWrite) M.reply('forward'); break;
      case 'c': if (canWrite) M.compose(); break;
      case ']': if (openMsg() && M.toggleContext) M.toggleContext(); break;
      case 'z': case 'w': case 'm': if (M.threadKey) M.threadKey(e.key, targets()[0]); break;
      case 'g': case '*':
        pending = e.key; clearTimeout(pendingTimer); pendingTimer = setTimeout(() => { pending = null; }, 1500);
        break;
      default: return;
    }
    e.preventDefault();
  }

  Object.assign(M, { KEYS, onKey, step, toggleKeys, moveCursor });
})();
