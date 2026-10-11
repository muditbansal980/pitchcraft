"""Canva Connect: OAuth (Authorization Code + PKCE), token storage and PPTX import.

Docs: https://www.canva.dev/docs/connect/authentication/ and the API reference for
``POST /v1/oauth/token``, ``POST /v1/oauth/revoke``, ``POST /v1/imports`` and
``GET /v1/imports/{jobId}``. Both import endpoints need the ``design:content:write``
scope.

- **Stateless ``state``.** The OAuth ``state`` is Fernet ciphertext of
  ``{user_id, code_verifier, exp}``, so no table is needed for pending logins.
  The browser carries it but can't read or forge it; the callback checks that it
  belongs to the logged-in user and hasn't expired.
- **Tokens at rest.** Access and refresh tokens are Fernet-encrypted with a key
  derived from ``settings.jwt_secret_key``. Rotating that secret logs users out
  and also disconnects Canva (old ciphertext can't be read, so it's dropped).
- **Refresh.** Access tokens last ~4 h. They are refreshed shortly before
  expiry, or after a 401. Canva refresh tokens are single-use, so refreshes are
  serialised per user.
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.canva_token import CanvaToken

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
API_BASE = "https://api.canva.com/rest/v1"
TOKEN_URL = f"{API_BASE}/oauth/token"
REVOKE_URL = f"{API_BASE}/oauth/revoke"
IMPORTS_URL = f"{API_BASE}/imports"
# Creating and polling an import job both need only this scope.
SCOPES = ("design:content:write",)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
STATE_TTL_SECONDS = 10 * 60
# Refresh this long before the access token's stated expiry.
EXPIRY_MARGIN = timedelta(seconds=60)
# Canva's maximum design title length, before base64 encoding.
MAX_TITLE_CHARS = 50
IMPORT_TIMEOUT_SECONDS = 60.0
POLL_DELAYS = (1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 5.0)  # then 5 s until the timeout

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# PPTX uploads can be large; allow longer for the upload itself.
UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# Tests set this to an ``httpx.MockTransport``.
_transport: httpx.AsyncBaseTransport | None = None

_refresh_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class CanvaError(Exception):
    """An error with the HTTP status and message the API route should return."""

    status_code = 502

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class CanvaNotConfiguredError(CanvaError):
    status_code = 503


class CanvaNotConnectedError(CanvaError):
    status_code = 409

    def __init__(self, detail: str = "Connect your Canva account first."):
        super().__init__(detail)


class CanvaThrottledError(CanvaError):
    status_code = 429

    def __init__(self, detail: str = "Canva is rate-limiting imports. Please try again in a minute."):
        super().__init__(detail)


# --- configuration and crypto -------------------------------------------------


def is_configured() -> bool:
    return bool(settings.canva_client_id and settings.canva_client_secret and settings.jwt_secret_key)


def require_configured() -> None:
    if not (settings.canva_client_id and settings.canva_client_secret):
        raise CanvaNotConfiguredError(
            "Canva isn't configured on the server: set CANVA_CLIENT_ID and CANVA_CLIENT_SECRET."
        )
    if not settings.jwt_secret_key:
        raise CanvaNotConfiguredError("JWT_SECRET_KEY is not configured")


def _fernet() -> Fernet:
    if not settings.jwt_secret_key:
        raise CanvaNotConfiguredError("JWT_SECRET_KEY is not configured")
    # Domain-separated so this key differs from anything else derived from the secret.
    digest = hashlib.sha256(b"pitchcraft:canva:" + settings.jwt_secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; they're stored as UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _http(timeout: httpx.Timeout = HTTP_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


def _basic_auth() -> str:
    raw = f"{settings.canva_client_id}:{settings.canva_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# --- PKCE and state -----------------------------------------------------------


def make_code_verifier() -> str:
    # 64 random bytes -> 86 URL-safe chars (Canva wants 43-128 of [A-Za-z0-9-._~]).
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_state(user_id: int, verifier: str, *, now: float | None = None) -> str:
    issued = time.time() if now is None else now
    payload = {
        "typ": "canva_oauth_state",
        "uid": user_id,
        "cv": verifier,
        "exp": int(issued) + STATE_TTL_SECONDS,
        # Makes every state unique even for the same verifier.
        "n": secrets.token_urlsafe(8),
    }
    return _fernet().encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode()


def parse_state(state: str, user_id: int, *, now: float | None = None) -> str:
    """Return the code verifier from ``state`` if it is valid, unexpired and ``user_id``'s."""
    invalid = CanvaError("The Canva sign-in link is invalid or has expired. Please try again.", 400)
    try:
        payload = json.loads(_fernet().decrypt(state.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise invalid from None
    if not isinstance(payload, dict) or payload.get("typ") != "canva_oauth_state":
        raise invalid
    if not isinstance(payload.get("exp"), int) or payload["exp"] < (time.time() if now is None else now):
        raise invalid
    if payload.get("uid") != user_id:
        raise CanvaError("This Canva sign-in was started by a different account.", 403)
    verifier = payload.get("cv")
    if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
        raise invalid
    return verifier


def build_authorize_url(user_id: int) -> str:
    require_configured()
    verifier = make_code_verifier()
    params = {
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "s256",
        "scope": " ".join(SCOPES),
        "response_type": "code",
        "client_id": settings.canva_client_id,
        "state": build_state(user_id, verifier),
        "redirect_uri": settings.canva_redirect_uri,
    }
    # The default quote_plus would turn the scope's space into "+"; Canva's docs use %20.
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


# --- token storage ------------------------------------------------------------


def _get_row(db: Session, user_id: int) -> CanvaToken | None:
    return db.scalar(select(CanvaToken).where(CanvaToken.user_id == user_id))


def is_connected(db: Session, user_id: int) -> bool:
    return _get_row(db, user_id) is not None


def _store_tokens(db: Session, user_id: int, data: dict[str, Any], row: CanvaToken | None = None) -> CanvaToken:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise CanvaError("Canva returned an unexpected token response.")
    expires_in = data.get("expires_in")
    lifetime = int(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else 4 * 3600
    values = {
        "access_token": _encrypt(access),
        "refresh_token": _encrypt(refresh),
        "expires_at": _now() + timedelta(seconds=lifetime),
        "scope": str(data.get("scope") or " ".join(SCOPES))[:500],
    }

    row = row or _get_row(db, user_id)
    if row is None:
        row = CanvaToken(user_id=user_id, **values)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent callback for the same user inserted first: update that row.
            db.rollback()
            row = _get_row(db, user_id)
            if row is None:
                raise
            return _store_tokens(db, user_id, data, row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()
    db.refresh(row)
    return row


def _drop_row(db: Session, row: CanvaToken) -> None:
    db.delete(row)
    db.commit()


def _canva_error(res: httpx.Response) -> tuple[str, str]:
    try:
        data = res.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return str(data.get("code") or data.get("error") or ""), str(
        data.get("message") or data.get("error_description") or ""
    )


async def _token_request(form: dict[str, str]) -> httpx.Response:
    try:
        async with _http() as client:
            return await client.post(
                TOKEN_URL,
                data=form,
                headers={"Authorization": _basic_auth(), "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Canva token request failed: %s", exc)
        raise CanvaError("Could not reach Canva. Please try again.") from exc


async def exchange_code(db: Session, user_id: int, code: str, state: str) -> None:
    """Finish the OAuth flow: check ``state``, swap the code for tokens and store them."""
    require_configured()
    verifier = parse_state(state, user_id)
    res = await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": settings.canva_redirect_uri,
        }
    )
    if res.status_code == 429:
        raise CanvaThrottledError("Canva is busy. Please try connecting again in a minute.")
    if res.status_code != 200:
        err, message = _canva_error(res)
        logger.warning("Canva code exchange failed: %s %s %s", res.status_code, err, message)
        if res.status_code == 400:
            raise CanvaError("Canva didn't accept the sign-in (it may have expired). Please connect again.", 400)
        if err == "invalid_client" or res.status_code == 401:
            raise CanvaError("Canva rejected this app's credentials. Check CANVA_CLIENT_ID/SECRET.", 502)
        raise CanvaError("Connecting to Canva failed. Please try again.")
    _store_tokens(db, user_id, res.json())


async def _refresh(db: Session, row: CanvaToken) -> None:
    try:
        refresh_token = _decrypt(row.refresh_token)
    except InvalidToken:
        # Encrypted with an older JWT secret.
        _drop_row(db, row)
        raise CanvaNotConnectedError("Your Canva connection expired. Please connect Canva again.") from None
    res = await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    if res.status_code == 429:
        raise CanvaThrottledError()
    if res.status_code in (400, 401):
        err, message = _canva_error(res)
        logger.info("Canva refresh rejected (%s %s): disconnecting user %s", err, message, row.user_id)
        if err == "invalid_client":
            raise CanvaError("Canva rejected this app's credentials. Check CANVA_CLIENT_ID/SECRET.", 502)
        _drop_row(db, row)
        raise CanvaNotConnectedError("Your Canva connection expired. Please connect Canva again.")
    if res.status_code != 200:
        raise CanvaError("Refreshing the Canva connection failed. Please try again.")
    _store_tokens(db, row.user_id, res.json(), row)


async def _access_token(db: Session, user_id: int, stale: str | None = None) -> str:
    """A usable access token. Pass ``stale`` (a token that just got a 401) to force a refresh."""
    async with _refresh_locks[user_id]:
        row = _get_row(db, user_id)
        if row is None:
            raise CanvaNotConnectedError()
        db.refresh(row)  # another request may have refreshed while we waited
        try:
            current = _decrypt(row.access_token)
        except InvalidToken:
            current = None
        expired = _aware(row.expires_at) - EXPIRY_MARGIN <= _now()
        if current is None or expired or (stale is not None and current == stale):
            await _refresh(db, row)
            current = _decrypt(row.access_token)
        return current


async def disconnect(db: Session, user_id: int) -> None:
    """Revoke the refresh token (best effort; also revokes its access tokens) and forget it."""
    row = _get_row(db, user_id)
    if row is None:
        return
    if settings.canva_client_id and settings.canva_client_secret:
        try:
            token = _decrypt(row.refresh_token)
            async with _http() as client:
                await client.post(REVOKE_URL, data={"token": token}, headers={"Authorization": _basic_auth()})
        except (InvalidToken, httpx.HTTPError) as exc:
            logger.info("Canva token revoke skipped: %s", exc)
    _drop_row(db, row)


# --- design import ------------------------------------------------------------


def _title_metadata(title: str) -> str:
    clean = " ".join(title.split())[:MAX_TITLE_CHARS].strip() or "PitchCraft deck"
    return json.dumps(
        {"title_base64": base64.b64encode(clean.encode()).decode(), "mime_type": PPTX_MIME},
        separators=(",", ":"),
    )


async def _authed(
    db: Session,
    user_id: int,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    """Send a request with the user's token; on a 401, refresh once and retry."""
    token = await _access_token(db, user_id)
    for attempt in range(2):
        try:
            res = await client.request(
                method, url, content=content, headers={**(headers or {}), "Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            logger.warning("Canva %s %s failed: %s", method, url, exc)
            raise CanvaError("Could not reach Canva. Please try again.") from exc
        if res.status_code != 401 or attempt == 1:
            break
        token = await _access_token(db, user_id, stale=token)

    if res.status_code == 401:
        row = _get_row(db, user_id)
        if row is not None:
            _drop_row(db, row)
        raise CanvaNotConnectedError("Canva no longer accepts this connection. Please connect Canva again.")
    if res.status_code == 429:
        raise CanvaThrottledError()
    if res.status_code == 403:
        raise CanvaNotConnectedError("Canva denied access (missing permission). Disconnect and connect Canva again.")
    return res


def _job_error(error: Any) -> CanvaError:
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    logger.warning("Canva import job failed: %s %s", code, message)
    if code in ("design_import_throttled", "design_creation_throttled"):
        return CanvaThrottledError()
    if code == "invalid_file":
        return CanvaError("Canva couldn't read the generated PPTX file.", 502)
    if code == "duplicate_import":
        return CanvaError("Canva says this deck was just imported. Check your Canva designs.", 409)
    return CanvaError(f"Canva couldn't import the deck{f': {message}' if message else '.'}", 502)


def _design_urls(job: dict[str, Any]) -> dict[str, str]:
    designs = (job.get("result") or {}).get("designs") or []
    urls = designs[0].get("urls") if designs and isinstance(designs[0], dict) else None
    if not isinstance(urls, dict) or not isinstance(urls.get("edit_url"), str):
        raise CanvaError("Canva finished the import but returned no design link.")
    return {"edit_url": urls["edit_url"], "view_url": str(urls.get("view_url") or urls["edit_url"])}


async def import_pptx(db: Session, user_id: int, pptx: bytes, title: str) -> dict[str, str]:
    """Upload ``pptx`` as a new Canva design and wait for it. Returns ``{edit_url, view_url}``."""
    require_configured()
    if not is_connected(db, user_id):
        raise CanvaNotConnectedError()

    async with _http(UPLOAD_TIMEOUT) as client:
        res = await _authed(
            db,
            user_id,
            client,
            "POST",
            IMPORTS_URL,
            content=pptx,
            headers={"Content-Type": "application/octet-stream", "Import-Metadata": _title_metadata(title)},
        )
        if res.status_code not in (200, 201, 202):
            err, message = _canva_error(res)
            logger.warning("Canva import create failed: %s %s %s", res.status_code, err, message)
            raise CanvaError(f"Canva rejected the import{f': {message}' if message else '.'}", 502)
        job = (res.json() or {}).get("job") or {}
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise CanvaError("Canva returned an unexpected import response.")

        deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
        attempt = 0
        while True:
            status = job.get("status")
            if status == "success":
                return _design_urls(job)
            if status == "failed":
                raise _job_error(job.get("error"))
            delay = POLL_DELAYS[min(attempt, len(POLL_DELAYS) - 1)]
            attempt += 1
            if time.monotonic() + delay > deadline:
                raise CanvaError(
                    "Canva is still processing the deck. Check your Canva designs in a minute.", 504
                )
            await asyncio.sleep(delay)
            res = await _authed(db, user_id, client, "GET", f"{IMPORTS_URL}/{job_id}")
            if res.status_code != 200:
                err, message = _canva_error(res)
                logger.warning("Canva import poll failed: %s %s %s", res.status_code, err, message)
                raise CanvaError("Checking the Canva import failed. Please try again.")
            job = (res.json() or {}).get("job") or {}
