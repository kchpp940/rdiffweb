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
        user:      The authenticated UserObject, or None.
        method:    How the user authenticated (token/password/session/none).
        scope:     List of scope strings granted by this authentication.
        is_valid:  Whether authentication succeeded.
        reject_reason: Machine-readable code if is_valid is False
                       ('invalid_credentials', 'mfa_required', 'token_expired', 'not_authenticated').
    """

    __slots__ = ('user', 'method', 'scope', 'is_valid', 'reject_reason')

    def __init__(self, user=None, method=AuthMethod.NONE, scope=None, is_valid=True, reject_reason=None):
        self.user = user
        self.method = method
        self.scope = scope or []
        self.is_valid = is_valid
        self.reject_reason = reject_reason

    def apply_to_request(self):
        """
        Write the authentication result into cherrypy.serving.request.

        This is the **only** place that sets request.scope and the
        canonical api_auth attribute.  After this call, controllers
        can reliably read request.api_auth to get the full picture.
        """
        request = cherrypy.serving.request
        request.api_auth = self
        request.scope = list(self.scope)

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


def resolve_api_auth(username=None, password=None):
    """
    Single entry point for API authentication.

    Resolves authentication through one of three paths:
      1. Access Token  – if password matches a non-expired token.
                           Checks expiration, updates access_time, applies token scope.
      2. Username/Password – if credentials match.
                           Rejects MFA-enabled users (MFA requires UI flow).
                           Grants 'all' scope.
      3. Session       – if the request already has a session login
                           (set by cherrypy_foundation.tools.auth).
                           Grants 'all' scope (MFA already completed in UI).

    Returns an AuthResult.  The caller is responsible for calling
    apply_to_request() on the successful result, or for raising
    an appropriate HTTP error on failure.
    """
    # --- Path 1: Access Token ---
    if username and password:
        userobj = UserObject.get_user(username)
        if userobj is not None:
            access_token = userobj.validate_access_token(password)
            if access_token:
                # Token is valid and not expired (checked by validate_access_token).
                access_token.accessed()
                access_token.commit()
                return AuthResult(
                    user=userobj,
                    method=AuthMethod.TOKEN,
                    scope=list(access_token.scope),
                    is_valid=True,
                )

            # --- Path 2: Username/Password ---
            # MFA-enabled users cannot use password auth via API.
            if userobj.mfa == UserObject.ENABLED_MFA:
                return AuthResult(
                    user=None,
                    method=AuthMethod.PASSWORD,
                    is_valid=False,
                    reject_reason='mfa_required',
                )

            valid = cherrypy.tools.auth.login_with_credentials(username, password)
            if valid:
                return AuthResult(
                    user=userobj,
                    method=AuthMethod.PASSWORD,
                    scope=['all'],
                    is_valid=True,
                )

    # --- Path 3: Session ---
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

    # --- No valid authentication ---
    return AuthResult(
        user=None,
        method=AuthMethod.NONE,
        is_valid=False,
        reject_reason='not_authenticated',
    )
