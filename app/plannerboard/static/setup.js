/* Email Boss setup wizard (moved out of setup.html so the page runs under script-src 'self'). */
const STEPS = ['you', 'work', 'done'];
const LABELS = { you: 'You and your email', work: 'Your work', done: 'Done' };
let cur = 0;
let PROFESSIONS = [];
let selectedProfession = '';
let IMAP_PRESETS = [];
let provider = '';            // gmail | outlook | m365 | yahoo | icloud | other
let method = 'password';      // oauth | password
let READY = { gmail: false, graph: false };
let oauthStarted = false;
let oauthOk = false;          // /api/mail/status answered ok for the signed-in provider
let oauthPoll = null;
let mailTested = false;       // the app password passed Test
let CFG = null;
let TZ = '';

const PROVIDERS = [
  { id: 'gmail', label: 'Gmail', note: 'Ends in gmail.com', preset: 'gmail', oauth: 'gmail' },
  { id: 'outlook', label: 'Outlook or Microsoft 365', note: 'Outlook.com, Hotmail, or work email', preset: 'outlook', oauth: 'graph' },
  { id: 'yahoo', label: 'Yahoo', note: 'Ends in yahoo.com', preset: 'yahoo' },
  { id: 'icloud', label: 'iCloud', note: 'Ends in icloud.com or me.com', preset: 'icloud' },
  { id: 'other', label: 'Something else', note: 'Any other email service', preset: 'custom' },
];
const GUIDES = {
  gmail: ['Open your Google Account (myaccount.google.com) and choose <b>Security</b>.',
    'Make sure <b>2-Step Verification</b> is on. Turn it on if it is not.',
    'Search for <b>App passwords</b> and open it.',
    'Name it <b>Email Boss</b> and press Create.',
    'Copy the 16 letters Google shows you and paste them below.'],
  outlook: ['Sign in at account.microsoft.com and choose <b>Security</b>.',
    'Open <b>Advanced security options</b>.',
    'Make sure two-step verification is on, then choose <b>Create a new app password</b>.',
    'Copy the password it shows and paste it below.'],
  yahoo: ['Sign in to Yahoo and open <b>Account Info</b>, then <b>Account Security</b>.',
    'Choose <b>Generate app password</b>.',
    'Name it <b>Email Boss</b> and press Generate.',
    'Copy the password and paste it below.'],
  icloud: ['Go to <b>appleid.apple.com</b> and sign in.',
    'Open <b>Sign-In and Security</b>, then <b>App-Specific Passwords</b>.',
    'Press the plus button and name it <b>Email Boss</b>.',
    'Copy the password and paste it below.'],
  other: ['Sign in to your email service on the web and look in its security settings for <b>App passwords</b>, or ask whoever runs your email.',
    'Create one named <b>Email Boss</b> and paste it below.',
    'Open Advanced and enter the mail server name your email service gave you.'],
};
const PROF_TEXT = {
  general: ['Something else', 'A bit of everything'],
  trades: ['Trades and home services', 'Plumbing, electrical, HVAC, handyman'],
  hvac_contractor: ['Heating and cooling contractor', 'Service calls, quotes, maintenance plans'],
  construction_gc: ['Building contractor', 'Bids, change orders, subcontractors'],
  events: ['Events and photography', 'Weddings, shoots, parties'],
  nonprofit: ['Church, club or office', 'Requests, rooms, funerals, events'],
  legal: ['Law practice', 'Clients, matters, deadlines'],
  real_estate: ['Real estate', 'Buyers, sellers, showings'],
  property_management: ['Property management', 'Tenants, repairs, owners'],
  insurance_agency: ['Insurance agency', 'Policies, renewals, claims'],
  consulting: ['Consulting', 'Clients, proposals, projects'],
  recruiting: ['Recruiting', 'Candidates, openings, interviews'],
  it_msp: ['Computer support', 'Help desk, small business IT'],
};
const AI_DEFAULT_MODEL = { anthropic: 'claude-sonnet-4-5', openai: 'gpt-4o-mini', ollama: 'llama3.1' };
const AI_DEFAULT_URL = { anthropic: '', openai: 'https://api.openai.com/v1', ollama: 'http://localhost:11434' };

