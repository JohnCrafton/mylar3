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

"""Per-issue lifecycle events.

issues.Status holds one word at a time, so an issue reading 'Downloaded'
carries no record of which provider supplied it or what post-processing did.
That history only exists in the rolling log; this records it against the
issue.

record() runs inside the search, snatch and post-process paths, so it never
raises and never blocks - the queue is bounded, and a full queue drops rather
than stalling a download thread. Writes happen on a single worker thread
(mylar.queues.events) to keep another writer off the request path.
"""

import mylar

# Anything not in here is dropped at the boundary rather than stored, so the
# UI never has to defend against arbitrary strings appearing as event types.
EVENT_TYPES = frozenset([
    'search_started',
    'search_no_results',
    'search_skipped',
    'snatched',
    'sent_to_client',
    'client_accepted',
    'client_rejected',
    'download_complete',
    'pp_started',
    'pp_matched',
    'pp_no_match',
    'file_moved',
    'file_copy_failed',
    'marked_failed',
    'retry_queued',
    'manual_override',
])

# What each event reads as in the UI. Kept beside EVENT_TYPES so adding a type
# without a label is obvious in review.
EVENT_LABELS = {
    'search_started': 'Search started',
    'search_no_results': 'No results found',
    'search_skipped': 'Search skipped',
    'snatched': 'Snatched',
    'sent_to_client': 'Sent to download client',
    'client_accepted': 'Accepted by client',
    'client_rejected': 'Rejected by client',
    'download_complete': 'Download complete',
    'pp_started': 'Post-processing started',
    'pp_matched': 'Matched to issue',
    'pp_no_match': 'No match during post-processing',
    'file_moved': 'File filed into series folder',
    'file_copy_failed': 'File could not be filed',
    'marked_failed': 'Marked failed',
    'retry_queued': 'Retry queued',
    'manual_override': 'Manual override',
}

# Bounded on purpose: see the module docstring. 2000 is far more than a normal
# backlog, so hitting it means the worker is wedged, and dropping is correct.
MAX_QUEUED = 2000


def make_event(issueid, comicid=None, event=None, provider=None, detail=None):
    """Parse loose arguments into a complete event row, or None.

    This is the validation boundary. Callers downstream may assume every key
    is present and every EventType is one of EVENT_TYPES.
    """
    if event not in EVENT_TYPES:
        return None
    if issueid is None or issueid == '' or issueid == 0:
        return None

    from mylar import helpers

    return {
        'IssueID': str(issueid),
        'ComicID': None if comicid is None else str(comicid),
        'EventType': event,
        'EventTime': helpers.now(),
        'Provider': None if provider is None else str(provider),
        'Detail': None if detail is None else str(detail),
        # Reserved for the download-client polling feature so that lands as
        # extra rows rather than a schema migration.
        'Progress': None,
        'QueuePosition': None,
        'Extra': None,
    }


def record(issueid, comicid=None, event=None, provider=None, detail=None):
    """Queue one lifecycle event. Returns whether it was queued.

    Never raises. The return value is for tests and callers that care; the
    download path ignores it.
    """
    try:
        if not mylar.CONFIG.ENABLE_ISSUE_EVENTS:
            return False

        entry = make_event(issueid, comicid, event, provider, detail)
        if entry is None:
            return False

        mylar.EVENT_QUEUE.put_nowait(entry)
        return True
    except Exception:
        # Deliberately silent, including no logger call: this runs on the
        # download path and the failure being swallowed is the whole point.
        return False


# Sortable columns for the activity feed, keyed by the DataTables column index.
# A whitelist rather than interpolation: iSortCol_0 arrives from the query
# string and ends up inside ORDER BY.
ACTIVITY_SORT_COLUMNS = {
    '0': 'e.EventTime',
    '1': 'c.ComicName',
    '2': 'e.EventType',
    '3': 'e.Provider',
}


def activity_order_by(column, direction):
    """Build a safe ORDER BY for the activity feed.

    Anything unrecognised falls back to newest-first, which is the only
    ordering that is always meaningful for an append-only log.
    """
    sort = ACTIVITY_SORT_COLUMNS.get(str(column), 'e.EventTime')
    way = 'ASC' if str(direction).lower() == 'asc' else 'DESC'
    # EventID breaks ties: several events for one issue can share a timestamp,
    # and without it their order flips between queries.
    return '%s %s, e.EventID %s' % (sort, way, way)

