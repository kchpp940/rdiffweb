# rdiffweb, A web interface to rdiff-backup repositories
# Copyright (C) 2012-2025 rdiffweb contributors
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

import json
import logging

import cherrypy

from rdiffweb.controller.api_currentuser import ApiCurrentUser
from rdiffweb.controller.api_openapi import OpenAPI
from rdiffweb.controller.page_admin_users import AdminApiUsers
from rdiffweb.core.model import UserObject

logger = logging.getLogger(__name__)


def _checkpassword(realm, username, password):
    """
    Check basic authentication.

    Unified authentication path for API:
    1. First tries access token validation (checks expiration, updates access_time, applies token scope)
    2. If MFA is enabled for user, rejects password-based auth via API
    3. Falls back to standard username/password validation (grants 'all' scope)
    4. On any failure, increments rate limit counter
    """
    # Validate username
    userobj = UserObject.get_user(username)
    if userobj is not None:
        # Path 1: Verify if the password matches a token.
        # validate_access_token() internally checks for expiration.
        access_token = userobj.validate_access_token(password)
        if access_token:
            # Update token access timestamp
            access_token.accessed()
            access_token.commit()
            cherrypy.serving.request.scope = access_token.scope
            return True
        # Path 2: Disable password authentication for MFA-enabled users on API.
        # (MFA flow requires UI interaction, which is not available via Basic Auth)
        if userobj.mfa == UserObject.ENABLED_MFA:
            cherrypy.tools.ratelimit.increase_hit()
            return False
    # Path 3: Standard username/password validation.
    # On success, this user gets 'all' scope (equivalent to full interactive session).
    valid = cherrypy.tools.auth.login_with_credentials(username, password)
    if valid:
        cherrypy.serving.request.scope = ['all']
        return True
    # Path 4: Invalid credentials.
    cherrypy.tools.ratelimit.increase_hit()
    return False


def _ensure_api_scope():
    """
    Post-authentication hook to guarantee scope is set on the request.

    Covers the case where a user authenticates via session (UI login) and then
    makes an API call. The auth_basic path in _checkpassword() sets scope,
    but session-based auth does not. This tool ensures any authenticated
    request has proper scope defaulting to ['all'] for interactive sessions.

    Priority is 75 to run after auth_basic (70) and auth (72) but before
    required_scope (85).
    """
    if not hasattr(cherrypy.serving.request, 'scope') or not cherrypy.serving.request.scope:
        # Session-authenticated users get full scope since they passed
        # the complete interactive auth flow (including MFA if enabled).
        if getattr(cherrypy.serving.request, 'login', False):
            cherrypy.serving.request.scope = ['all']


# Register the scope-sync tool. Priority 75 = after auth, before required_scope.
cherrypy.tools.ensure_api_scope = cherrypy.Tool('before_handler', _ensure_api_scope, priority=75)


def _api_json_error():
    """
    Error response formatter for API endpoints.

    Ensures all API errors (400, 401, 403, 404, 405, 500, etc.) are returned
    as a consistent JSON structure instead of the default HTML error page.

    Response format:
        {
            "code": 400,
            "status": "400 Bad Request",
            "message": "Detailed error description"
        }
    """
    response = cherrypy.serving.response
    request = cherrypy.serving.request

    # Only apply to API paths
    if not request.path_info.startswith('/api/'):
        return

    # Get error info from the request
    error = getattr(request, 'error_page', None)
    if error is None:
        # Try to get from HTTPError
        status = response.status
        code = response.status_code
        message = ''
    else:
        status = error.status
        code = error.code
        message = error.message or ''

    # Build consistent error response
    error_body = {
        "code": code,
        "status": status,
        "message": message,
    }

    # Set response
    response.headers['Content-Type'] = 'application/json'
    response.body = json.dumps(error_body).encode('utf-8')


# Register API error formatter. Priority 10 = early in the error response chain.
cherrypy.tools.api_json_error = cherrypy.Tool('before_error_response', _api_json_error, priority=10)


@cherrypy.expose
@cherrypy.tools.allow(on=False)
@cherrypy.tools.json_out(on=True)
@cherrypy.tools.json_in(on=True, force=False)
@cherrypy.tools.auth_basic(realm='rdiffweb', checkpassword=_checkpassword, priority=70)
@cherrypy.tools.auth(on=True, redirect=False)
@cherrypy.tools.ensure_api_scope()
@cherrypy.tools.api_json_error()
@cherrypy.tools.auth_mfa(on=False)
@cherrypy.tools.i18n(on=False)
@cherrypy.tools.ratelimit(scope='rdiffweb-api', hit=0, debug=1, priority=69)
@cherrypy.tools.sessions(on=False)
class ApiPage:
    """
    This class provide a restful API to access some of the rdiffweb resources.
    """

    currentuser = ApiCurrentUser()
    openapi_json = OpenAPI()
    users = AdminApiUsers()

    def get(self):
        """
        Returns the current application version in JSON format.

        **Example Response**

        ```json
        {
            "version": "1.2.8"
        }
        ```
        """
        version = cherrypy.tree.apps[''].version
        return {
            "version": version,
        }
