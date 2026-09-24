"""accounts.py — bring-your-own-credentials sessions.

Lets someone connect their own Vobiz account in the dashboard and place calls
from a number they own, instead of the account in .env.

HOW CREDENTIALS ARE HANDLED, because this module holds other people's secrets:

  - Memory only. Nothing is written to disk, and nothing reaches events.jsonl
    or the event feed. The browser is handed an opaque session id, never the
    token back again.
  - Sessions expire (SESSION_TTL) and are capped (MAX_SESSIONS), so a long
    running demo server does not accumulate credentials indefinitely.
  - __repr__ is overridden on the session so a stray log line or traceback
    cannot print the token.

The dashboard is typically exposed on a public tunnel. Anyone with that URL can
reach the connect endpoint, so this is a demo affordance, not an auth system.
"""

import secrets
import time

import aiohttp

VOBIZ_API = "https://api.vobiz.ai/api/v1"
SESSION_TTL = 60 * 60 * 8        # 8 hours, comfortably longer than a demo
MAX_SESSIONS = 50


class Account:
    """One connected Vobiz account. Never log this object."""

    __slots__ = ("auth_id", "_token", "numbers", "created_at")

    def __init__(self, auth_id: str, token: str, numbers: list):
        self.auth_id = auth_id
        self._token = token
        self.numbers = numbers
        self.created_at = time.time()

    @property
    def token(self) -> str:
        return self._token

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL

    def __repr__(self):                      # keeps the token out of tracebacks
        return f"<Account {self.auth_id} numbers={len(self.numbers)}>"

    __str__ = __repr__


_sessions: dict[str, Account] = {}


def _reap() -> None:
    for sid in [s for s, a in _sessions.items() if a.expired]:
        _sessions.pop(sid, None)
    # Oldest-first eviction if somehow still over the cap.
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=lambda s: _sessions[s].created_at)
        _sessions.pop(oldest, None)


async def fetch_numbers(session: aiohttp.ClientSession, auth_id: str, token: str) -> list:
    """List the voice-capable numbers on an account.

    Doubles as credential validation: a 401 here means the pair is wrong.
    Note the path is lowercase `numbers` — `/Number/` returns 401 regardless of
    whether the credentials are valid, which reads as an auth failure and is not.
    """
    url = f"{VOBIZ_API}/Account/{auth_id}/numbers"
    headers = {"X-Auth-ID": auth_id, "X-Auth-Token": token,
               "Content-Type": "application/json"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
        body = await r.text()
        if r.status in (401, 403):
            raise PermissionError(
                f"Vobiz rejected these credentials ({r.status}). Check the Auth ID "
                "and Auth Token are from the same account and copied in full.")
        if r.status == 404:
            raise ValueError(
                f"Vobiz does not recognise the Auth ID {auth_id!r} (404).")
        if r.status != 200:
            raise RuntimeError(f"Vobiz returned {r.status}: {body[:200]}")
        import json
        data = json.loads(body)

    return [
        {"e164": n.get("e164"), "country": n.get("country"), "status": n.get("status")}
        for n in data.get("items", [])
        if n.get("voice_enabled") and n.get("status") == "active" and n.get("e164")
    ]


async def connect(http: aiohttp.ClientSession, auth_id: str, token: str) -> tuple[str, Account]:
    """Validate a credential pair and open a session. Raises on bad credentials."""
    auth_id = (auth_id or "").strip()
    token = (token or "").strip()
    if not auth_id or not token:
        raise ValueError("Both Auth ID and Auth Token are required")

    numbers = await fetch_numbers(http, auth_id, token)
    if not numbers:
        # ValueError, not RuntimeError: this is a problem with the account, not
        # with reaching Vobiz, and the caller maps RuntimeError to "could not
        # reach Vobiz" — which would send someone debugging their network.
        raise ValueError(
            f"Credentials for {auth_id} are valid, but the account has no active "
            "voice-enabled number to call from. Buy or activate a number first."
        )

    _reap()
    session_id = secrets.token_urlsafe(24)
    _sessions[session_id] = Account(auth_id, token, numbers)
    return session_id, _sessions[session_id]


def get(session_id: str) -> Account | None:
    """Return a live session, or None if unknown or expired."""
    if not session_id:
        return None
    account = _sessions.get(session_id)
    if account is None:
        return None
    if account.expired:
        _sessions.pop(session_id, None)
        return None
    return account


def disconnect(session_id: str) -> bool:
    return _sessions.pop(session_id, None) is not None


class SessionExpired(Exception):
    """A session id was supplied but is unknown or expired."""


def credentials(session_id: str, fallback_id: str, fallback_token: str):
    """Resolve which credentials a request should use.

    No session at all -> the server's own .env account.
    A valid session    -> that account.
    A session that was supplied but is dead -> raise.

    That last case must NOT fall back silently. The caller believes they are on
    their own account; quietly dialling from the server's account instead would
    bill the wrong party and place a call they did not authorise.

    Returns (auth_id, token, source); the caller logs the source, never the token.
    """
    if not session_id:
        return fallback_id, fallback_token, "env"
    account = get(session_id)
    if account is None:
        raise SessionExpired(
            "Your account session has expired. Reconnect your Vobiz account.")
    return account.auth_id, account.token, "session"
