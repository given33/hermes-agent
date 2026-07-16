"""Server-rendered /login page.

No React, no JavaScript dependency. Listed providers come from the
registry; clicking a provider sends a GET to
``/auth/login?provider=<name>``.

Visual styling mirrors the Nous Research design system (the
``@nous-research/ui`` package the React dashboard uses): the same
``Collapse`` / ``Rules Compressed`` typeface, amber-on-dark colour
tokens (``#170d02`` / ``#ffac02`` / ``#fff``), uppercase + wide-tracking
brand chrome, and the inset-bevel button shadow. Fonts are served
out of the SPA's ``/fonts/`` directory which the dashboard-auth gate
already allowlists pre-auth (see ``_GATE_PUBLIC_PREFIXES`` in
``middleware.py``), so the page renders without needing the React
bundle loaded.

Test-stable class names: the existing test suite extracts the
``class="provider-btn"`` anchor href to walk the OAuth flow. That
class name MUST NOT change without updating
``tests/hermes_cli/test_dashboard_auth_401_reauth.py``.
"""
from __future__ import annotations

import html

from hermes_cli.dashboard_auth import list_session_providers

# Inline minimal CSS. The dashboard's full skin lives in the React
# bundle, which we deliberately do NOT load here — the login page must
# not depend on the SPA build being present or on the injected session
# token.
#
# Single curly braces are placeholders for ``str.format``; CSS curlies
# are doubled (``{{`` / ``}}``).
_LOGIN_HTML_TEMPLATE = """\
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#170d02">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Hermes Agent">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<title>登录 — Hermes Agent</title>
<style>
  /* Brand fonts shipped by @nous-research/ui — same files the SPA loads. */
  @font-face {{
    font-family: 'Collapse';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/Collapse-Regular.woff2') format('woff2');
  }}
  @font-face {{
    font-family: 'Collapse';
    font-style: normal;
    font-weight: 700;
    font-display: swap;
    src: url('/fonts/Collapse-Bold.woff2') format('woff2');
  }}
  @font-face {{
    font-family: 'Rules Compressed';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/RulesCompressed-Regular.woff2') format('woff2');
  }}
  @font-face {{
    font-family: 'Rules Compressed';
    font-style: normal;
    font-weight: 600;
    font-display: swap;
    src: url('/fonts/RulesCompressed-Medium.woff2') format('woff2');
  }}

  :root {{
    --background-base: #170d02;
    --background: #170d02;
    --midground: #ffac02;
    --foreground: #ffffff;
    --hairline: color-mix(in srgb, #ffac02 18%, transparent);
    --hairline-strong: color-mix(in srgb, #ffac02 35%, transparent);
  }}

  *, *::before, *::after {{ box-sizing: border-box; }}

  html, body {{
    margin: 0;
    padding: 0;
    min-height: 100%;
    background: var(--background-base);
    color: var(--foreground);
    font-family: 'Collapse', system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 16px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }}

  /* Subtle dot-grid backdrop — DS idiom (see `.dither` in globals.css). */
  body {{
    background-image:
      radial-gradient(
        ellipse at top,
        color-mix(in srgb, var(--midground) 6%, transparent) 0%,
        transparent 55%
      ),
      repeating-conic-gradient(
        color-mix(in srgb, var(--midground) 4%, transparent) 0% 25%,
        transparent 0% 50%
      );
    background-size: auto, 3px 3px;
    background-attachment: fixed;
  }}

  /* Layout: vertically center on tall screens, top-anchor on short. */
  body {{
    display: grid;
    place-items: center;
    padding: clamp(1.5rem, 6vh, 6rem) 1.25rem;
  }}

  main {{
    width: 100%;
    max-width: 26rem;
    position: relative;
    animation: slide-up 0.6s ease-out both;
  }}

  @keyframes slide-up {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    main {{ animation: none; }}
  }}

  /* Brand wordmark above the card — same uppercase + wide-tracking
     idiom DS Buttons use. */
  .brand {{
    text-align: center;
    margin-bottom: 1.75rem;
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600;
    font-size: 1.05rem;
    letter-spacing: 0.32em;
    text-transform: uppercase;
    color: var(--midground);
  }}
  .brand .dot {{
    display: inline-block;
    width: 6px;
    height: 6px;
    background: var(--midground);
    margin: 0 0.55em 0.18em;
    vertical-align: middle;
    border-radius: 1px;
  }}

  .card {{
    position: relative;
    padding: 2.25rem 2rem 2rem;
    background: color-mix(in srgb, #ffffff 2%, var(--background-base));
    border: 1px solid var(--hairline);
    /* Hairline highlight + bevel shadow — matches DS Button SHADOW_DEFAULT
       (`inset -1px -1px 0 #00000080, inset 1px 1px 0 #ffffff80`) at panel scale. */
    box-shadow:
      inset 1px 1px 0 0 color-mix(in srgb, #ffffff 5%, transparent),
      inset -1px -1px 0 0 rgba(0, 0, 0, 0.4),
      0 24px 60px -20px rgba(0, 0, 0, 0.6);
  }}

  h1 {{
    margin: 0 0 0.4rem;
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600;
    font-size: 1.85rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--foreground);
  }}

  .subtitle {{
    margin: 0 0 1.75rem;
    color: color-mix(in srgb, var(--foreground) 65%, transparent);
    font-size: 0.95rem;
  }}

  .provider-list {{
    display: grid;
    gap: 0.75rem;
  }}

  /* Provider button — mirrors DS Button (default variant):
     amber surface, dark text, uppercase + wide tracking, inset bevel. */
  .provider-btn {{
    display: block;
    width: 100%;
    box-sizing: border-box;
    padding: 0.95rem 1rem;
    text-align: center;
    background: var(--midground);
    color: var(--background-base);
    font-family: 'Collapse', sans-serif;
    font-weight: 700;
    font-size: 0.78rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    text-decoration: none;
    border: 0;
    border-radius: 0;  /* DS Button is squared — no rounded corners. */
    cursor: pointer;
    box-shadow:
      inset 1px 1px 0 0 rgba(255, 255, 255, 0.5),
      inset -1px -1px 0 0 rgba(0, 0, 0, 0.5);
    transition: filter 0.12s ease-out;
  }}
  .provider-btn:hover {{
    filter: brightness(1.08);
  }}
  .provider-btn:active {{
    /* DS Button uses `active:invert` on the default surface. */
    filter: invert(1);
  }}
  .provider-btn:focus-visible {{
    outline: 2px solid var(--midground);
    outline-offset: 3px;
  }}

  /* Password provider form — same visual language as the OAuth buttons:
     squared inputs, hairline borders, amber focus ring. */
  .provider-form {{
    display: grid;
    gap: 0.75rem;
    text-align: left;
  }}
  .form-title {{
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600;
    font-size: 0.72rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: color-mix(in srgb, var(--foreground) 70%, transparent);
  }}
  .field {{
    display: grid;
    gap: 0.3rem;
  }}
  .field-label {{
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: color-mix(in srgb, var(--foreground) 55%, transparent);
  }}
  .field-input {{
    width: 100%;
    box-sizing: border-box;
    padding: 0.7rem 0.8rem;
    background: color-mix(in srgb, #000000 25%, var(--background-base));
    color: var(--foreground);
    border: 1px solid var(--hairline-strong);
    border-radius: 0;
    font-family: 'Collapse', sans-serif;
    font-size: 0.95rem;
  }}
  .field-input:focus-visible {{
    outline: none;
    border-color: var(--midground);
    box-shadow: 0 0 0 1px var(--midground);
  }}
  .form-error {{
    color: #ff6b6b;
    font-size: 0.82rem;
    letter-spacing: 0.02em;
  }}
  .provider-form .provider-btn {{
    margin-top: 0.25rem;
  }}

  footer {{
    margin-top: 1.75rem;
    text-align: center;
    color: color-mix(in srgb, var(--foreground) 45%, transparent);
    font-size: 0.75rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    line-height: 1.7;
  }}
  footer .sep {{
    display: inline-block;
    width: 1.5rem;
    height: 1px;
    background: var(--hairline-strong);
    vertical-align: middle;
    margin: 0 0.6em 0.2em;
  }}

  /* Selection — DS uses midground bg + background text. */
  ::selection {{
    background: var(--midground);
    color: var(--background-base);
  }}
</style>
</head>
<body>
<main>
  <div class="brand">Nous<span class="dot"></span>Research</div>
  <div class="card">
    <h1>{page_heading}</h1>
    <p class="subtitle">{page_subtitle}</p>
    <div class="provider-list">
{provider_buttons}
    </div>
  </div>
  <footer>
    <span class="sep"></span>公网访问 &middot; 需要身份验证<span class="sep"></span>
  </footer>
</main>
{password_script}
</body>
</html>
"""

