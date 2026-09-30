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
import os
import threading
import uuid

from mylar import orphan_batches as batches
from mylar import orphan_moves as moves
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
        self._wake = threading.Event()

    def begin(self, kind):
        with self._lock:
            if self._kind is not None:
                return False
            self._kind, self._stop, self._progress, self._message = kind, False, {}, ''
            self._wake.clear()
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
            self._wake.set()

    def sleep(self, seconds):
        """Sleep, but wake early if a stop is requested."""
        self._wake.wait(seconds)

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


FileDeps = collections.namedtuple(
    'FileDeps', 'ensure_series name_for rescan record_history now clock stop'
                ' library_root progress log')

BatchResult = collections.namedtuple(
    'BatchResult', 'batch_id comicid moved parked skipped failed stopped error')


def _series(db, comicid):
    rows = db.select('SELECT * FROM comics WHERE ComicID=?', [comicid])
    if not rows:
        return None
    comic = dict(rows[0])
    if not comic.get('ComicLocation') or comic.get('Status') == 'Loading':
        return None
    issues = [dict(r) for r in db.select(
        'SELECT IssueID, Issue_Number, Location FROM issues WHERE ComicID=?', [comicid])]
    if not issues:
        return None
    comic['issues'] = issues
    return comic


def wait_for_series(db, comicid, add, sleep, timeout, poll=5):
    """The series ready to file into, adding it first if Mylar lacks it.

    Adding is queued inside Mylar, so this waits for the folder and issue list
    to appear. None after timeout seconds.
    """
    if not db.select('SELECT ComicID FROM comics WHERE ComicID=?', [comicid]):
        add(comicid)
    waited = 0
    while True:
        comic = _series(db, comicid)
        if comic is not None:
            return comic
        if waited >= timeout:
            return None
        sleep(poll)
        waited += poll


def _finish(db, batch_id, comic, deps, skipped, started):
    """Execute a recorded batch and bring every table into line with the disk."""
    result = moves.execute(db, batch_id, deps.now, stop=deps.stop)
    moves.remove_empty_dirs(db, batch_id, deps.library_root, deps.now)

    issues = dict((i['IssueID'], i) for i in comic['issues'])
    done = [dict(r) for r in db.select(
        "SELECT m.OrphanID, m.IssueID, m.Kind, m.Destination, o.Status AS Was,"
        " o.FilePath, o.FileName, o.FileSize FROM orphan_moves m"
        " JOIN orphans o ON o.OrphanID = m.OrphanID"
        " WHERE m.BatchID=? AND m.Status='done' AND m.Kind IN ('move', 'park')",
        [batch_id])]

    moved = [r for r in done if r['Kind'] == 'move']
    parked = [r for r in done if r['Kind'] == 'park']
    unrecorded = 0
    for row in moved:
        if row['Was'] == 'filed':
            continue          # recorded on an earlier pass of this batch
        try:
            deps.record_history(row, comic, issues.get(row['IssueID'], {}),
                                row['Destination'])
        except Exception as e:
            deps.log('[ORPHANS] Could not record history for %s: %s' % (row['FileName'], e))
            unrecorded += 1
            continue
        db.action("UPDATE orphans SET Status='filed', IssueID=? WHERE OrphanID=?",
                  [row['IssueID'], row['OrphanID']])
    if parked:
        db.action("UPDATE orphans SET Status='parked' WHERE OrphanID=?",
                  [(r['OrphanID'],) for r in parked], executemany=True)

    if moved:
        # Mylar works out each issue's status and location from what is now on
        # disk - the same thing import does - rather than rows written by hand
        deps.rescan(comic['ComicID'])

    counts = moves.batch_counts(db, batch_id)
    finished = (not counts.get('planned') and not counts.get('failed')
                and not counts.get('moving') and not unrecorded)
    db.action('UPDATE orphan_groups SET Status=? WHERE GroupID IN'
              ' (SELECT DISTINCT GroupID FROM orphan_moves WHERE BatchID=?'
              " AND GroupID IS NOT NULL)", ['filed' if finished else 'partial', batch_id])

    outcome = BatchResult(batch_id, comic['ComicID'], len(moved), len(parked), skipped,
                          counts.get('failed', 0), result.stopped, None)
    deps.log('[ORPHANS] Batch %s %s (%s): %d moved, %d parked, %d skipped, %d failed'
             '%s in %.0fs' % (batch_id, comic.get('ComicName'), comic['ComicID'],
                               outcome.moved, outcome.parked, outcome.skipped,
                               outcome.failed, ', stopped' if outcome.stopped else '',
                               deps.clock() - started))
    return outcome


