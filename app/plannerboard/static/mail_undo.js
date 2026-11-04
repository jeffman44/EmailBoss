/* Email Boss mail client: list actions and the undo stack (2.0 Phase 2).
 *
 * Every mutating list action (archive, move, trash, star, read / unread)
 * goes through listAction(): it asks the server to record an inverse
 * (undoable: true), updates the rows on screen at once, and pushes one entry
 * on the stack. Ctrl+Z (or the toast's Undo) pops the newest entry and asks
 * /api/mail/undo/<action_id> to reverse each message in it. Sending is not
 * here: send has its own 10 second window in the composer.
 */
(() => {
  const M = window.EBMailCore;
  const S = M.S;
  const STACK = [];
  const MAX = 50;

  function caps() { return (S.caps && S.caps.capabilities) || {}; }

  function nextRefAfter(refs, rows) {
    const gone = new Set(refs);
    const idx = rows.findIndex(r => gone.has(r.ref));
    const rest = rows.filter(r => !gone.has(r.ref));
    if (!rest.length) return null;
    return (rest[Math.min(Math.max(idx, 0), rest.length - 1)] || {}).ref || null;
  }

  // kind: 'archive' | 'trash' | 'move' | 'star' | 'read'
  async function listAction(kind, refs, opts) {
    opts = opts || {};
    refs = (refs || []).filter(Boolean);
    if (!refs.length) return;
    const c = caps();
    const searching = M.searchShowing && M.searchShowing();
    const shown = M.viewRows ? M.viewRows() : S.rows;
    const isMove = kind === 'archive' || kind === 'trash' || kind === 'move';
    const field = kind === 'star' ? 'flagged' : 'read';
    const label = kind === 'archive' ? 'Archived' : kind === 'trash' ? 'Moved to Trash' : kind === 'move' ? 'Moved'
      : kind === 'star' ? (opts.on ? 'Starred' : 'Unstarred') : (opts.on ? 'Marked read' : 'Marked unread');
    // On the stack before the first request: a Ctrl+Z pressed while this is
    // still running undoes THIS action (what has been done of it so far).
    const entry = { label, ids: [], pending: true };
    STACK.push(entry);
    if (STACK.length > MAX) STACK.shift();
    const done = [];
    const next = isMove ? nextRefAfter(refs, shown) : null;
    for (const ref of refs) {
      try {
        if (isMove) {
          const dest = kind === 'archive' ? 'archive' : kind === 'trash' ? 'trash' : opts.folder;
          if (kind === 'trash' && !c.move) { await M.post('/api/mail/client/delete', { ref }); done.push(ref); continue; }
          const r = await M.post('/api/mail/client/move', { ref, folder: dest, undoable: true, label });
          if (r.action_id) entry.ids.push(r.action_id);
        } else {
          const r = await M.post('/api/mail/client/flags', { ref, [field]: opts.on, undoable: true, label });
          if (r.action_id) entry.ids.push(r.action_id);
        }
        done.push(ref);
      } catch (e) { M.say(e.message); }
    }
    entry.pending = false;
    // Only what the server took leaves (or changes on) the screen.
    if (isMove) {
      const gone = new Set(done);
      S.rows = S.rows.filter(r => !gone.has(r.ref));
      done.forEach(r => S.sel.delete(r));
      if (S.total) S.total = Math.max(0, S.total - done.length);
      if (searching) PBMailSearch.drop(done);
      if (S.openRef && gone.has(S.openRef)) { S.open = null; S.openRef = null; if (M.renderOpen) M.renderOpen(); }
      if (done.length) S.cursorRef = next;
    } else {
      done.forEach((ref) => {
        const row = S.rows.find(x => x.ref === ref);
        if (row) {
          if (kind === 'star') row.flagged = opts.on;
          else { if (row.is_read !== opts.on && M.bumpUnread) M.bumpUnread(opts.on ? -1 : 1); row.is_read = opts.on; }
        }
        if (searching) PBMailSearch.update(ref, { [kind === 'star' ? 'flagged' : 'is_read']: opts.on });
        if (S.open && S.open.ref === ref) { if (kind === 'star') S.open.flagged = opts.on; else S.open.is_read = opts.on; }
      });
    }
    if (searching) PBMailSearch.render();
    else if (M.renderRows) M.renderRows();
    if (S.cursorRef && !searching && M.scrollToRef) M.scrollToRef(S.cursorRef);
    if (kind === 'star' && S.open && M.renderOpen) M.renderOpen();
    const n = done.length;
    if (!entry.ids.length) {
      const i = STACK.indexOf(entry); if (i >= 0) STACK.splice(i, 1);
      if (n) M.say(n > 1 ? `${label} (${n})` : label);
      return;
    }
    entry.label = n > 1 ? `${label} (${n})` : label;
    entry.toast = M.toastAction(entry.label, 'Undo', () => undo(entry), 10000);
  }

  async function undo(entry) {
    if (!entry) entry = STACK.pop(); else { const i = STACK.indexOf(entry); if (i >= 0) STACK.splice(i, 1); }
    if (!entry) { M.say('Nothing to undo'); return; }
    if (entry.toast) entry.toast.dismiss();
    let ok = 0;
    for (const id of entry.ids) {
      try { await M.post('/api/mail/undo/' + encodeURIComponent(id), {}); ok++; }
      catch (e) { M.say(e.message); }
    }
    if (ok) M.say(`Undone: ${entry.label}`);
    if (M.searchShowing && M.searchShowing() && window.PBMailSearch) await PBMailSearch.run(PBMailSearch.state.q);
    else if (M.loadList) await M.loadList(true, true);
  }

  // Ctrl+Z / Cmd+Z in the mail view, outside text fields (where the browser's
  // own undo belongs to the text).
  window.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || (e.key !== 'z' && e.key !== 'Z')) return;
    if (!M.viewVisible || !M.viewVisible() || (M.overlayOpen && M.overlayOpen())) return;
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (M.composerOpen && M.composerOpen()) return;
    e.preventDefault();
    undo();
  });

  Object.assign(M, { listAction, undo, undoStack: STACK });
})();