_EMPTY_HTML = """\
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>暂时无法登录 — Hermes Agent</title>
<style>
  @font-face {
    font-family: 'Collapse';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/Collapse-Regular.woff2') format('woff2');
  }
  @font-face {
    font-family: 'Rules Compressed';
    font-style: normal;
    font-weight: 600;
    font-display: swap;
    src: url('/fonts/RulesCompressed-Medium.woff2') format('woff2');
  }
  :root {
    --background-base: #170d02;
    --midground: #ffac02;
    --foreground: #ffffff;
    --hairline: color-mix(in srgb, #ffac02 18%, transparent);
  }
  *, *::before, *::after { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; min-height: 100%;
    background: var(--background-base);
    color: var(--foreground);
    font-family: 'Collapse', system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 16px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  body {
    display: grid; place-items: center;
    padding: clamp(1.5rem, 6vh, 6rem) 1.25rem;
  }
  main {
    width: 100%; max-width: 32rem;
    padding: 2.25rem 2rem;
    background: color-mix(in srgb, #ffffff 2%, var(--background-base));
    border: 1px solid var(--hairline);
    box-shadow:
      inset 1px 1px 0 0 color-mix(in srgb, #ffffff 5%, transparent),
      inset -1px -1px 0 0 rgba(0, 0, 0, 0.4),
      0 24px 60px -20px rgba(0, 0, 0, 0.6);
  }
  h1 {
    margin: 0 0 1rem;
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600; font-size: 1.5rem;
    letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--midground);
  }
  p { margin: 0 0 1rem; }
  code {
    background: var(--midground);
    color: var(--background-base);
    padding: 0.1em 0.35em;
    font-family: 'Courier New', monospace;
    font-size: 0.9em;
  }
</style>
</head>
<body>
<main>
<h1>暂时无法登录</h1>
<p>此管理面板已开放到非本机地址，但尚未安装身份验证提供程序。</p>
<p>请安装默认的 <code>plugins/dashboard-auth-nous</code> 或其他身份验证插件。
不建议在公网环境使用 <code>--insecure</code> 绕过登录验证。</p>
</main>
</body>
</html>
"""


