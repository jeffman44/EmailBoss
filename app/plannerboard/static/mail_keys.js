/* Email Boss mail client: keyboard shortcuts. */
(() => {
  const M = window.EBMailCore;
  const $ = (...a) => M.$(...a);
  const C = M.C;
  const S = M.S;
  const closeCompose = (...a) => M.closeCompose(...a);
  const compose = (...a) => M.compose(...a);
  const el = (...a) => M.el(...a);
  const move = (...a) => M.move(...a);
  const openMessage = (...a) => M.openMessage(...a);
  const renderOpen = (...a) => M.renderOpen(...a);
  const renderRows = (...a) => M.renderRows(...a);
  const reply = (...a) => M.reply(...a);
  const viewVisible = (...a) => M.viewVisible(...a);

  // -- keyboard
  const KEYS = [['j / k', 'Next / previous message'], ['r', 'Reply'], ['e', 'Archive'], ['c', 'New message'],
    ['Esc', 'Close this window, composer or selection'], ['?', 'Show this list']];
  function toggleKeys(force) {
    const box = $('mail-keys'); if (!box) return;
    const on = typeof force === 'boolean' ? force : !box.classList.contains('open');
    box.classList.toggle('open', on);
    if (on) {
      box.textContent = '';
      const card = el('div', { class: 'mail-keys-card' }, [el('h3', { text: 'Keyboard shortcuts' })]);
      KEYS.forEach(([k, d]) => card.appendChild(el('div', { class: 'mail-keys-row' }, [el('kbd', { text: k }), el('span', { text: d })])));
      box.appendChild(card);
    }
  }
  function step(dir) {
    if (!S.rows.length) return;
    const i = S.rows.findIndex(r => r.ref === S.openRef);
    const n = i < 0 ? (dir > 0 ? 0 : S.rows.length - 1) : Math.min(S.rows.length - 1, Math.max(0, i + dir));
    if (S.rows[n]) openMessage(S.rows[n].ref);
  }
  function onKey(e) {
    if (!viewVisible()) return;
    const keysOpen = $('mail-keys') && $('mail-keys').classList.contains('open');
    if (e.key === 'Escape') {
      if (keysOpen) { toggleKeys(false); e.preventDefault(); return; }
      if (C.open) { closeCompose(); e.preventDefault(); return; }
      if (S.openRef) { S.openRef = null; S.open = null; renderRows(); renderOpen(); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (C.open) return;
    const c = (S.caps && S.caps.capabilities) || {};
    switch (e.key) {
      case '?': toggleKeys(); break;
      case 'j': step(1); break;
      case 'k': step(-1); break;
      case 'e': if (S.open && c.move) move('archive'); break;
      case 'r': if (S.open && (c.draft || c.send)) reply('reply'); break;
      case 'c': if (c.draft || c.send) compose(); break;
      default: return;
    }
    e.preventDefault();
  }

  Object.assign(M, { KEYS, onKey, step, toggleKeys });
})();