const $ = id => document.getElementById(id);
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function toggle(id) { $(id).hidden = !$(id).hidden; }

const PB_TOKEN = (document.querySelector('meta[name="pb-token"]') || {}).content || '';
async function api(url, opts) {
  opts = Object.assign({}, opts || {});
  opts.credentials = 'same-origin';
  opts.headers = Object.assign({ 'X-Requested-With': 'EmailBoss' }, opts.headers || {});
  if (PB_TOKEN) opts.headers['Authorization'] = 'Bearer ' + PB_TOKEN;
  const m = (opts.method || 'GET').toUpperCase();
  if (m !== 'GET') opts.headers['Content-Type'] = 'application/json';
  return fetch(url, opts);
}
async function jget(url) { try { const r = await api(url); return r.ok ? await r.json() : null; } catch (e) { return null; } }

// ── steps ─────────────────────────────────────────────────────────────────
function render() {
  $('dots').innerHTML = STEPS.map((s, i) =>
    `<span class="dot ${i < cur ? 'done' : ''} ${i === cur ? 'cur' : ''}">${i + 1}. ${esc(LABELS[s])}</span>`).join('');
  document.querySelectorAll('.card[data-step]').forEach(c => { c.hidden = c.dataset.step !== STEPS[cur]; });
  $('back').style.visibility = cur === 0 ? 'hidden' : 'visible';
  $('next').style.display = cur === STEPS.length - 1 ? 'none' : '';
  syncNext();
  if (STEPS[cur] === 'done') $('pa-path').textContent = ($('data_root').value.trim() || 'your Email Boss folder') + '/mail_cache';
  window.scrollTo(0, 0);
}
function validate(i) {
  const s = STEPS[i];
  if (s === 'you') {
    if (!$('name').value.trim()) return 'Please type your name.';
    if ($('email').value.indexOf('@') < 1) return 'Please type your email address, like jane@example.com.';
    if (!provider) return 'Please choose which email you use.';
    if (!$('use_pa').checked) {
      if (method === 'oauth' && oauthKind()) {
        if (!oauthStarted) return 'Press the sign-in button to connect your email, or choose "Use an app password instead".';
        if (!oauthOk) return 'Waiting for sign-in... Finish signing in in the other tab, or choose "Use an app password instead".';
      } else if (!$('imap_password').value || !mailTested) {
        return 'Paste your app password, then press Test';
      }
    }
  }
  if (s === 'work' && !selectedProfession) return 'Please pick the kind of work you do. Pick the nearest.';
  return '';
}
function step(d) {
  const target = Math.max(0, Math.min(STEPS.length - 1, cur + d));
  if (d > 0) {
    const err = validate(cur);
    if (err) { $('err-' + STEPS[cur]).textContent = err; return; }
    $('err-' + STEPS[cur]).textContent = '';
  }
  cur = target; render();
}

