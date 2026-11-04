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

  function nextRefAfter(refs) {
    const gone = new Set(refs);
    const idx = S.rows.findIndex(r => gone.has(r.ref));
    const rest = S.rows.filter(r => !gone.has(r.ref));
    if (!rest.length) return null;
    return (rest[Math.min(Math.max(idx, 0), rest.length - 1)] || {}).ref || null;
  }

  // kind: 'archive' | 'trash' | 'move' | 'star' | 'read'
  async function listAction(kind, refs, opts) {
    opts = opts || {};
    refs = (refs || []).filter(Boolean);
    if (!refs.length) return;
    const c = caps();
    const ids = [];
    let label = '';
    let failed = 0;
    if (kind === 'archive' || kind === 'trash' || kind === 'move') {
      const dest = kind === 'archive' ? 'archive' : kind === 'trash' ? 'trash' : opts.folder;
      label = kind === 'archive' ? 'Archived' : kind === 'trash' ? 'Moved to Trash' : 'Moved';
      const next = nextRefAfter(refs);
      for (const ref of refs) {
        try {
          if (kind === 'trash' && !c.move) { await M.post('/api/mail/client/delete', { ref }); continue; }
          const r = await M.post('/api/mail/client/move', { ref, folder: dest, from: S.folder, undoable: true, label });
          if (r.action_id) ids.push(r.action_id);
        } catch (e) { failed++; M.say(e.message); }
      }
      const gone = new Set(refs);
      S.rows = S.rows.filter(r => !gone.has(r.ref));
      refs.forEach(r => S.sel.delete(r));
      if (S.total) S.total = Math.max(0, S.total - refs.length + failed);
      if (S.openRef && gone.has(S.openRef)) { S.open = null; S.openRef = null; if (M.renderOpen) M.renderOpen(); }
      S.cursorRef = next;
    } else if (kind === 'star' || kind === 'read') {
      const field = kind === 'star' ? 'flagged' : 'read';
      const on = opts.on;
      label = kind === 'star' ? (on ? 'Starred' : 'Unstarred') : (on ? 'Marked read' : 'Marked unread');
      for (const ref of refs) {
        try {
          const r = await M.post('/api/mail/client/flags', { ref, [field]: on, undoable: true, label });
          if (r.action_id) ids.push(r.action_id);
          const row = S.rows.find(x => x.ref === ref);
          if (row) { if (kind === 'star') row.flagged = on; else { if (row.is_read !== on && M.bumpUnread) M.bumpUnread(on ? -1 : 1); row.is_read = on; } }
          if (S.open && S.open.ref === ref) { if (kind === 'star') S.open.flagged = on; else S.open.is_read = on; }
        } catch (e) { failed++; M.say(e.message); }
      }
    }
    if (M.renderRows) M.renderRows();
    if (S.cursorRef && M.scrollToRef) M.scrollToRef(S.cursorRef);
    if (kind === 'star' && S.open && M.renderOpen) M.renderOpen();
    const n = refs.length - failed;
    if (!n) return;
    const text = n > 1 ? `${label} (${n})` : label;
    if (ids.length) {
      const entry = { label: text, ids };
      STACK.push(entry);
      if (STACK.length > MAX) STACK.shift();
      entry.toast = M.toastAction(text, 'Undo', () => undo(entry), 10000);
    } else {
      M.say(text);
    }
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
    if (M.loadList) await M.loadList(true, true);
  }

  // Ctrl+Z / Cmd+Z in the mail view, outside text fields (where the browser's
  // own undo belongs to the text).
  window.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || (e.key !== 'z' && e.key !== 'Z')) return;
    if (!M.viewVisible || !M.viewVisible()) return;
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (M.composerOpen && M.composerOpen()) return;
    e.preventDefault();
    undo();
  });

  Object.assign(M, { listAction, undo, undoStack: STACK });
})();
