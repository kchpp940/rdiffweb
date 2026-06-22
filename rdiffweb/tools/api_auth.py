# Unified API authentication for rdiffweb
# Copyright (C) 2023-2025 IKUS Software
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import base64
import logging
from enum import Enum

import cherrypy

from rdiffweb.core.model import UserObject

logger = logging.getLogger(__name__)


class AuthMethod(Enum):
    TOKEN = 'token'
    PASSWORD = 'password'
    SESSION = 'session'
    NONE = 'none'


class AuthResult:
    """
    Immutable authentication result consumed by all API controllers.

    Every authentication path (token, password, session) produces this
    single object.  Controllers should never inspect request.login,
    request.currentuser.mfa, or request.scope directly to infer
    authentication state — they read it from here instead.

    Attributes:
        user:          The authenticated UserObject, or None.
        method:        How the user authenticated (token/password/session/none).
        scope:         List of scope strings granted by this authentication.
        is_valid:      Whether authentication succeeded.
        reject_reason: Machine-readable code if is_valid is False
                       ('invalid_credentials', 'mfa_required', 'token_expired',
                        'not_authenticated', 'invalid_scope').
        http_code:     Suggested HTTP status code (401 or 403) if is_valid is False.
    """

    __slots__ = ('user', 'method', 'scope', 'is_valid', 'reject_reason', 'http_code')

    def __init__(
        self,
        user=None,
        method=AuthMethod.NONE,
        scope=None,
        is_valid=True,
        reject_reason=None,
        http_code=None,
    ):
        self.user = user
        self.method = method
        self.scope = scope or []
        self.is_valid = is_valid
        self.reject_reason = reject_reason
        # Default HTTP codes by reject_reason if not specified
        if not is_valid and http_code is None:
            http_code = {
                'not_authenticated': 401,
                'invalid_credentials': 401,
                'token_expired': 401,
                'mfa_required': 401,
                'invalid_scope': 403,
            }.get(reject_reason, 401)
        self.http_code = http_code

    def apply_to_request(self):
        """
        Write the authentication result into cherrypy.serving.request.

        This is the **only** place that sets request.scope,
        request.api_auth, and request.currentuser for API requests.
        After this call, controllers can reliably read request.api_auth
        to get the full picture.
        """
        request = cherrypy.serving.request
        request.api_auth = self
        request.scope = list(self.scope)
        # Set currentuser for backward compatibility with cherrypy_foundation
        # tools that still read it (e.g. logging).  But new code should
        # read from api_auth.user instead.
        if self.user is not None:
            request.currentuser = self.user

    @classmethod
    def from_request(cls):
        """
        Read the authentication result that was previously applied
        to the current request.

        Returns an AuthResult with is_valid=False if none was set
        (i.e. the request was not authenticated through the unified path).
        """
        auth = getattr(cherrypy.serving.request, 'api_auth', None)
        if auth is not None:
            return auth
        return cls(is_valid=False, reject_reason='not_authenticated')


def _parse_basic_auth_header():
    """
    Parse the HTTP Basic Auth header from the current request.

    Returns a (username, password) tuple, or (None, None) if no valid
    Basic Auth header is present.
    """
    auth_header = cherrypy.serving.request.headers.get('Authorization', '')
    if not auth_header.startswith('Basic '):
        return None, None
    try:
        decoded = base64.b64decode(auth_header[6:]).decode('utf-8')
        username, password = decoded.split(':', 1)
        return username or None, password or None
    except Exception:
        return None, None