// ── screen 1 ──────────────────────────────────────────────────────────────
function renderProviders() {
  $('providers').innerHTML = PROVIDERS.map(p =>
    `<button type="button" class="choice ${p.id === provider ? 'sel' : ''}" data-on-click="${EH(null, EK('pickProvider', p.id))}"><b>${esc(p.label)}</b><span>${esc(p.note)}</span></button>`).join('');
}
function provMeta() { return PROVIDERS.find(p => p.id === provider) || null; }
function oauthKind() { const m = provMeta(); return m && m.oauth && READY[m.oauth] ? m.oauth : ''; }
function pickProvider(id) {
  provider = id; oauthStarted = false; oauthOk = false; mailTested = false; stopOauthPoll();
  $('oauthmsg').textContent = '';
  renderProviders();
  $('mailbox').hidden = false;
  applyPreset(true);
  setMethod(oauthKind() ? 'oauth' : 'password');
  const em = $('email').value.trim();
  if (em && !$('imap_user').value.trim()) $('imap_user').value = em;
}
function setMethod(m) {
  method = (m === 'oauth' && oauthKind()) ? 'oauth' : 'password';
  if (method !== 'oauth') stopOauthPoll();
  syncNext();
  $('oauth-block').hidden = method !== 'oauth';
  $('pw-block').hidden = method !== 'password';
  $('back-oauth').hidden = !oauthKind();
  $('m365-note').hidden = provider !== 'outlook';
  $('oauth-btn').textContent = oauthKind() === 'graph' ? 'Sign in with Microsoft' : 'Sign in with Google';
  $('guide').innerHTML = (GUIDES[provider] || []).map(t => `<li>${t}</li>`).join('');
}
function currentPreset() { const m = provMeta(); return IMAP_PRESETS.find(p => m && p.id === m.preset) || null; }
function applyPreset() {
  const p = currentPreset(); if (!p) return;
  $('imap_host').value = p.host || '';
  $('imap_port').value = p.port || 993;
  $('imap_inbox').value = p.inbox_folder || 'INBOX';
  $('imap_sent').value = p.sent_folder || 'Sent';
}
function changeTz() { $('tzbox').hidden = false; $('tzinput').value = TZ; $('tzinput').focus(); }
function getTz() { return ($('tzinput').value || TZ || '').trim(); }

function imapUser() { return $('imap_user').value.trim() || $('email').value.trim(); }
function mailBody() {
  const poll = Math.max(60, (parseInt($('poll_min').value, 10) || 5) * 60);
  if ($('use_pa').checked) return { provider: 'pa_folder', poll_seconds: poll };
  if (method === 'oauth' && oauthKind()) {
    const k = oauthKind(), s = {};
    const cid = $('oauth_client_id').value.trim(); if (cid) s.client_id = cid;
    if (k === 'graph') s.tenant = $('oauth_tenant').value.trim() || 'common';
    const m = { provider: k, poll_seconds: poll }; m[k] = s; return m;
  }
  const p = currentPreset();
  return { provider: 'imap', poll_seconds: poll,
    imap: { preset: p ? p.id : 'custom', host: $('imap_host').value.trim(), port: parseInt($('imap_port').value, 10) || 993,
            user: imapUser(), inbox: $('imap_inbox').value.trim() || 'INBOX', sent: $('imap_sent').value.trim() || 'Sent' } };
}

