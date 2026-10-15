/* Cookie notice (October 2026).
 *
 * SpotApply stores only what it needs to work: the sign-in session (a cookie
 * and the browser's localStorage) and a few settings you choose, such as a
 * collapsed sidebar. There are no analytics, advertising or tracking cookies,
 * so there is nothing optional to accept or refuse — offering an
 * "Accept / Reject" choice that changed nothing would itself mislead. This
 * notice says what is stored, links to /cookies, and remembers that it was
 * dismissed (that one flag is stored the same way). If an optional cookie is
 * ever added, it needs a real opt-in here BEFORE it is set.
 */
(function () {
  var KEY = 'sa_cookie_notice_v1';
  try { if (localStorage.getItem(KEY)) return; } catch (e) { /* storage blocked: still show */ }

  function show() {
    if (document.getElementById('sa-cookie-notice')) return;
    var box = document.createElement('div');
    box.id = 'sa-cookie-notice';
    box.setAttribute('role', 'region');
    box.setAttribute('aria-label', 'Cookie notice');
    box.style.cssText = [
      'position:fixed', 'left:16px', 'right:16px', 'bottom:16px', 'z-index:2147483000',
      'max-width:560px', 'margin:0 auto', 'background:#0C2A3E', 'color:#F5FAFF',
      'border-radius:14px', 'padding:14px 16px', 'box-shadow:0 10px 30px rgba(12,42,62,.35)',
      'font:14px/1.5 Outfit, system-ui, -apple-system, Segoe UI, sans-serif',
      'display:flex', 'flex-wrap:wrap', 'align-items:center', 'gap:10px 14px'
    ].join(';');

    var text = document.createElement('p');
    text.style.cssText = 'margin:0;flex:1 1 260px;color:#F5FAFF;';
    text.appendChild(document.createTextNode(
      'We use only essential cookies and browser storage to keep you signed in and remember your settings. No tracking or advertising cookies. '));
    var link = document.createElement('a');
    link.href = '/cookies';
    link.textContent = 'Cookie Policy';
    link.style.cssText = 'color:#9FD8FF;text-decoration:underline;';
    text.appendChild(link);

    var ok = document.createElement('button');
    ok.type = 'button';
    ok.textContent = 'OK';
    ok.setAttribute('aria-label', 'Dismiss cookie notice');
    ok.style.cssText = 'background:#F5FAFF;color:#0C2A3E;border:0;border-radius:10px;padding:8px 18px;font:700 14px Outfit, system-ui, sans-serif;cursor:pointer;';
    ok.addEventListener('click', function () {
      try { localStorage.setItem(KEY, String(Date.now())); } catch (e) {}
      box.remove();
    });
    ok.addEventListener('focus', function () { ok.style.outline = '3px solid #9FD8FF'; ok.style.outlineOffset = '2px'; });
    ok.addEventListener('blur', function () { ok.style.outline = ''; });

    box.appendChild(text);
    box.appendChild(ok);
    document.body.appendChild(box);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show);
  else show();
})();
