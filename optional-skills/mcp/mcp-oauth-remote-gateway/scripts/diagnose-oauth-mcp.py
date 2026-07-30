#!/usr/bin/env python3
"""Diagnose an OAuth-gated remote MCP server's connection state.

Decides which recovery branch you're in WITHOUT mutating disk by default:
  1. Smoke-test the stored access_token against the MCP endpoint (server/discover).
  2. If 401, attempt a refresh and smoke-test the freshly-minted token.

Branches printed at the end:
  TOKEN_OK            -> stored token works; "not connected" is the circuit
                         breaker (SKILL pitfall 7) -> restart the gateway.
  REFRESH_FIXED       -> refresh minted a working token; pass --write to persist
                         it (atomic, 0600), then restart to clear the breaker.
  SESSION_REVOKED     -> refresh succeeds but the new token still receives the
                         MCP -32002 session-expired error; full re-auth required.
  REFRESH_DEAD        -> refresh grant itself returns invalid_grant (pitfall 9)
                         -> full interactive re-auth, or switch to a static API key.

Usage:
  python3 diagnose-oauth-mcp.py <server_name> [--mcp-url URL] [--token-endpoint URL] [--write]

  <server_name> matches the files in $HERMES_HOME/mcp-tokens/<server>.json etc.
  If --mcp-url / --token-endpoint are omitted, they're read from the token's
  `resource` field and the AS .well-known metadata respectively.

NEVER prints secret values — only lengths, scope, expiry, and HTTP status.
"""
import json, os, sys, time, argparse, urllib.request, urllib.error, urllib.parse

UA = "python-httpx/0.27"  # CF blocks default urllib UA on many providers


def _hermes_home():
    # Prefer Hermes' own resolver (profile-safe); fall back to env then ~/.hermes.
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _tokens_dir():
    return os.path.join(_hermes_home(), "mcp-tokens")


def _post(url, data=None, headers=None, form=False, timeout=30):
    if form:
        body = urllib.parse.urlencode(data).encode()
    else:
        body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    req.add_header("User-Agent", UA)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _mcp_discover(mcp_url, access_token):
    status, _hdrs, body = _post(
        mcp_url,
        data={
            "jsonrpc": "2.0", "id": 1, "method": "server/discover",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "hermes-diag", "version": "2.0"
                },
            }},
        },
        headers={
            "Authorization": "Bearer " + access_token,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "server/discover",
        },
    )
    decoded = body.decode(errors="replace")
    txt = decoded[:400]
    ok = status == 200 and ("supportedVersions" in txt or '"result"' in txt)
    session_revoked = False
    try:
        payload = json.loads(decoded)
        error = payload.get("error") if isinstance(payload, dict) else None
        session_revoked = isinstance(error, dict) and error.get("code") == -32002
    except (TypeError, ValueError):
        pass
    return ok, session_revoked, status, txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--mcp-url")
    ap.add_argument("--token-endpoint")
    ap.add_argument("--write", action="store_true", help="persist a working refreshed token")
    args = ap.parse_args()

    tdir = _tokens_dir()
    tpath = os.path.join(tdir, args.server + ".json")
    cpath = os.path.join(tdir, args.server + ".client.json")
    tok = json.load(open(tpath))
    client = json.load(open(cpath))

    mcp_url = args.mcp_url or tok.get("resource")
    if not mcp_url:
        print("FATAL: no --mcp-url and no `resource` in token file"); sys.exit(2)
    resource = tok.get("resource") or mcp_url

    print(f"server={args.server} mcp_url={mcp_url}")
    exp = tok.get("expires_at")
    if isinstance(exp, (int, float)):
        print(f"stored expires_at in {round((exp - time.time())/60)} min")

    # Step 1: stored token
    ok, _session_revoked, status, txt = _mcp_discover(mcp_url, tok["access_token"])
    print(f"[1] stored-token server/discover -> HTTP {status} ok={ok}")
    if ok:
        print("BRANCH=TOKEN_OK  -> stored token works; 'not connected' is the breaker (7). Restart the gateway.")
        return

    # Step 2: refresh
    if not tok.get("refresh_token"):
        print("BRANCH=REFRESH_DEAD  -> no refresh_token present; full re-auth required.")
        return
    token_ep = args.token_endpoint
    if not token_ep:
        # derive AS metadata from the host root
        host = urllib.parse.urlsplit(mcp_url)
        as_url = f"{host.scheme}://{host.netloc}/.well-known/oauth-authorization-server"
        try:
            token_ep = _get_json(as_url)["token_endpoint"]
        except Exception as e:
            print(f"FATAL: cannot discover token_endpoint ({e}); pass --token-endpoint"); sys.exit(2)
    print(f"[2] token_endpoint={token_ep}")

    rstatus, _rh, rbody = _post(
        token_ep, form=True,
        data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
              "client_id": client["client_id"], "resource": resource},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if rstatus != 200:
        print(f"[2] refresh -> HTTP {rstatus} {rbody[:200].decode(errors='replace')}")
        print("BRANCH=REFRESH_DEAD  -> refresh grant rejected (9). Full re-auth or switch to static API key.")
        return
    j = json.loads(rbody)
    new_at = j["access_token"]
    print(f"[2] refresh -> HTTP 200 new_token_len={len(new_at)} scope={j.get('scope')} "
          f"expires_in={j.get('expires_in')} rotated_refresh={bool(j.get('refresh_token'))}")

    # Step 2b: smoke-test the freshly-minted token
    ok2, session_revoked2, status2, txt2 = _mcp_discover(mcp_url, new_at)
    print(f"[2b] new-token server/discover -> HTTP {status2} ok={ok2}")
    if ok2:
        if args.write:
            new = dict(tok)
            new.update({"access_token": new_at, "token_type": j.get("token_type", "Bearer"),
                        "expires_in": j.get("expires_in", tok.get("expires_in")),
                        "scope": j.get("scope", tok.get("scope")),
                        "expires_at": time.time() + float(j.get("expires_in", 3600))})
            if j.get("refresh_token"):
                new["refresh_token"] = j["refresh_token"]
            tmp = tpath + ".tmp"
            open(tmp, "w").write(json.dumps(new, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, tpath)
            print(f"     wrote {tpath} (0600). NOW RESTART the gateway to clear the breaker.")
        print("BRANCH=REFRESH_FIXED  -> refreshed token works. Persist (--write) + restart gateway.")
        return

    if session_revoked2:
        print("BRANCH=SESSION_REVOKED  -> refreshed token still has an expired MCP session.")
        print("     Refreshing again will not help. Full interactive re-auth is required.")
        return

    print(f"BRANCH=UNKNOWN  -> new token failed discovery: {txt2[:200]}")


if __name__ == "__main__":
    main()
