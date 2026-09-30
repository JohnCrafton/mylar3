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

"""The long-running orphan jobs: identifying groups, and filing series.

Everything with a side effect outside the database - ComicVine, the clock,
sleeping, adding a series, rescanning, history - is passed in, so the jobs can
be run against a stub in tests and against Mylar in webserve.
"""

import collections
import json
import threading

from mylar import orphan_batches as batches
from mylar import orphans


class JobState(object):
    """The one orphan job that may run at a time, and what it is doing.

    Shared between the worker thread and the status endpoint, so every
    access takes the lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._kind = None
        self._stop = False
        self._progress = {}
        self._message = ''

    def begin(self, kind):
        with self._lock:
            if self._kind is not None:
                return False
            self._kind, self._stop, self._progress, self._message = kind, False, {}, ''
            return True

    def update(self, progress):
        with self._lock:
            self._progress = dict(progress)

    def finish(self, message):
        with self._lock:
            self._kind, self._message = None, message

    def request_stop(self):
        with self._lock:
            self._stop = True

    def should_stop(self):
        with self._lock:
            return self._stop

    def snapshot(self):
        with self._lock:
            return {'running': self._kind is not None, 'kind': self._kind,
                    'progress': dict(self._progress), 'message': self._message,
                    'stopping': self._stop}


STATE = JobState()


def _unfiled_orphans(db):
    return [dict(r) for r in db.select(
        "SELECT OrphanID, FilePath, FileName, FileSize, ParsedSeries, ParsedIssue,"
        " ParsedYear, ParsedVolume FROM orphans WHERE Status IN ('new', 'identified')")]


def _save_group(db, group, ev, ranked, tier, failed, when):
    known = [n for n in ev.numbers if n is not None]
    top = ranked[0] if ranked else None
    db.action('UPDATE orphans SET GroupID=? WHERE OrphanID=?',
              [(group.groupid, r['OrphanID']) for r in group.orphans], executemany=True)
    result = db.action('INSERT OR REPLACE INTO orphan_groups (GroupID, Folder, Volume, Evidence,'
                       ' Candidates, Tier, Failed, ComicID, Status, IdentifiedDate)'
                       ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       [group.groupid, group.folder, group.volume,
                        json.dumps({'series': ev.series, 'share': round(ev.share, 3),
                                    'year': ev.year, 'folder_year': ev.folder_year,
                                    'files': ev.files, 'max_issue': max(known) if known else None,
                                    'numbered': round(len(known) / float(ev.files), 3)}),
                        json.dumps(batches.candidate_summary(ranked)),
                        tier, json.dumps(failed),
                        top.get('comicid') if tier == 'auto' and top else None,
                        'identified', when])
    if result is None:
        raise RuntimeError('Could not record group %s' % group.groupid)


def identify_groups(db, search, now, sleep, pace, stop=lambda: False,
                    progress=lambda d: None):
    """Tier every unidentified group, largest first. Safe to stop and rerun.

    A group whose search failed is not recorded, so the next run asks again.
    After MAX_SEARCH_FAILURES failures in a row ComicVine is taken to be down
    or refusing this server, and the job stops rather than keep asking.
    """
    groups = batches.group_orphans(_unfiled_orphans(db))
    done = set(r['GroupID'] for r in db.select('SELECT GroupID FROM orphan_groups'))
    todo = sorted((g for g in groups.values() if g.groupid not in done),
                  key=lambda g: (-len(g.orphans), g.folder))

    counts = collections.Counter()
    failures = 0

    def summary(stopped, reason):
        return {'auto': counts['auto'], 'review': counts['review'],
                'mixed': counts['mixed'], 'stopped': stopped, 'reason': reason}

    for position, group in enumerate(todo):
        if stop():
            return summary(True, 'Stopped on request.')
        ev = batches.evidence(group)
        ranked = []
        searched = False
        if ev.series and ev.share >= batches.MIXED_SHARE:
            parsed = batches.search_parsed(ev)
            results = search(ev.series, parsed['issue'], ev.year)
            searched = True
            if results is False:
                failures += 1
                if failures >= batches.MAX_SEARCH_FAILURES:
                    return summary(True, 'ComicVine did not answer %d searches in a row;'
                                         ' it may be down or limiting this server.'
                                         ' Nothing was recorded for those groups - run'
                                         ' again later to carry on.' % failures)
                sleep(pace)
                continue
            failures = 0
            ranked = orphans.rank_candidates(parsed, results or [])

        tier, failed = batches.tier(ev, ranked)
        _save_group(db, group, ev, ranked, tier, failed, now())
        counts[tier] += 1
        progress({'done': position + 1, 'total': len(todo), 'folder': group.folder,
                  'auto': counts['auto'], 'review': counts['review'],
                  'mixed': counts['mixed']})
        if searched:
            sleep(pace)

    return summary(False, None)


def approve_auto(db, exclude=()):
    """Approve every identified auto group except those excluded."""
    excluded = [g for g in exclude if g]
    query = ("UPDATE orphan_groups SET Status='approved'"
             " WHERE Tier='auto' AND Status='identified'")
    if excluded:
        query += ' AND GroupID NOT IN (%s)' % ', '.join(['?'] * len(excluded))
    return db.action(query, excluded).rowcount


def pick_series(db, groupid, comicid):
    """Approve a group against a series the user chose."""
    return db.action("UPDATE orphan_groups SET ComicID=?, Status='approved'"
                     " WHERE GroupID=? AND Status NOT IN ('filed', 'partial')",
                     [comicid, groupid]).rowcount == 1


def skip_group(db, groupid):
    return db.action("UPDATE orphan_groups SET Status='skipped' WHERE GroupID=?"
                     " AND Status NOT IN ('filed', 'partial')", [groupid]).rowcount == 1


def load_groups(db):
    """Groups for the page, split by tier, largest first."""
    first = dict((r['GroupID'], r['OrphanID']) for r in db.select(
        'SELECT GroupID, MIN(OrphanID) AS OrphanID FROM orphans'
        ' WHERE GroupID IS NOT NULL GROUP BY GroupID'))
    tiers = {'auto': [], 'review': [], 'mixed': []}
    for row in db.select('SELECT * FROM orphan_groups'):
        group = dict(row)
        group['Evidence'] = json.loads(group['Evidence'] or '{}')
        group['Candidates'] = json.loads(group['Candidates'] or '[]')
        group['Failed'] = json.loads(group['Failed'] or '[]')
        group['Files'] = group['Evidence'].get('files', 0)
        group['FirstOrphanID'] = first.get(group['GroupID'])
        tiers.setdefault(group['Tier'], []).append(group)
    for members in tiers.values():
        members.sort(key=lambda g: (-g['Files'], g['Folder']))
    return tiers


def load_batches(db):
    """Filing batches, newest first, with their row counts."""
    batches_ = collections.OrderedDict()
    for row in db.select("SELECT BatchID, RunID, ComicID, Status, COUNT(*) AS n,"
                         " MAX(WhenDone) AS WhenDone FROM orphan_moves"
                         " WHERE Kind IN ('move', 'park')"
                         " GROUP BY BatchID, RunID, ComicID, Status"
                         " ORDER BY MAX(WhenDone) DESC"):
        entry = batches_.setdefault(row['BatchID'], {
            'BatchID': row['BatchID'], 'RunID': row['RunID'], 'ComicID': row['ComicID'],
            'done': 0, 'failed': 0, 'planned': 0, 'reverted': 0, 'moving': 0, 'cancelled': 0, 'When': row['WhenDone']})
        entry[row['Status']] = row['n']
        entry['When'] = max(entry['When'] or '', row['WhenDone'] or '')
    return sorted(batches_.values(), key=lambda b: b['When'] or '', reverse=True)
