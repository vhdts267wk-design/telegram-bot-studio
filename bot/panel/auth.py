"""Authentication and CSRF helpers for the admin panel."""

import hmac
import secrets

from fastapi import HTTPException, Request

from bot.panel.rate_limit import LoginRateLimiter


class AuthRedirect(Exception):
    """Raised by login_required to bounce unauthenticated users to /login."""


def _constant_time_equals(left: str, right: str) -> bool:
    """Compare UTF-8 bytes so non-ASCII input cannot crash authentication."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def check_credentials(request: Request, username: str, password: str) -> bool:
    """Constant-time check of submitted credentials against configured ones."""
    settings = request.app.state.settings
    user_ok = _constant_time_equals(username or "", settings.panel_username)
    pass_ok = _constant_time_equals(password or "", settings.panel_password)
    # Evaluate both before returning to avoid short-circuit timing differences.
    return user_ok and pass_ok


def login_required(request: Request) -> None:
    """FastAPI dependency that enforces an authenticated session."""
    if not request.session.get("authenticated"):
        raise AuthRedirect()


def get_csrf_token(request: Request) -> str:
    """Return the session CSRF token, creating one on first use."""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    """Validate a submitted CSRF token against the session token."""
    expected = request.session.get("csrf", "")
    if not expected or not _constant_time_equals(expected, submitted or ""):
        raise HTTPException(status_code=400, detail="Invalid CSRF token. Reload and retry.")
