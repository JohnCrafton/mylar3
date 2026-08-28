#  This file is part of Mylar.
#
#  Mylar is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Mylar is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Mylar.  If not, see <http://www.gnu.org/licenses/>.

"""Taxonomy for the optional navigation overlay.

A declarative table rather than runtime introspection of WebInterface, which
would surface handlers needing arguments and API-only routes. Entries are
(label, endpoint, params, kind, confirm):

  params   query-string arguments. Handlers taking **kwargs may index a key
           out of it unconditionally - manageIssues() 500s without 'status' -
           and signature inspection cannot see that.
  kind     'page' for endpoints that render or redirect, linked directly;
           'task' for endpoints returning JSON or nothing, fired over ajax.
  confirm  ask before acting; reserved for endpoints that change state.

Absent on purpose: manageNotifs, blockProviders and manageExceptions read like
pages but only return JSON; idirectory() serves a template that does not exist
in the tree; searchit() requires a name, so the overlay renders it as a form.

tests/test_navmenu.py checks every endpoint here is exposed, argument-free,
and serves a template that exists.
"""

ARTICLES = (
    ('Library', 'home', {}, 'page', False),
    ('Wanted / Upcoming', 'upcoming', {}, 'page', False),
    ("This Week's Pull", 'pullist', {}, 'page', False),
    ('Story Arcs', 'storyarc_main', {}, 'page', False),
    ('Reading List', 'readlist', {}, 'page', False),
    ('Download History', 'history', {}, 'page', False),
    ('Download Queue', 'queueManage', {}, 'page', False),
    ('Manage', 'manage', {}, 'page', False),
    ('Manage Comics', 'manageComics', {}, 'page', False),
    ('Wanted Issues', 'manageIssues', {'status': 'Wanted'}, 'page', False),
    ('Snatched Issues', 'manageIssues', {'status': 'Snatched'}, 'page', False),
    ('Failed Downloads', 'manageFailed', {}, 'page', False),
    ('Import Results', 'importResults', {}, 'page', False),
    ('Logs', 'logs', {}, 'page', False),
    ('Config Dump', 'config_dump', {}, 'page', False),
)

ACTIONS = (
    ('Import a CBL Reading List', 'cblimport', {}, 'page', False),
    ('Search for Wanted Issues', 'forceSearch', {}, 'task', True),
    ('Force RSS Check', 'force_rss', {}, 'task', True),
    ('Force DB Update', 'forceUpdate', {}, 'page', True),
    ('Refresh Watchlist', 'dbupdater_watchlist', {}, 'task', True),
    ('Recreate Pull List', 'pullrecreate', {}, 'task', True),
    ('Check Future Pull', 'future_check', {}, 'page', True),
    ('Check for Mylar Update', 'checkGithub', {}, 'task', False),
    ('Generate API Key', 'generateAPI', {}, 'task', True),
    ('Clear Logs', 'clearLogs', {}, 'page', True),
    ('Download Care Package', 'carepackage', {}, 'page', False),
    ('Settings', 'config', {}, 'page', False),
    ('Update Mylar', 'update', {}, 'page', True),
    ('Restart Mylar', 'restart', {}, 'page', True),
    ('Shut Down Mylar', 'shutdown', {}, 'page', True),
)

MENU = {'articles': ARTICLES, 'actions': ACTIONS}


def _entries(group):
    return [
        {
            'label': label,
            'endpoint': endpoint,
            'params': dict(params),
            'kind': kind,
            'confirm': confirm,
        }
        for label, endpoint, params, kind, confirm in group
    ]


def menu_payload():
    """Return the menu as plain serialisable data.

    A fresh structure is built on every call so a caller mutating the result
    cannot corrupt the module-level tables.
    """
    return {'articles': _entries(ARTICLES), 'actions': _entries(ACTIONS)}