def file_series(db, comicid, deps, run_id):
    """Plan, record and carry out filing every approved group of one series."""
    started = deps.clock()
    comic = deps.ensure_series(comicid)
    if comic is None:
        deps.log('[ORPHANS] Series %s could not be added or is still loading;'
                 ' its groups were left approved for the next run.' % comicid)
        return BatchResult(None, comicid, 0, 0, 0, 0, False,
                           'The series could not be added to Mylar.')

    files = [dict(r) for r in db.select(
        "SELECT o.* FROM orphans o JOIN orphan_groups g ON g.GroupID = o.GroupID"
        " WHERE g.ComicID=? AND g.Status='approved'"
        " AND o.Status IN ('new', 'identified')", [comicid])]
    location = comic['ComicLocation']

    def has_file(issue):
        # a recorded Location is not proof - 258 of one library's were stale
        return bool(issue.get('Location')) and os.path.isfile(
            os.path.join(location, issue['Location']))

    plan = batches.plan_series(files, comic['issues'], has_file, location,
                               lambda o, i: deps.name_for(comic, o, i),
                               deps.library_root)
    batch_id = uuid.uuid4().hex[:12]
    moves.record_plan(db, run_id, batch_id, comicid,
                      dict((f['OrphanID'], f['GroupID']) for f in files),
                      plan.moves, deps.now())
    if not plan.moves:
        db.action("UPDATE orphan_groups SET Status='filed' WHERE ComicID=?"
                  " AND Status='approved'", [comicid])
        deps.log('[ORPHANS] Series %s: nothing to move, %d skipped'
                 % (comicid, len(plan.skipped)))
        return BatchResult(batch_id, comicid, 0, 0, len(plan.skipped), 0, False, None)
    return _finish(db, batch_id, comic, deps, len(plan.skipped), started)


def resume_batch(db, batch_id, deps):
    """Carry on a stopped or partly failed batch from its log."""
    rows = db.select('SELECT ComicID FROM orphan_moves WHERE BatchID=? LIMIT 1', [batch_id])
    if not rows:
        return BatchResult(batch_id, None, 0, 0, 0, 0, False, 'Unknown batch')
    counts = moves.batch_counts(db, batch_id)
    unfiled = db.select(
        "SELECT COUNT(*) AS n FROM orphan_moves m JOIN orphans o ON o.OrphanID = m.OrphanID"
        " WHERE m.BatchID=? AND m.Status='done' AND m.Kind='move' AND o.Status != 'filed'",
        [batch_id])[0]['n']
    if not (counts.get('planned') or counts.get('failed') or counts.get('moving') or unfiled):
        return BatchResult(batch_id, rows[0]['ComicID'], 0, 0, 0, 0, False,
                           'Nothing left to file in this batch.')
    comic = deps.ensure_series(rows[0]['ComicID'])
    if comic is None:
        return BatchResult(batch_id, rows[0]['ComicID'], 0, 0, 0, 0, False,
                           'The series is not ready in Mylar.')
    return _finish(db, batch_id, comic, deps, 0, deps.clock())


def run_file_job(db, deps, run_id):
    """File every approved series, one batch per series."""
    # largest first; summed here rather than with json_extract, which not every
    # sqlite build Mylar runs on has
    files = collections.Counter()
    for row in db.select("SELECT ComicID, Evidence FROM orphan_groups"
                         " WHERE Status='approved' AND ComicID IS NOT NULL"):
        files[row['ComicID']] += json.loads(row['Evidence'] or '{}').get('files', 0)
    series = [c for c, _ in sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))]
    results = []
    for position, comicid in enumerate(series):
        if deps.stop():
            break
        outcome = file_series(db, comicid, deps, run_id)
        results.append(outcome)
        deps.progress({'done': position + 1, 'total': len(series), 'comicid': comicid,
                       'moved': sum(r.moved for r in results),
                       'parked': sum(r.parked for r in results),
                       'failed': sum(r.failed for r in results)})
        if outcome.stopped:
            break
    deps.log('[ORPHANS] Run %s: %d series, %d moved, %d parked, %d skipped, %d failed'
             % (run_id, len(results), sum(r.moved for r in results),
                sum(r.parked for r in results), sum(r.skipped for r in results),
                sum(r.failed for r in results)))
    return results


def revert_batch(db, batch_id, deps):
    """Undo a batch and put its orphans and groups back as they were."""
    before = dict((r['Seq'], r['OrphanID']) for r in db.select(
        "SELECT Seq, OrphanID FROM orphan_moves WHERE BatchID=? AND Status='done'"
        " AND Kind IN ('move', 'park')", [batch_id]))
    result = moves.revert(db, batch_id, deps.now)
    now_reverted = set(r['Seq'] for r in db.select(
        "SELECT Seq FROM orphan_moves WHERE BatchID=? AND Status='reverted'", [batch_id]))
    back = [(before[s],) for s in before if s in now_reverted]
    if back:
        db.action("UPDATE orphans SET Status='identified', IssueID=NULL WHERE OrphanID=?",
                  back, executemany=True)
    group_status = 'partial' if result.skipped else 'reverted'
    db.action("UPDATE orphan_groups SET Status=? WHERE GroupID IN"
              " (SELECT DISTINCT GroupID FROM orphan_moves WHERE BatchID=?"
              " AND GroupID IS NOT NULL)", [group_status, batch_id])
    rows = db.select('SELECT ComicID FROM orphan_moves WHERE BatchID=? LIMIT 1', [batch_id])
    if rows and rows[0]['ComicID']:
        deps.rescan(rows[0]['ComicID'])
    deps.log('[ORPHANS] Reverted batch %s: %d put back, %d skipped%s' % (
        batch_id, result.reverted, len(result.skipped),
        ''.join('; %s (%s)' % s for s in result.skipped)))
    return result


def revert_run(db, run_id, deps):
    """Undo every batch of a run, newest first."""
    batch_ids = [r['BatchID'] for r in db.select(
        'SELECT BatchID, MAX(WhenDone) AS w FROM orphan_moves WHERE RunID=?'
        ' GROUP BY BatchID ORDER BY w DESC', [run_id])]
    return [revert_batch(db, b, deps) for b in batch_ids]
