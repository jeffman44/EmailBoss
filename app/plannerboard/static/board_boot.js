// Every /api/ call carries same-origin credentials, the CSRF marker header, and
// a JSON content type on writes (the engine answers 415 otherwise).
(function () {
  const _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const u = new URL(url, location.href);
      if (u.origin === location.origin && u.pathname.indexOf('/api/') === 0) {
        init = Object.assign({}, init || {});
        init.credentials = 'same-origin';
        const h = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
        h.set('X-Requested-With', 'EmailBoss');
        const method = String(init.method || (typeof input !== 'string' && input.method) || 'GET').toUpperCase();
        const isForm = typeof FormData !== 'undefined' && init.body instanceof FormData;
        if (method !== 'GET' && method !== 'HEAD' && !isForm) h.set('Content-Type', 'application/json');
        init.headers = h;
      }
    } catch (e) { /* fall through to the plain fetch */ }
    return _fetch(input, init);
  };
})();

// ---------------------------------------------------------------------------
// Delegated UI events. The page carries no inline event handlers: markup holds
// data-on-<event> attributes with a JSON call spec ({f, a, c, sp, pd, g}) and
// one listener per event type runs it. Data never reaches code: it travels as
// JSON arguments, HTML-escaped into the attribute, so the CSP can drop
// script-src 'unsafe-inline'.
// ---------------------------------------------------------------------------
(function () {
  var AMP = /[&"'<>]/g, ENT = { '&': '&amp;', '"': '&quot;', "'": '&#39;', '<': '&lt;', '>': '&gt;' };
  window.$T = { $: 'this' }; window.$V = { $: 'value' }; window.$C = { $: 'checked' }; window.$E = { $: 'event' };
  window.EBH = window.EBH || {};
  window.EK = function (f) { return { f: f, a: Array.prototype.slice.call(arguments, 1) }; };
  window.EH = function (opts) {
    var calls = Array.prototype.slice.call(arguments, 1);
    var o = calls.length === 1 ? { f: calls[0].f, a: calls[0].a } : { c: calls };
    if (opts) for (var k in opts) o[k] = opts[k];
    return JSON.stringify(o).replace(AMP, function (c) { return ENT[c]; });
  };
  function arg(a, el, ev) {
    if (a && typeof a === 'object' && !Array.isArray(a) && typeof a.$ === 'string') {
      if (a.$ === 'this') return el;
      if (a.$ === 'value') return el.value;
      if (a.$ === 'checked') return el.checked;
      if (a.$ === 'event') return ev;
    }
    return a;
  }
  function call(c, el, ev) {
    var fn = Object.prototype.hasOwnProperty.call(window.EBH, c.f) ? window.EBH[c.f] : window[c.f];
    if (typeof fn !== 'function') { console.error('ui handler missing: ' + c.f); return; }
    return fn.apply(null, (c.a || []).map(function (a) { return arg(a, el, ev); }));
  }
  var EVENTS = { click: 'click', change: 'change', input: 'input', keydown: 'keydown', focus: 'focusin', blur: 'focusout',
                 dragstart: 'dragstart', dragend: 'dragend', dragover: 'dragover', dragleave: 'dragleave', drop: 'drop' };
  Object.keys(EVENTS).forEach(function (name) {
    var attr = 'data-on-' + name;
    document.documentElement.addEventListener(EVENTS[name], function (ev) {
      var el = ev.target;
      while (el && el.nodeType === 1) {
        if (el.hasAttribute(attr) && ((name !== 'focus' && name !== 'blur') || el === ev.target)) {
          var spec = null;
          try { spec = JSON.parse(el.getAttribute(attr)); } catch (e) { console.error('ui handler unreadable'); }
          if (spec) {
            var ok = !spec.g || (spec.g === 'self' && ev.target === el) || (spec.g === 'enter' && ev.key === 'Enter');
            if (ok) {
              if (spec.pd) ev.preventDefault();
              (spec.c || [spec]).forEach(function (c) { call(c, el, ev); });
            }
            if (spec.sp) break;
          }
        }
        el = el.parentElement;
      }
    });
  });
})();