def resolve_api_auth():
    """
    Single entry point for API authentication.  The three paths are
    strictly mutually exclusive:

      1. Basic Auth credentials present → resolves within this path
           ├─ password matches a non-expired access token → TOKEN path
           │   (updates access_time, applies token scope)
           ├─ password matches user password
           │   ├─ MFA-enabled user → 401 mfa_required
           │   └─ normal user → PASSWORD path, scope=['all']
           └─ neither matches → 401 invalid_credentials

      2. No Basic Auth header → Session path
           ├─ request.login has a valid session user → SESSION path, scope=['all']
           └─ no session → 401 not_authenticated

    Returns an AuthResult.  is_valid is True only if authentication
    fully succeeded.  http_code / reject_reason explain why on failure.
    """
    username, password = _parse_basic_auth_header()

    # --- Path 1: Basic Auth credentials present → strict resolution within this path ---
    if username and password:
        userobj = UserObject.get_user(username)
        if userobj is None:
            # Unknown username: don't leak that the user doesn't exist,
            # return generic invalid credentials.
            return AuthResult(
                user=None,
                method=AuthMethod.NONE,
                is_valid=False,
                reject_reason='invalid_credentials',
            )

        # Path 1a: Try access token first.  If it matches (even if the
        # password *also* happens to match the user's real password),
        # we commit to the TOKEN path — no fallback to password auth.
        access_token = userobj.validate_access_token(password)
        if access_token:
            # Token is valid and not expired (checked inside validate_access_token).
            try:
                access_token.accessed()
                access_token.commit()
            except Exception:
                # If access_time update fails, still treat authentication
                # as valid but log the issue.
                logger.warning("failed to update token access_time for user %s", username)
            return AuthResult(
                user=userobj,
                method=AuthMethod.TOKEN,
                scope=list(access_token.scope),
                is_valid=True,
            )

        # Path 1b: Token didn't match.  Fall back to password auth, but
        # MFA-enabled users are always rejected via API (no UI flow).
        if userobj.mfa == UserObject.ENABLED_MFA:
            return AuthResult(
                user=None,
                method=AuthMethod.PASSWORD,
                is_valid=False,
                reject_reason='mfa_required',
            )

        # Path 1c: Standard username/password check.
        try:
            valid = cherrypy.tools.auth.login_with_credentials(username, password)
        except Exception:
            valid = False
        if valid:
            return AuthResult(
                user=userobj,
                method=AuthMethod.PASSWORD,
                scope=['all'],
                is_valid=True,
            )

        # Neither token nor password matched.
        return AuthResult(
            user=None,
            method=AuthMethod.NONE,
            is_valid=False,
            reject_reason='invalid_credentials',
        )

    # --- Path 2: No Basic Auth header → Session path ---
    login = getattr(cherrypy.serving.request, 'login', None)
    if login:
        userobj = UserObject.get_user(login)
        if userobj is not None:
            return AuthResult(
                user=userobj,
                method=AuthMethod.SESSION,
                scope=['all'],
                is_valid=True,
            )

    # --- No valid authentication at all ---
    return AuthResult(
        user=None,
        method=AuthMethod.NONE,
        is_valid=False,
        reject_reason='not_authenticated',
    )


def _api_authenticate_tool():
    """
    CherryPy Tool that wraps resolve_api_auth() and applies the result
    to the request.

    This is the SINGLE authentication hook for the API.  It replaces
    cherrypy.tools.auth_basic, cherrypy.tools.auth (for API paths),
    and the old resolve_api_auth / ensure_api_scope helpers.

    Priority 70: runs before required_scope (85) and is_admin (80).
    On authentication failure it raises cherrypy.HTTPError with the
    correct status code (401 / 403) and message.
    """
    auth = resolve_api_auth()
    if auth.is_valid:
        auth.apply_to_request()
        return

    # Apply the failed result so downstream error handlers can inspect it.
    auth.apply_to_request()

    # Rate-limiting on any authentication failure (token mismatch,
    # invalid password, MFA rejection).  Pure "no credentials at all"
    # doesn't count as a failed attempt.
    if auth.reject_reason in ('invalid_credentials', 'mfa_required', 'token_expired'):
        try:
            cherrypy.tools.ratelimit.increase_hit()
        except Exception:
            pass

    # Map reject_reason to a precise HTTP error.
    message_map = {
        'invalid_credentials': 'Invalid credentials',
        'mfa_required': 'MFA is enabled; password authentication is not available via the API',
        'token_expired': 'Access token has expired',
        'not_authenticated': 'Authentication required',
    }
    message = message_map.get(auth.reject_reason, 'Authentication failed')
    raise cherrypy.HTTPError(auth.http_code, message)


# Register the single API authentication tool.
# Priority 70 = before required_scope (85) and is_admin (80).
cherrypy.tools.api_authenticate = cherrypy.Tool('before_handler', _api_authenticate_tool, priority=70)
