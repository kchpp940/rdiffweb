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

logger = logging.getLogger(__name__)


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
@cherrypy.tools.api_authenticate()
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
