/* Email Boss mail client (the MAIL view).
 *
 * Talks only to /api/mail/client/* (plus /api/drafts and /api/ai/draft-reply).
 * Every request carries the same-origin credentials and the X-Requested-With
 * header; writes are JSON. Everything here is idempotent: renderBoard() calls
 * PBMail.show() on every 30 s cycle and that must not rebuild the pane or
 * lose the open message / composer.
 *
 * Bodies render inside a sandboxed iframe (no scripts, no same-origin). Remote
 * images and CSS url() are replaced by a placeholder until the person presses
 * "Load images"; then each image is fetched through the server proxy
 * (/api/mail/client/image) and handed to the frame as a data: URL, so the page
 * CSP stays img-src 'self' data:. CSS url(), image-set, escapes, svg and math
 * never reach the frame. Anything that reaches the composer's innerHTML has been
 * sanitized on the server and is sanitized again here.
 *
 * Sending: Send asks "Send to X and N others?", then waits 10 seconds with an
 * Undo toast before the POST. Archive / Trash get an Undo toast too. The
 * composer autosaves to the board draft outbox every 20 seconds.
 */
window.PBMail = (() => {
  const M = window.EBMailCore;
  const S = M.S;
  const aiDraft = (...a) => M.aiDraft(...a);
  const attach = (...a) => M.attach(...a);
  const closeCompose = (...a) => M.closeCompose(...a);
  const compose = (...a) => M.compose(...a);
  const del = (...a) => M.del(...a);
  const fileTo = (...a) => M.fileTo(...a);
  const flag = (...a) => M.flag(...a);
  const hide = (...a) => M.hide(...a);
  const loadCaps = (...a) => M.loadCaps(...a);
  const markRead = (...a) => M.markRead(...a);
  const more = (...a) => M.more(...a);
  const move = (...a) => M.move(...a);
  const openMessage = (...a) => M.openMessage(...a);
  const prepareBody = (...a) => M.prepareBody(...a);
  const refresh = (...a) => M.refresh(...a);
  const reloadFolders = (...a) => M.reloadFolders(...a);
  const removeAtt = (...a) => M.removeAtt(...a);
  const renderFolders = (...a) => M.renderFolders(...a);
  const reply = (...a) => M.reply(...a);
  const sanitizeClient = (...a) => M.sanitizeClient(...a);
  const saveDraft = (...a) => M.saveDraft(...a);
  const searchServer = (...a) => M.searchServer(...a);
  const selectFolder = (...a) => M.selectFolder(...a);
  const send = (...a) => M.send(...a);
  const show = (...a) => M.show(...a);
  const stage = (...a) => M.stage(...a);
  const toggleCc = (...a) => M.toggleCc(...a);
  const toggleKeys = (...a) => M.toggleKeys(...a);
  const trash = (...a) => M.trash(...a);
  return { show, hide, selectFolder, reloadFolders, openMessage, more, refresh, searchServer, flag, markRead, move, del, trash, fileTo, stage,
           compose, closeCompose, toggleCc, attach, removeAtt, reply, send, saveDraft, aiDraft, toggleKeys, state: S,
           _sanitizeClient: sanitizeClient, _prepareBody: prepareBody,
           // Settings flipped a mail flag: re-read capabilities (send gate, provider) without losing the open folder.
           resetCaps: async () => { S.caps = null; if (S.built) { await loadCaps(); renderFolders(); } } };
})();

