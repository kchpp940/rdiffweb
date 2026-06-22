# Required scope tools for cherrypy
# Copyright (C) 2023 IKUS Software
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
import cherrypy

from rdiffweb.tools.api_auth import AuthResult


# Canonical list of all supported scopes and their display names.
# Used for validation, UI display, and OpenAPI generation.
# IMPORTANT: Scopes are independent - there is NO implicit inheritance.
# Read scopes grant read-only access; write scopes grant write-only access.
# The 'all' scope is the exception and grants full access (password/session users).
SCOPE_DEFS = {
    'all': 'Everything - Full read and write access to all resources.',
    'read_user': 'Read current user - Read access to your profile, repositories, ssh keys and access tokens. Read-only.',
    'write_user': 'Write current user - Write access to your profile, repositories, ssh keys and access tokens. Does not include read access.',
    'admin_read_users': 'Admin read users - Read access to all user data. Read-only.',
    'admin_write_users': 'Admin write users - Create, update and delete all users. Does not include read access.',
}


def required_scope(scope):
    """
    Check the current authentication has the required scope to access the resource.

    Reads scope from the unified AuthResult (request.api_auth) rather than
    directly from request.scope, ensuring the value was set by the single
    resolve_api_auth() entry point.

    Each endpoint must explicitly declare which scopes are allowed. There is NO
    implicit scope inheritance (e.g. write_user does NOT grant read_user).
    The 'all' scope is the only exception: it grants access to every endpoint
    and is automatically assigned to password/session authenticated users.

    Pattern:
      - Read endpoints:  scope='all,read_user'  or  scope='all,admin_read_users'
      - Write endpoints: scope='all,write_user' or  scope='all,admin_write_users'
    """
    # Convert single scope or scope list to array.
    if isinstance(scope, str):
        scope = scope.split(',')

    # Read scope from the unified AuthResult.
    auth = AuthResult.from_request()

    # If authentication failed entirely, raise 403.
    if not auth.is_valid:
        raise cherrypy.HTTPError(403)

    current_scope = auth.scope
    if not current_scope:
        raise cherrypy.HTTPError(403)

    # Check if any of the required scopes matches the current scope.
    # 'all' is a wildcard that matches every required scope.
    current_scope_set = set(current_scope)
    if 'all' in current_scope_set:
        return True
    for s in scope:
        if s != 'all' and s in current_scope_set:
            return True
    raise cherrypy.HTTPError(403)


# Make sure it's running after authentication (priority = 85)
cherrypy.tools.required_scope = cherrypy.Tool('before_handler', required_scope, priority=85)