# Inline script that wires every password provider form to POST JSON to
# ``/auth/password-login`` and navigate on success. Emitted ONLY when at
# least one ``supports_password`` provider is listed (OAuth-only login
# pages stay script-free, preserving the no-JS contract for that case).
#
# Plain string (NOT run through ``str.format``), so braces are literal —
# do not double them. A single delegated submit handler covers all forms;
# the provider name is read from the form's ``data-provider`` attribute.
_PASSWORD_FORM_SCRIPT = """\
<script>
(function () {
  function handle(form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var err = form.querySelector('.form-error');
      var btn = form.querySelector('button[type=submit]');
      if (err) { err.hidden = true; err.textContent = ''; }
      if (btn) { btn.disabled = true; }
      var body = {
        provider: form.getAttribute('data-provider') || '',
        username: (form.querySelector('input[name=username]') || {}).value || '',
        password: (form.querySelector('input[name=password]') || {}).value || '',
        next: (form.querySelector('input[name=next]') || {}).value || ''
      };
      fetch('/auth/password-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin'
      }).then(function (resp) {
        if (resp.ok) {
          return resp.json().then(function (data) {
            window.location.assign((data && data.next) || '/');
          });
        }
        var msg = resp.status === 429
          ? '尝试次数过多，请稍后再试。'
          : (resp.status === 401 ? '用户名或密码错误。'
                                 : '登录失败，请重试。');
        if (err) { err.textContent = msg; err.hidden = false; }
        if (btn) { btn.disabled = false; }
      }).catch(function () {
        if (err) { err.textContent = '网络连接失败，请重试。'; err.hidden = false; }
        if (btn) { btn.disabled = false; }
      });
    });
  }
  var forms = document.querySelectorAll('form.provider-form');
  for (var i = 0; i < forms.length; i++) { handle(forms[i]); }
})();
</script>
"""