function friendlyMailError(raw, status) {
  const t = String(raw || '').toLowerCase();
  if (/authenticationfailed|invalid credentials|login failed|auth.*fail|\[auth|535|username and password|bad credentials|password/.test(t)) {
    if (/application-specific|app.?specific|two.?step|2-?step|2fa|web login required|webalert/.test(t))
      return 'Your email needs 2-Step Verification turned on before it will give out app passwords. Turn it on, make an app password, and try again.';
    return 'That password was not accepted. Use the app password from the steps above, not your normal password. Check for typos or extra spaces.';
  }
  if (/application-specific|app.?specific|two.?step|2-?step|web login required|webalert/.test(t))
    return 'Your email needs 2-Step Verification turned on before it will give out app passwords. Turn it on, make an app password, and try again.';
  if (/imap.*(disabled|not enabled)|disabled.*imap|imap access|not enabled for imap/.test(t))
    return 'Your email service has outside access switched off. In your email settings, turn on IMAP access, then try again.';
  if (/getaddrinfo|name or service|network is unreachable|timed out|timeout|connection refused|no route|offline|failed to fetch|networkerror/.test(t))
    return 'Email Boss could not reach the internet. Check that this computer is online, then try again.';
  if (/folder|mailbox|nonexistent|no such/.test(t))
    return 'Signed in, but a mail folder was not found. Open Advanced and check the Inbox and Sent folder names.';
  return 'The test did not work' + (status ? ' (code ' + status + ')' : '') + '. Check the app password and try again. If it keeps failing, open Advanced and check the mail server.';
}
async function testMail() {
  const msg = $('mailmsg'); msg.className = 'msg'; msg.textContent = 'Testing...';
  if (!$('imap_password').value) { msg.className = 'msg err'; msg.textContent = 'Paste your app password first.'; return; }
  try {
    const body = mailBody();
    body.imap_password = $('imap_password').value;
    const r = await api('/api/mail/test', { method: 'POST', body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok) { mailTested = true; syncNext(); $('err-you').textContent = ''; msg.className = 'msg ok'; msg.textContent = 'Connected. Your email works.'; return; }
    mailTested = false; syncNext();
    let why = j.error || j.detail || '';
    for (const k of ['inbox', 'sent']) { const f = (j.folders || {})[k]; if (f && f.ok === false) why += ' folder ' + (f.error || 'not found'); }
    msg.className = 'msg err'; msg.textContent = friendlyMailError(why, r.status);
  } catch (e) { msg.className = 'msg err'; msg.textContent = friendlyMailError(String(e)); }
}
async function oauthStart() {
  const kind = oauthKind(); if (!kind) return;
  const msg = $('oauthmsg'); msg.className = 'msg'; msg.textContent = 'Starting sign-in...';
  try {
    const settings = {};
    const cid = $('oauth_client_id').value.trim(); if (cid) settings.client_id = cid;
    if (kind === 'graph') settings.tenant = $('oauth_tenant').value.trim() || 'common';
    const r = await api('/api/mail/oauth/start', { method: 'POST', body: JSON.stringify({ provider: kind, settings }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) { msg.className = 'msg err'; msg.textContent = 'Sign-in could not start. You can use an app password instead.'; return; }
    const url = j.url || j.auth_url;
    if (url) { window.open(url, '_blank', 'noopener'); oauthStarted = true; oauthOk = false; msg.className = 'msg'; msg.textContent = 'Waiting for sign-in... Finish in the new tab; this page moves on by itself.'; startOauthPoll(kind); }
    else { msg.className = 'msg err'; msg.textContent = 'Sign-in could not start. You can use an app password instead.'; }
  } catch (e) { msg.className = 'msg err'; msg.textContent = 'Could not reach Email Boss. Try again.'; }
}

// OAuth: after the sign-in tab opens, ask /api/mail/status every 2 s; Next
// lights up only once the signed-in provider answers ok.
function stopOauthPoll() { if (oauthPoll) { clearInterval(oauthPoll); oauthPoll = null; } }
function startOauthPoll(kind) {
  stopOauthPoll(); syncNext();
  const tick = async () => {
    const j = await jget('/api/mail/status');
    if (j && j.ok === true && (!j.provider || j.provider === kind)) {
      oauthOk = true; stopOauthPoll(); syncNext();
      const msg = $('oauthmsg'); msg.className = 'msg ok'; msg.textContent = 'Connected. Press Next.';
      $('err-you').textContent = '';
    }
  };
  oauthPoll = setInterval(tick, 2000);
}
// Next stays clickable (it explains what is missing) but looks ready only when it is.
function syncNext() {
  const n = $('next'); if (!n) return;
  const ready = STEPS[cur] !== 'you' || !validate(cur);
  n.classList.toggle('waiting', !ready);
  // Mid sign-in, Next is off until the mailbox answers; the escape link stays.
  n.disabled = STEPS[cur] === 'you' && method === 'oauth' && !!oauthKind() && oauthStarted && !oauthOk && !$('use_pa').checked;
}
function pwChanged() { mailTested = false; syncNext(); }

// ── screen 2 ──────────────────────────────────────────────────────────────
function renderProfessions() {
  const order = Object.keys(PROF_TEXT);
  const list = PROFESSIONS.filter(p => !p.error).slice().sort((a, b) => {
    const ia = order.indexOf(a.id), ib = order.indexOf(b.id);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  $('professions').innerHTML = list.map(p => {
    const t = PROF_TEXT[p.id] || [p.label, p.description || ''];
    return `<button type="button" class="choice ${p.id === selectedProfession ? 'sel' : ''}" data-on-click="${EH(null, EK('pickProfession', p.id))}"><b>${esc(t[0])}</b><span>${esc(t[1])}</span></button>`;
  }).join('');
}
function pickProfession(id) { selectedProfession = id; $('err-work').textContent = ''; renderProfessions(); }

// ── screen 3 ──────────────────────────────────────────────────────────────
function aiMode() { return $('ai_own').checked ? 'own' : ($('ai_off').checked ? 'none' : 'emailboss'); }
function aiModeChanged() { $('ai-own-block').hidden = aiMode() !== 'own'; if (aiMode() === 'own') aiProviderChanged(true); }
function aiProviderChanged(keep) {
  const p = $('ai_provider').value;
  if (!keep || !$('ai_model').value) $('ai_model').value = AI_DEFAULT_MODEL[p] || '';
  if (!keep || !$('ai_base_url').value) $('ai_base_url').value = AI_DEFAULT_URL[p] || '';
  $('ai-key-wrap').style.visibility = p === 'ollama' ? 'hidden' : 'visible';
}
function paChanged() { $('pa-hint').hidden = !$('use_pa').checked; syncNext(); }
function aiBody() {
  const m = aiMode();
  if (m === 'none') return { provider: 'none' };
  if (m === 'emailboss') return { provider: 'emailboss' };
  const p = $('ai_provider').value;
  const a = { provider: p, model: $('ai_model').value.trim(), base_url: $('ai_base_url').value.trim() };
  if ($('ai_api_key').value && p !== 'ollama') a.api_key = $('ai_api_key').value;
  return a;
}
async function testAI() {
  const msg = $('aimsg'); msg.className = 'msg'; msg.textContent = 'Testing...';
  try {
    const r = await api('/api/ai/test', { method: 'POST', body: JSON.stringify(aiBody()) });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok) { msg.className = 'msg ok'; msg.textContent = 'AI works.'; }
    else { msg.className = 'msg err'; msg.textContent = 'The AI test did not work. Check the key and the model name.'; }
  } catch (e) { msg.className = 'msg err'; msg.textContent = 'Could not reach Email Boss. Try again.'; }
}

// POST /api/setup body, compatible with api/setup.py + config.build_document.
function buildBody() {
  const tz = getTz();
  const bind = $('bind').value.trim() || '127.0.0.1';
  const body = {
    user: { name: $('name').value.trim(), email: $('email').value.trim() },
    profession: selectedProfession,
    paths: {},
    server: { bind: bind, port: parseInt($('port').value, 10) || 8080 },
    mail: mailBody(),
    ai: aiBody(),
    license: { key: $('license_key').value.trim() },
    features: { money: true, game_layer: true, analytics: true },
    force: true,
  };
  if ($('data_root').value.trim()) body.paths.data_root = $('data_root').value.trim();
  if (tz) { body.timezone = tz; body.business_hours = { tz: tz }; body.config = { timezone: tz }; }
  if (body.mail.provider === 'imap' && $('imap_password').value) body.mail.imap_password = $('imap_password').value;
  return body;
}

async function save() {
  for (let k = 0; k < 2; k++) { const err = validate(k); if (err) { cur = k; render(); $('err-' + STEPS[k]).textContent = err; return; } }
  const body = buildBody();
  $('save').disabled = true;
  const msg = $('msg'); msg.className = 'msg'; msg.textContent = 'Setting things up...';
  let j;
  try {
    const r = await api('/api/setup', { method: 'POST', body: JSON.stringify(body) });
    j = await r.json().catch(() => ({}));
    if (!r.ok) {
      msg.className = 'msg err';
      msg.textContent = r.status === 400 && j.error ? j.error : 'Setup did not finish (code ' + r.status + '). Please try again.';
      $('save').disabled = false; return;
    }
  } catch (e) { msg.className = 'msg err'; msg.textContent = 'Could not reach Email Boss. Try again.'; $('save').disabled = false; return; }
  $('imap_password').value = ''; $('ai_api_key').value = '';

  // Best effort: store the time zone (older servers ignore it in /api/setup).
  const tz = getTz();
  if (tz) { try { await api('/api/config', { method: 'PUT', body: JSON.stringify({ timezone: tz, business_hours: { tz: tz } }) }); } catch (e) {} }
  // Free trial; if it fails the board shows the license banner.
  try { await api('/api/license/trial', { method: 'POST', body: JSON.stringify({ email: body.user.email }) }); } catch (e) {}

  const extra = (j.api_token ? '<br>Your security token: <code>' + esc(j.api_token) + '</code>' : '');
  if (j.restart_required) {
    let restarted = false;
    try { const r = await api('/api/restart', { method: 'POST', body: '{}' }); restarted = r.ok; } catch (e) {}
    msg.className = 'msg ok';
    msg.innerHTML = restarted
      ? 'Almost done. Email Boss is restarting. This page will open it in a few seconds.' + extra
      : '<b>Close and reopen Email Boss</b> to finish. Your choices are saved.' + extra;
    if (restarted) setTimeout(function wait() {
      api('/api/health').then(r => { if (r.ok) location.href = '/'; else setTimeout(wait, 1500); }).catch(() => setTimeout(wait, 1500));
    }, 3000);
    return;
  }
  msg.className = 'msg ok'; msg.innerHTML = 'All set.' + extra;
  setTimeout(() => { location.href = '/'; }, j.api_token ? 6000 : 600);
}

async function boot() {
  try { TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { TZ = ''; }
  $('tzname').textContent = TZ || 'not detected';
  if (!TZ) changeTz();
  CFG = await jget('/api/config');
  if (CFG) {
    const u = CFG.user || {};
    if (u.name) $('name').value = u.name;
    if (u.email) $('email').value = u.email;
    if (CFG.paths && CFG.paths.data_root) $('data_root').value = CFG.paths.data_root;
    if (CFG.server && CFG.server.bind) $('bind').value = CFG.server.bind;
    if (CFG.server && CFG.server.port) $('port').value = CFG.server.port;
    if (CFG.profession && CFG.profession.template && CFG.configured) selectedProfession = CFG.profession.template;
  }
  const probe = await jget('/api/setup/probe');
  if (probe && probe.suggest && probe.suggest.data_root) $('data_default').textContent = 'Default: ' + probe.suggest.data_root;
  const rd = await jget('/api/mail/oauth/ready');
  if (rd) READY = { gmail: !!rd.gmail, graph: !!rd.graph };
  const mp = await jget('/api/mail/providers');
  const imap = mp && (mp.providers || []).find(x => x.id === 'imap');
  IMAP_PRESETS = (imap && imap.presets) || (mp && mp.imap_presets) || [];
  if (!IMAP_PRESETS.length) {
    IMAP_PRESETS = [
      { id: 'gmail', host: 'imap.gmail.com', port: 993, inbox_folder: 'INBOX', sent_folder: '[Gmail]/Sent Mail' },
      { id: 'outlook', host: 'outlook.office365.com', port: 993, inbox_folder: 'INBOX', sent_folder: 'Sent' },
      { id: 'icloud', host: 'imap.mail.me.com', port: 993, inbox_folder: 'INBOX', sent_folder: 'Sent Messages' },
      { id: 'yahoo', host: 'imap.mail.yahoo.com', port: 993, inbox_folder: 'INBOX', sent_folder: 'Sent' },
      { id: 'custom', host: '', port: 993, inbox_folder: 'INBOX', sent_folder: 'Sent' },
    ];
  }
  const pj = await jget('/api/professions');
  PROFESSIONS = (pj && pj.professions) || [];
  if (!PROFESSIONS.length) $('professions').innerHTML = '<div class="msg err">Could not load the list. Is Email Boss running?</div>';
  else renderProfessions();
  renderProviders();
  aiProviderChanged(false);
  render();
}
boot();
