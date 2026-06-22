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


def required_scope(scope):
    """
    Check the current authentication has the required scope to access the resource.

    Supported scope hierarchy (implicit inclusion):
      - 'all' implicitly includes all other scopes
      - 'write_user' implicitly includes 'read_user'
      - 'admin_write_users' implicitly includes 'admin_read_users'
    """
    # Convert single scope or scope list to array.
    if isinstance(scope, str):
        scope = scope.split(',')
    # Get the current user scope
    current_scope = getattr(cherrypy.serving.request, 'scope', [])
    if not current_scope:
        raise cherrypy.HTTPError(403)

    # Expand current scope with implicit permissions
    expanded_scope = set(current_scope)
    if 'all' in current_scope:
        expanded_scope.update([
            'read_user', 'write_user',
            'admin_read_users', 'admin_write_users',
        ])
    if 'write_user' in current_scope:
        expanded_scope.add('read_user')
    if 'admin_write_users' in current_scope:
        expanded_scope.add('admin_read_users')

    # Check if our expanded current_scope match any of the required scope.
    for s in scope:
        if s in expanded_scope:
            return True
    raise cherrypy.HTTPError(403)


# Make sure it's running after authentication (priority = 72)
cherrypy.tools.required_scope = cherrypy.Tool('before_handler', required_scope, priority=85)
