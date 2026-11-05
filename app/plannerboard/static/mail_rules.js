/* Email Boss mail client: thread actions and rules (2.0 Phase 3).
 *
 * Keys: z snooze, w waiting on a reply, m mute. The palette (Ctrl+K) adds
 * pin, split, merge, unsubscribe and the Rules editor. Every thread action
 * and every rule run lands on the undo stack, so Ctrl+Z reverses it.
 *
 * Rules: conditions all must hold; actions come from the server's fixed
 * list (it has no status, stage, money or send action). A rule that changes
 * the mailbox (star, skip inbox, archive) is saved only after its dry run
 * ("would match N in the last 30 days") and an explicit confirmation.
 */
(() => {
  const M = window.EBMailCore;
  const S = M.S;
  const el = M.el;
  const $ = (id) => document.getElementById(id);
  let lastRunSeen = Date.now() / 1000;
  let picker = null;

  // ── thread actions ──
  function targetRef(ref) {
    return ref || S.cursorRef || S.openRef || (S.open && S.open.ref) || null;
  }

  async function threadOp(op, ref, extra) {
    ref = targetRef(ref);
    if (!ref) { M.say('Pick a message first'); return null; }
    let r;
    try { r = await M.post('/api/mail/thread/' + op, Object.assign({ ref }, extra || {})); }
    catch (e) { M.say(e.message); return null; }
    if (r.action_id) M.pushUndo(r.label || 'Done', [r.action_id]);
    else if (r.label) M.say(r.label);
    if (M.loadList) await M.loadList(true, true);
    return r;
  }

  function closePicker() {
    if (!picker) return;
    picker.remove(); picker = null;
    document.removeEventListener('keydown', pickerKeys, true);
  }
  function pickerKeys(e) {
    if (!picker) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePicker(); return; }
    const n = parseInt(e.key, 10);
    const btns = picker.querySelectorAll('button[data-n]');
    if (n >= 1 && n <= btns.length) { e.preventDefault(); e.stopPropagation(); btns[n - 1].click(); }
  }
  function pick(title, choices) {
    closePicker();
    const card = el('div', { class: 'ebx-card', role: 'dialog', 'aria-label': title }, [el('h3', { text: title })]);
    const list = el('div', { class: 'eb-pick' });
    choices.forEach((c, i) => list.appendChild(el('button', { class: 'mail-btn', dataset: { n: String(i + 1) },
      on: { click: () => { closePicker(); c.run(); } } }, [el('kbd', { text: String(i + 1) }), c.label])));
    card.appendChild(list);
    picker = el('div', { class: 'ebx-modal open' }, [card]);
    picker.addEventListener('click', (e) => { if (e.target === picker) closePicker(); });
    document.body.appendChild(picker);
    document.addEventListener('keydown', pickerKeys, true);
    const first = list.querySelector('button'); if (first) first.focus();
  }

  function at(hour, addDays) {
    const d = new Date(); d.setDate(d.getDate() + (addDays || 0)); d.setHours(hour, 0, 0, 0);
    return d.toISOString();
  }
  function nextMonday() {
    const d = new Date(); const add = ((8 - d.getDay()) % 7) || 7;
    return at(8, add);
  }

  function snooze(ref) {
    ref = targetRef(ref);
    if (!ref) return M.say('Pick a message first');
    pick('Snooze until', [
      { label: 'Later today (3 hours)', run: () => threadOp('snooze', ref, { hours: 3 }) },
      { label: 'Tomorrow morning', run: () => threadOp('snooze', ref, { until: at(8, 1) }) },
      { label: 'Next week', run: () => threadOp('snooze', ref, { until: nextMonday() }) },
      { label: 'Wake it now', run: () => threadOp('snooze', ref, { off: true }) },
    ]);
  }
  function waiting(ref) {
    ref = targetRef(ref);
    if (!ref) return M.say('Pick a message first');
    pick('Remind me if no reply by', [
      { label: 'In 2 days', run: () => threadOp('waiting', ref, { days: 2 }) },
      { label: 'In 3 days', run: () => threadOp('waiting', ref, { days: 3 }) },
      { label: 'In a week', run: () => threadOp('waiting', ref, { days: 7 }) },
      { label: 'Stop waiting', run: () => threadOp('waiting', ref, { off: true }) },
    ]);
  }
  function threadKey(k, ref) {
    if (k === 'z') snooze(ref);
    else if (k === 'w') waiting(ref);
    else if (k === 'm') threadOp('mute', ref);
  }

  async function mergeSelected() {
    const into = targetRef();
    const refs = [...S.sel].filter(r => r !== into);
    if (!into || !refs.length) { M.say('Select the messages to merge (x), then open the thread to merge them into'); return; }
    const ids = [];
    for (const ref of refs) {
      try { const r = await M.post('/api/mail/thread/merge', { ref, into }); if (r.action_id) ids.push(r.action_id); }
      catch (e) { M.say(e.message); }
    }
    S.sel.clear();
    M.pushUndo(`Merged ${ids.length} into one thread`, ids);
    if (M.loadList) await M.loadList(true, true);
  }

  async function unsubscribe(ref) {
    ref = targetRef(ref);
    if (!ref) return M.say('Pick a message first');
    let r;
    try { r = await M.post('/api/mail/thread/unsubscribe', { ref }); } catch (e) { M.say(e.message); return; }
    // Nothing is sent from here: a web link opens in the browser; a mailto
    // becomes a draft you send yourself.
    if (r.web) { window.open(r.web, '_blank', 'noopener,noreferrer'); M.say('The unsubscribe page opened in your browser'); return; }
    if (r.mailto && M.compose) {
      const u = r.mailto.replace(/^mailto:/i, '');
      const [to, q] = u.split('?');
      const p = new URLSearchParams(q || '');
      M.compose({ to: decodeURIComponent(to || ''), subject: p.get('subject') || 'unsubscribe', body_html: M.textToHtml ? M.textToHtml(p.get('body') || '') : '' });
      M.say('Unsubscribe draft ready. Review it and send it yourself.');
    }
  }

  // ── rule runs join the undo stack ──
  async function pollRuns() {
    if (!M.viewVisible || !M.viewVisible()) return;
    let r;
    try { r = await M.api('/api/mail/rules/runs?since=' + encodeURIComponent(String(lastRunSeen))); } catch (e) { return; }
    if (!r || !r.ok) return;
    const fresh = (r.runs || []).filter(x => !x.undone && x.action_id).reverse();
    if (r.runs && r.runs.length) lastRunSeen = Math.max(lastRunSeen, ...r.runs.map(x => x.ran_at));
    fresh.forEach((run) => {
      const did = (run.done || []).filter(d => !d.skipped && !d.error && !d.noop).map(d => d.type.replace(/_/g, ' '));
      M.pushUndo(`Rule “${run.rule}”: ${did.join(', ') || 'matched'}`, [run.action_id], fresh.length <= 3);
    });
    if (fresh.length > 3) M.toastAction(`${fresh.length} rule runs. Ctrl+Z undoes them one at a time.`, '', null, 8000);
  }
  setInterval(pollRuns, 20000);

  // ── the rules editor ──
  const FIELD_LABEL = { from: 'From contains', to: 'To or Cc contains', subject: 'Subject contains', body: 'Body contains',
    has_attachment: 'Has an attachment', list_id: 'Mailing list (List-Id) contains', project: 'Thread filed to project' };
  const ACTION_LABEL = { file_to_project: 'File to project', label: 'Add a label', star: 'Star', snooze: 'Snooze (hours)',
    skip_inbox: 'Skip the inbox (mark read, archive)', notify: 'Notify me', archive: 'Archive' };
  let meta = null;
  let modal = null;

  function closeRules() {
    if (!modal) return;
    modal.remove(); modal = null;
    document.removeEventListener('keydown', rulesKeys, true);
  }
  function rulesKeys(e) { if (e.key === 'Escape' && modal) { e.preventDefault(); e.stopPropagation(); closeRules(); } }
  function shell(title) {
    closeRules();
    const card = el('div', { class: 'ebx-card', role: 'dialog', 'aria-label': title }, [el('h3', { text: title })]);
    modal = el('div', { class: 'ebx-modal open', id: 'eb-rules' }, [card]);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeRules(); });
    document.body.appendChild(modal);
    document.addEventListener('keydown', rulesKeys, true);
    return card;
  }

  async function openRules() {
    try { meta = await M.api('/api/mail/rules'); } catch (e) { M.say(e.message); return; }
    const card = shell('Rules');
    card.appendChild(el('div', { class: 'rules-note', text: 'Rules run on new mail as it arrives, never on mail you already had. They can file, label, star, snooze, archive and notify. They never change a project’s status, stage or money, and never send anything.' }));
    if (!meta.rules.length) card.appendChild(el('div', { class: 'rules-note', text: 'No rules yet.' }));
    meta.rules.forEach((r) => {
      card.appendChild(el('div', { class: 'rules-row' }, [
        el('label', {}, [el('input', { type: 'checkbox', ...(r.enabled ? { checked: 'checked' } : {}), 'aria-label': 'On',
          on: { change: (e) => saveRule(Object.assign({}, r, { enabled: e.target.checked }), r.id, !!r.dry_run_ack, true) } })]),
        el('span', { class: 'rn', text: r.name }),
        el('button', { class: 'mail-btn', text: 'Edit', on: { click: () => editRule(r) } }),
        el('button', { class: 'mail-btn danger', text: 'Delete', on: { click: () => deleteRule(r) } }),
      ]));
    });
    card.appendChild(el('div', { class: 'rules-actions' }, [
      el('button', { class: 'mail-btn', text: 'Close', on: { click: closeRules } }),
      el('button', { class: 'mail-btn primary', text: 'New rule', on: { click: () => editRule(null) } }),
    ]));
  }

  function options(sel, values, labels, cur) {
    values.forEach(v => sel.appendChild(el('option', { value: v, ...(v === cur ? { selected: 'selected' } : {}) }, [labels[v] || v])));
    return sel;
  }
  function projectChoices() {
    try { return (typeof DATA !== 'undefined' && DATA && DATA.projects ? DATA.projects : []).filter(p => !p.archived); } catch (e) { return []; }
  }
  function valueInput(kind, type, value) {
    if (kind === 'cond' && type === 'has_attachment') {
      return options(el('select', { 'aria-label': 'Value' }), ['true', 'false'], { true: 'yes', false: 'no' }, String(value !== false && value !== 'false'));
    }
    if ((kind === 'act' && type === 'file_to_project') || (kind === 'cond' && type === 'project')) {
      const sel = el('select', { 'aria-label': 'Project' });
      if (kind === 'cond') options(sel, ['any', 'none'], { any: 'any project', none: 'no project yet' }, value);
      projectChoices().forEach(p => sel.appendChild(el('option', { value: p.id, ...(p.id === value ? { selected: 'selected' } : {}) }, [p.name || p.id])));
      return sel;
    }
    if (kind === 'act' && ['star', 'skip_inbox', 'archive', 'notify'].includes(type)) return el('span');
    return el('input', { type: kind === 'act' && type === 'snooze' ? 'number' : 'text', value: value == null ? '' : String(value), 'aria-label': 'Value' });
  }
  function line(kind, item, host) {
    const types = kind === 'cond' ? meta.conditions : meta.actions;
    const labels = kind === 'cond' ? FIELD_LABEL : ACTION_LABEL;
    const row = el('div', { class: 'rules-line', dataset: { kind } });
    const sel = options(el('select', { 'aria-label': kind === 'cond' ? 'Condition' : 'Action' }), types, labels, item[kind === 'cond' ? 'field' : 'type']);
    let input = valueInput(kind, sel.value, item.value);
    sel.addEventListener('change', () => { const n = valueInput(kind, sel.value, ''); row.replaceChild(n, input); input = n; });
    row.appendChild(sel); row.appendChild(input);
    row.appendChild(el('button', { class: 'mail-btn', text: '×', title: 'Remove', on: { click: () => row.remove() } }));
    row._read = () => {
      const v = input.tagName === 'SPAN' ? '' : input.value;
      return kind === 'cond' ? { field: sel.value, value: sel.value === 'has_attachment' ? v === 'true' : v } : { type: sel.value, value: v };
    };
    host.appendChild(row);
  }

  function editRule(rule) {
    const r = rule || { name: '', conditions: [{ field: 'from', value: '' }], actions: [{ type: 'label', value: '' }] };
    const card = shell(rule ? 'Edit rule' : 'New rule');
    const name = el('input', { type: 'text', value: r.name || '', placeholder: 'Name', 'aria-label': 'Rule name' });
    card.appendChild(el('div', { class: 'rules-line' }, [name]));
    card.appendChild(el('div', { class: 'rules-note', text: 'When all of these match:' }));
    const conds = el('div'); card.appendChild(conds);
    r.conditions.forEach(c => line('cond', c, conds));
    card.appendChild(el('button', { class: 'mail-btn', text: '+ condition', on: { click: () => line('cond', { field: 'from', value: '' }, conds) } }));
    card.appendChild(el('div', { class: 'rules-note', text: 'Do this:' }));
    const acts = el('div'); card.appendChild(acts);
    r.actions.forEach(a => line('act', a, acts));
    card.appendChild(el('button', { class: 'mail-btn', text: '+ action', on: { click: () => line('act', { type: 'label', value: '' }, acts) } }));
    const dry = el('div', { class: 'rules-dry', text: 'Run the dry run to see what this rule would have matched.' });
    const ack = el('input', { type: 'checkbox', id: 'rules-ack' });
    const ackRow = el('label', { class: 'rules-note' }, [ack, ' This rule changes my mailbox. I checked the dry run.']);
    ackRow.style.display = 'none';
    card.appendChild(dry); card.appendChild(ackRow);
    const read = () => ({ name: name.value, enabled: rule ? rule.enabled : true,
      conditions: [...conds.children].map(x => x._read()), actions: [...acts.children].map(x => x._read()) });
    const runDry = async () => {
      try {
        const d = await M.post('/api/mail/rules/dry-run', { rule: read() });
        dry.textContent = `Would match ${d.matches} message${d.matches === 1 ? '' : 's'} in the last ${d.days} days.`
          + (d.examples.length ? ' For example: ' + d.examples.map(x => `“${x.subject}”`).join(', ') + '.' : '');
        ackRow.style.display = d.changes_mailbox ? '' : 'none';
        if (!d.changes_mailbox) ack.checked = false;
      } catch (e) { dry.textContent = e.message; }
    };
    card.appendChild(el('div', { class: 'rules-actions' }, [
      el('button', { class: 'mail-btn', text: 'Cancel', on: { click: openRules } }),
      el('button', { class: 'mail-btn', text: 'Dry run', on: { click: runDry } }),
      el('button', { class: 'mail-btn primary', text: 'Save', on: { click: () => saveRule(read(), rule && rule.id, ack.checked) } }),
    ]));
    name.focus();
  }

  async function saveRule(rule, id, acked, quiet) {
    try {
      const payload = { rule, dry_run_ack: !!acked };
      if (id) await M.put('/api/mail/rules/' + id, payload); else await M.post('/api/mail/rules', payload);
      if (!quiet) M.say('Rule saved');
      openRules();
    } catch (e) { M.say(e.message); }
  }
  async function deleteRule(r) {
    try { await M.del_('/api/mail/rules/' + r.id); M.say(`Deleted “${r.name}”`); openRules(); }
    catch (e) { M.say(e.message); }
  }

  // ── palette commands ──
  if (window.PBPalette && PBPalette.addCommands) {
    const go = (fn) => PBPalette.mailThen(fn);
    PBPalette.addCommands([
      { label: 'Snooze thread', hint: 'z', run: () => go(() => snooze()) },
      { label: 'Waiting on a reply (nudge me)', hint: 'w', run: () => go(() => waiting()) },
      { label: 'Mute / unmute thread', hint: 'm', run: () => go(() => threadOp('mute')) },
      { label: 'Pin / unpin thread', run: () => go(() => threadOp('pin')) },
      { label: 'Split this message into its own thread', run: () => go(() => threadOp('split')) },
      { label: 'Merge selected messages into this thread', run: () => go(mergeSelected) },
      { label: 'Unsubscribe from this mailing list', run: () => go(() => unsubscribe()) },
      { label: 'Rules', run: () => go(openRules) },
    ]);
  }

  Object.assign(M, { threadKey, threadOp, snooze, waiting, openRules, unsubscribe, pollRuns });
})();