_OWNER_REGISTRATION_SCRIPT = """\
<script>
(function () {
  var form = document.querySelector('form.owner-registration-form');
  if (!form) { return; }
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var err = form.querySelector('.form-error');
    var btn = form.querySelector('button[type=submit]');
    var username = (form.querySelector('input[name=username]') || {}).value || '';
    var password = (form.querySelector('input[name=password]') || {}).value || '';
    var confirmation = (form.querySelector('input[name=confirmation]') || {}).value || '';
    var setupToken = (form.querySelector('input[name=setup_token]') || {}).value || '';
    var next = (form.querySelector('input[name=next]') || {}).value || '';
    if (err) { err.hidden = true; err.textContent = ''; }
    if (password !== confirmation) {
      if (err) { err.textContent = '两次输入的密码不一致。'; err.hidden = false; }
      return;
    }
    if (btn) { btn.disabled = true; }
    fetch('/auth/mobile/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: username,
        password: password,
        setup_token: setupToken
      }),
      credentials: 'same-origin'
    }).then(function (resp) {
      if (!resp.ok) {
        var message = resp.status === 403
          ? '初始化码错误。'
          : (resp.status === 409
              ? '所有者账号已经存在。'
              : '注册失败，请检查账号和密码。');
        throw new Error(message);
      }
      return fetch('/auth/password-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: 'basic', username: username, password: password, next: next
        }),
        credentials: 'same-origin'
      });
    }).then(function (resp) {
      if (!resp.ok) { throw new Error('账号已创建，但登录失败，请重新登录。'); }
      return resp.json();
    }).then(function (data) {
      window.location.assign((data && data.next) || '/');
    }).catch(function (error) {
      if (err) { err.textContent = error.message || '注册失败，请重试。'; err.hidden = false; }
      if (btn) { btn.disabled = false; }
    });
  });
})();
</script>
"""


def render_login_html(*, next_path: str = "") -> str:
    """Return the full HTML for ``GET /login``.

    ``next_path`` — when set, the post-login landing path the user
    originally requested. Threaded into each provider button's ``href``
    as a ``next=`` query parameter so the OAuth round trip carries it
    end-to-end. The caller (``routes.login_page``) is responsible for
    validating ``next_path`` against the same-origin rules before we
    emit it; we still HTML-escape it as defence in depth.
    """
    providers = list_session_providers()
    if not providers:
        from hermes_cli.dashboard_auth.owner_mobile import (
            owner_registration_open,
            owner_setup_token_required,
        )

        if owner_registration_open():
            return _LOGIN_HTML_TEMPLATE.format(
                page_heading="注册",
                page_subtitle="创建此 Hermes 服务器的所有者账号。",
                provider_buttons=_render_owner_registration_form(
                    next_path,
                    setup_token_required=owner_setup_token_required(),
                ),
                password_script=_OWNER_REGISTRATION_SCRIPT,
            )
        return _EMPTY_HTML

    if next_path:
        # URL-encode then HTML-escape. The URL-encode step matches the
        # gate's ``_safe_next_target`` output shape (also URL-encoded),
        # so a value that round-tripped from /login?next=... back into
        # the button href is byte-identical.
        from urllib.parse import quote
        next_qs = f"&next={html.escape(quote(next_path, safe=''), quote=True)}"
    else:
        next_qs = ""

    buttons = []
    needs_password_script = False
    for p in providers:
        if getattr(p, "supports_password", False):
            needs_password_script = True
            buttons.append(_render_password_form(p, next_path))
        else:
            buttons.append(
                f'      <a class="provider-btn" '
                f'href="/auth/login?provider={html.escape(p.name, quote=True)}{next_qs}">'
                f'使用 {html.escape(p.display_name)} 登录</a>'
            )
    script = _PASSWORD_FORM_SCRIPT if needs_password_script else ""
    return _LOGIN_HTML_TEMPLATE.format(
        page_heading="登录",
        page_subtitle="登录后继续使用 Hermes Agent 管理面板。",
        provider_buttons="\n".join(buttons),
        password_script=script,
    )


