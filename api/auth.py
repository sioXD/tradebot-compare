"""Login nur mit Passwort (Cookie-Session) fuer das Dashboard.

nginx schickt jede Anfrage an /trade/ zuerst an /_auth. Ist das Cookie
gueltig, geht es weiter; sonst wird auf /login umgeleitet.

Das Passwort ist API_TOKEN aus .env – also dasselbe wie der Widget-Token.
Es gibt bewusst kein Benutzernamen-Feld.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

PASSWORD = os.getenv("API_TOKEN", "").strip()
COOKIE_NAME = "tb_auth"
SESSION_SECONDS = 30 * 24 * 3600  # 30 Tage, damit das iPad eingeloggt bleibt
MAX_ATTEMPTS = 10  # pro IP und Minute

# Cookie-Signatur leitet sich aus dem Passwort ab: Passwort aendern ->
# alle alten Sessions werden automatisch ungueltig.
SECRET = hashlib.sha256(f"tradebot-cookie:{PASSWORD}".encode()).digest()
_attempts: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

LOGIN_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login · Tradingbots</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0f1218; color: #e8edf5;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .card {
    width: min(360px, calc(100vw - 32px)); padding: 28px 24px;
    background: #171b24; border: 1px solid #262d3a; border-radius: 16px;
    box-shadow: 0 18px 50px rgba(0,0,0,.45);
  }
  h1 { margin: 0 0 4px; font-size: 20px; }
  p.sub { margin: 0 0 20px; color: #9aa4b2; font-size: 13px; }
  label { display: block; margin-bottom: 6px; font-size: 13px; color: #c3ccda; }
  input[type=password] {
    width: 100%; padding: 12px 14px; font-size: 16px;
    background: #0f1218; color: #fff; border: 1px solid #2c3444; border-radius: 10px;
  }
  input[type=password]:focus { outline: none; border-color: #4f8cff; }
  button {
    width: 100%; margin-top: 16px; padding: 12px 14px; font-size: 15px; font-weight: 600;
    background: #4f8cff; color: #fff; border: 0; border-radius: 10px; cursor: pointer;
  }
  button:active { transform: translateY(1px); }
  .error {
    margin: 0 0 16px; padding: 10px 12px; border-radius: 10px;
    background: #3a1d22; border: 1px solid #6b2b34; color: #ff9aa5; font-size: 13px;
  }
  .hint { margin: 14px 0 0; color: #6f7a8a; font-size: 11px; text-align: center; }
</style>
</head>
<body>
  <form class="card" method="post" action="/auth">
    <h1>Tradingbots</h1>
    <p class="sub">Bitte Passwort eingeben.</p>
    __ERROR__
    <label for="password">Passwort</label>
    <input id="password" name="password" type="password" required autofocus
           autocomplete="current-password" autocapitalize="off" autocorrect="off">
    <button type="submit">Anmelden</button>
    <p class="hint">Paper Trading · keine Anlageberatung</p>
  </form>
</body>
</html>
"""

ERROR_HTML = '<p class="error">Falsches Passwort. Bitte erneut versuchen.</p>'


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else "?")).strip()


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _attempts[ip]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= MAX_ATTEMPTS:
        return True
    bucket.append(now)
    return False


def _make_cookie() -> str:
    exp = int(time.time()) + SESSION_SECONDS
    sig = hmac.new(SECRET, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _cookie_valid(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp_s, sig = value.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    expected = hmac.new(SECRET, exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    error = ERROR_HTML if request.query_params.get("error") else ""
    return HTMLResponse(
        LOGIN_HTML.replace("__ERROR__", error),
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


@app.post("/auth")
async def do_login(request: Request) -> Response:
    if not PASSWORD:
        return HTMLResponse("Login ist nicht konfiguriert (API_TOKEN fehlt).", status_code=503)

    ip = _client_ip(request)
    if _rate_limited(ip):
        return HTMLResponse("Zu viele Versuche. Bitte eine Minute warten.", status_code=429)

    body = (await request.body()).decode("utf-8", "replace")
    password = parse_qs(body).get("password", [""])[0]

    if not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        return RedirectResponse("/login?error=1", status_code=303)

    resp = RedirectResponse("/trade/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        _make_cookie(),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/_auth")
def check_auth(request: Request) -> Response:
    if _cookie_valid(request.cookies.get(COOKIE_NAME)):
        return Response(status_code=200)
    return Response(status_code=401)


@app.get("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
