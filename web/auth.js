/* Sign-in for every portal page. Load it first, in <head>.
 * - Not signed in (or the session expired): go to signin.html, then come back.
 * - Adds "Authorization: Bearer <token>" to every request to the portal API.
 * - A "please sign in" (401) reply from the API also returns you to the sign-in page.
 * - In the portal itself, fills #impilo-user-menu: your name, change password, manage users, sign out.
 */
(function () {
  'use strict';
  var API = (window.IMPILO_API_ORIGIN||"http://127.0.0.1:8765")+'';
  var KEY = 'impilo.auth.v1';
  var SIGNIN = 'signin.html';
  var isSignin = /signin\.html$/.test(location.pathname);

  function read() { try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { return null; } }
  function valid(s) { return s && s.token && s.expires * 1000 > Date.now(); }
  var session = read();

  function toSignin(reason) {
    try { localStorage.removeItem(KEY); } catch (e) { }
    var top = window.top || window;
    var next = encodeURIComponent(top.location.pathname.split('/').pop() + top.location.search);
    top.location.replace(SIGNIN + '?next=' + next + (reason ? '&reason=' + reason : ''));
  }

  window.ImpiloAuth = {
    api: API,
    get user() { return session && session.user; },
    get token() { return session && session.token; },
    save: function (s) { session = s; localStorage.setItem(KEY, JSON.stringify(s)); },
    signOut: function () { toSignin('signedout'); },
    // What this account may do (the API checks the same rules on every request).
    can: function (module) { var u = session && session.user; return !!u && (u.role === 'admin' || (u.modules || []).indexOf(module) >= 0); },
    mayDownload: function (kind) { var u = session && session.user; return !!u && (u.role === 'admin' || !!(u.downloads || {})[kind]); },
    get patientData() { var u = session && session.user; return !!u && (u.role === 'admin' || !!u.patientData); }
  };
  if (isSignin) return;
  if (!valid(session)) { toSignin(session ? 'expired' : ''); return; }
  // End the session at its expiry time even if the page stays open.
  setTimeout(function () { toSignin('expired'); }, Math.max(0, session.expires * 1000 - Date.now()));

  // Every request to the API carries the session; a 401 sends you back to sign in.
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    // fetch() accepts text, a URL object or a Request; the registers pass URL objects.
    var url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input && input.url) || '';
    if (url.indexOf(API + '/api/') !== 0) return realFetch(input, init);
    init = Object.assign({}, init || {});
    var headers = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
    headers.set('Authorization', 'Bearer ' + session.token);
    init.headers = headers;
    return realFetch(input, init).then(function (r) {
      if (r.status === 401) toSignin('expired');
      return r;
    });
  };

  // Another tab signing out (or in as someone else) applies here too.
  window.addEventListener('storage', function (e) {
    if (e.key !== KEY) return;
    var s = read();
    if (!valid(s) || !session || s.user.username !== session.user.username) location.reload(); else session = s;
  });

  // Download buttons someone isn't allowed to use are hidden (the API refuses them anyway).
  function guardDownloads() {
    if (!session || !session.user) return;
    var hide = [];
    if (!window.ImpiloAuth.mayDownload('registers') && /-register\.html$/.test(location.pathname)) hide.push('.download-block');
    if (!hide.length) return;
    var st = document.createElement('style'); st.textContent = hide.join(',') + '{display:none!important}'; document.head.appendChild(st);
  }
  guardDownloads();

  if (window.top !== window) return;  // the user menu lives in the portal shell only

  // Pick up permission changes made by an administrator since sign-in.
  fetch(API + '/api/auth/me', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (u) {
    if (!u || !session) return;
    var changed = JSON.stringify(u) !== JSON.stringify(session.user);
    session.user = u; localStorage.setItem(KEY, JSON.stringify(session));
    if (changed) guardNav();
  }).catch(function () { });

  // ------------------------------------------------------------------ sections this account may open
  var NAV = [['Overview', 'overview'], ['Data intelligence', 'intel'], ['Geospatial intelligence', 'geo'], ['Registers', 'registers'], ['Reports', 'reports'],
    ['Facilities reporting', 'facilities'], ['Ask the data', 'ask'], ['Data workspace', 'workspace'], ['Configuration', 'config'], ['Users & access', 'users']];
  function moduleOf(btn) { var t = btn.textContent.trim(); for (var i = 0; i < NAV.length; i++) if (t.indexOf(NAV[i][0]) === 0) return NAV[i][1]; return null; }
  function guardNav() {
    var buttons = document.querySelectorAll('nav[aria-label="Portal sections"] button');
    if (!buttons.length) return;
    var firstAllowed = null, activeBlocked = false;
    buttons.forEach(function (b) {
      var m = moduleOf(b), ok = !m || window.ImpiloAuth.can(m);
      if (b.style.display !== (ok ? '' : 'none')) b.style.display = ok ? '' : 'none';
      if (ok && !firstAllowed) firstAllowed = b;
      if (!ok && b.getAttribute('aria-current') === 'page') activeBlocked = true;
    });
    var ask = Array.prototype.find.call(document.querySelectorAll('button'), function (b) { return b.textContent.trim() === 'Ask the data' && !b.closest('nav'); });
    if (ask) ask.style.display = window.ImpiloAuth.can('ask') ? '' : 'none';
    if (activeBlocked && firstAllowed) firstAllowed.click();
  }
  function openSection(label) {
    var b = Array.prototype.find.call(document.querySelectorAll('nav[aria-label="Portal sections"] button'), function (x) { return x.textContent.trim().indexOf(label) === 0; });
    if (b) { b.click(); window.scrollTo({ top: 0 }); }
  }

  // ------------------------------------------------------------------ user menu (portal header)
  var css = document.createElement('style');
  css.textContent =
    '#impilo-user-menu{position:relative;font-family:Inter,-apple-system,"Segoe UI",sans-serif}' +
    '.um-btn{display:flex;align-items:center;gap:8px;border:1px solid #e3e9f3;background:#fff;border-radius:999px;padding:3px 10px 3px 3px;cursor:pointer;font-size:12.5px;font-weight:600;color:#0b1f3a}' +
    '.um-btn:hover{background:#f5f8fb}.um-av{width:28px;height:28px;border-radius:50%;background:#218a39;color:#fff;display:grid;place-items:center;font-size:11.5px;font-weight:700}' +
    '.um-pop{position:absolute;right:0;top:calc(100% + 6px);z-index:60;width:250px;background:#fff;border:1px solid #e3e9f3;border-radius:12px;box-shadow:0 18px 40px -16px rgba(11,31,58,.35);padding:6px}' +
    '.um-pop .who{padding:10px 10px 8px;border-bottom:1px solid #edf1f7;margin-bottom:4px;font-size:12.5px;color:#637797}.um-pop .who b{display:block;color:#0b1f3a;font-size:13.5px}' +
    '.um-pop button{display:block;width:100%;text-align:left;border:0;background:none;padding:9px 10px;border-radius:8px;font-size:13px;color:#0b1f3a;cursor:pointer}' +
    '.um-pop button:hover{background:#f0f6f1}.um-pop button.out{color:#b42318}' +
    '.um-modal{position:fixed;inset:0;z-index:100;background:rgba(11,31,58,.45);display:grid;place-items:center;padding:20px;font-family:Inter,-apple-system,"Segoe UI",sans-serif}' +
    '.um-card{background:#fff;border-radius:16px;width:min(560px,100%);max-height:90vh;overflow:auto;padding:22px;box-shadow:0 30px 80px -30px rgba(0,0,0,.5)}' +
    '.um-card h2{margin:0 0 4px;font-size:18px;color:#0b1f3a}.um-card .sub{font-size:12.5px;color:#637797;margin-bottom:12px}' +
    '.um-card label{display:block;font-size:12px;font-weight:700;color:#465f88;margin:12px 0 5px}' +
    '.um-card input,.um-card select{width:100%;box-sizing:border-box;border:1px solid #d7dfec;border-radius:9px;padding:9px 11px;font-size:13.5px;background:#f8fafc}' +
    '.um-row{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}' +
    '.um-b{border:1px solid #218a39;background:#218a39;color:#fff;border-radius:9px;padding:8px 14px;font-weight:600;font-size:13px;cursor:pointer}.um-b.g{background:#fff;color:#218a39}.um-b.r{background:#fff;color:#b42318;border-color:#f3c6c6}' +
    '.um-msg{font-size:12.5px;margin-top:10px;min-height:16px}.um-msg.bad{color:#b42318}.um-msg.ok{color:#176c2b}' +
    '.um-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}.um-table td,.um-table th{padding:8px 6px;border-bottom:1px solid #edf1f7;text-align:left}.um-table th{font-size:11px;color:#465f88}' +
    '.um-pill{font-size:11px;font-weight:600;border-radius:999px;padding:2px 8px;background:#ecf7ee;color:#176c2b}.um-pill.off{background:#f1f5f9;color:#637797}';
  document.head.appendChild(css);

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function initials(n) { return String(n || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(function (w) { return w[0].toUpperCase(); }).join(''); }
  function api(path, opts) {
    opts = opts || {};
    return fetch(API + path, Object.assign({ cache: 'no-store' }, opts, { headers: Object.assign({ 'content-type': 'application/json' }, opts.headers || {}) }))
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { if (!r.ok) { var d = b.detail; throw Error(Array.isArray(d) ? d.map(function (x) { return x.msg; }).join('; ') : d || 'Request failed'); } return b; }); });
  }

  function mountMenu() {
    var host = document.getElementById('impilo-user-menu');
    if (!host || host.querySelector('.um-btn')) return;
    var u = session.user;
    host.innerHTML = '<button class="um-btn" aria-haspopup="menu" aria-expanded="false"><span class="um-av">' + esc(initials(u.name)) + '</span>' + esc(u.name.split(' ')[0]) + '</button>';
    var btn = host.querySelector('.um-btn');
    btn.addEventListener('click', function (e) { e.stopPropagation(); togglePop(host, btn); });
  }
  function togglePop(host, btn) {
    var old = host.querySelector('.um-pop'); if (old) { old.remove(); btn.setAttribute('aria-expanded', 'false'); return; }
    var u = session.user, pop = document.createElement('div');
    pop.className = 'um-pop'; pop.setAttribute('role', 'menu');
    pop.innerHTML = '<div class="who"><b>' + esc(u.name) + '</b>' + esc(u.username) + ' · ' + (u.role === 'admin' ? 'Administrator' : 'Standard user') + '</div>' +
      '<button role="menuitem" data-a="pw">Change password</button>' + (u.role === 'admin' ? '<button role="menuitem" data-a="users">Users &amp; access</button>' : '') +
      '<button role="menuitem" class="out" data-a="out">Sign out</button>';
    host.appendChild(pop); btn.setAttribute('aria-expanded', 'true');
    pop.addEventListener('click', function (e) {
      var a = e.target.getAttribute('data-a'); if (!a) return; pop.remove();
      if (a === 'out') window.ImpiloAuth.signOut(); else if (a === 'pw') passwordDialog(); else openSection('Users & access');
    });
  }
  document.addEventListener('click', function (e) { var p = document.querySelector('.um-pop'); if (p && !e.target.closest('#impilo-user-menu')) p.remove(); });

  function modal(html) {
    var m = document.createElement('div'); m.className = 'um-modal'; m.innerHTML = '<div class="um-card" role="dialog" aria-modal="true">' + html + '</div>';
    m.addEventListener('click', function (e) { if (e.target === m) m.remove(); });
    document.addEventListener('keydown', function k(e) { if (e.key === 'Escape') { m.remove(); document.removeEventListener('keydown', k); } });
    document.body.appendChild(m); var f = m.querySelector('input'); if (f) f.focus(); return m;
  }
  function passwordDialog() {
    var m = modal('<h2>Change password</h2><div class="sub">At least 10 characters, with upper- and lower-case letters and a number. Other sessions are signed out.</div>' +
      '<label>Current password</label><input type="password" id="pwCur" autocomplete="current-password"><label>New password</label><input type="password" id="pwNew" autocomplete="new-password">' +
      '<label>Repeat new password</label><input type="password" id="pwNew2" autocomplete="new-password"><div class="um-msg" id="pwMsg"></div><div class="um-row"><button class="um-b g" data-x>Cancel</button><button class="um-b" id="pwSave">Change password</button></div>');
    m.querySelector('[data-x]').onclick = function () { m.remove(); };
    m.querySelector('#pwSave').onclick = function () {
      var msg = m.querySelector('#pwMsg'), n = m.querySelector('#pwNew').value;
      if (n !== m.querySelector('#pwNew2').value) { msg.className = 'um-msg bad'; msg.textContent = 'The new passwords don\'t match.'; return; }
      api('/api/auth/password', { method: 'POST', body: JSON.stringify({ current: m.querySelector('#pwCur').value, new: n }) })
        .then(function (r) { window.ImpiloAuth.save({ token: r.token, expires: r.expires, user: r.user }); msg.className = 'um-msg ok'; msg.textContent = 'Password changed.'; setTimeout(function () { m.remove(); }, 1200); })
        .catch(function (e) { msg.className = 'um-msg bad'; msg.textContent = e.message; });
    };
  }
  function start() {
    mountMenu(); guardNav();
    new MutationObserver(function () { mountMenu(); guardNav(); }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-current'] });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