def _render_owner_registration_form(
    next_path: str,
    *,
    setup_token_required: bool,
) -> str:
    safe_next = html.escape(next_path, quote=True) if next_path else ""
    setup_token_field = ""
    if setup_token_required:
        setup_token_field = (
            '        <label class="field">\n'
            '          <span class="field-label">服务器初始化码</span>\n'
            '          <input class="field-input" type="password" name="setup_token" '
            'autocomplete="off" autocapitalize="none" autocorrect="off" '
            'spellcheck="false" required>\n'
            '        </label>\n'
        )
    return (
        '      <form class="provider-form owner-registration-form" autocomplete="on">\n'
        f'        <input type="hidden" name="next" value="{safe_next}">\n'
        f'{setup_token_field}'
        '        <label class="field">\n'
        '          <span class="field-label">账号</span>\n'
        '          <input class="field-input" type="text" name="username" '
        'autocomplete="username" autocapitalize="none" autocorrect="off" '
        'spellcheck="false" required>\n'
        '        </label>\n'
        '        <label class="field">\n'
        '          <span class="field-label">密码</span>\n'
        '          <input class="field-input" type="password" name="password" '
        'autocomplete="new-password" minlength="8" required>\n'
        '        </label>\n'
        '        <label class="field">\n'
        '          <span class="field-label">确认密码</span>\n'
        '          <input class="field-input" type="password" name="confirmation" '
        'autocomplete="new-password" minlength="8" required>\n'
        '        </label>\n'
        '        <div class="form-error" role="alert" hidden></div>\n'
        '        <button class="provider-btn" type="submit">注册并登录</button>\n'
        '      </form>'
    )


def _render_password_form(provider, next_path: str) -> str:
    """Render a username/password form for a ``supports_password`` provider.

    The form is wired by :data:`_PASSWORD_FORM_SCRIPT` (a single delegated
    submit handler) to POST JSON to ``/auth/password-login`` and navigate
    on success. ``next_path`` is carried in a hidden field; it has already
    been validated same-origin by the caller and is HTML-escaped here as
    defence in depth. The provider ``name`` is emitted in a ``data-``
    attribute (not a hidden input) so the script reads it without trusting
    form-field ordering.
    """
    pname = html.escape(provider.name, quote=True)
    plabel = html.escape(provider.display_name)
    safe_next = html.escape(next_path, quote=True) if next_path else ""
    return (
        f'      <form class="provider-form" data-provider="{pname}" '
        f'autocomplete="on">\n'
        f'        <div class="form-title">使用 {plabel} 登录</div>\n'
        f'        <input type="hidden" name="next" value="{safe_next}">\n'
        f'        <label class="field">\n'
        f'          <span class="field-label">用户名</span>\n'
        f'          <input class="field-input" type="text" name="username" '
        f'autocomplete="username" autocapitalize="none" '
        f'autocorrect="off" spellcheck="false" required>\n'
        f'        </label>\n'
        f'        <label class="field">\n'
        f'          <span class="field-label">密码</span>\n'
        f'          <input class="field-input" type="password" name="password" '
        f'autocomplete="current-password" required>\n'
        f'        </label>\n'
        f'        <div class="form-error" role="alert" hidden></div>\n'
        f'        <button class="provider-btn" type="submit">登录</button>\n'
        f'      </form>'
    )
