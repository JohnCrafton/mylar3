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

"""Moving orphans on disk, with a log written first so any of it can be undone.

Every move is planned into orphan_moves before a file is touched, marked as it
happens, and reverted from the same rows. Only os.rename is used: on one
filesystem it is instant and atomic, and anything that would need a copy is
refused rather than carried out as copy-then-delete.
"""

import collections
import errno
import os

MoveResult = collections.namedtuple('MoveResult', 'done failed stopped')
RevertResult = collections.namedtuple('RevertResult', 'reverted skipped')

_INSERT = ('INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, GroupID, OrphanID,'
           ' IssueID, Kind, Source, Destination, Status, Error, WhenDone)'
           ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')


def record_plan(db, run_id, batch_id, comicid, groupid_of, moves, when):
    """Write every planned move before any of them happens."""
    rows = [(batch_id, seq, run_id, comicid, groupid_of.get(m.orphanid), m.orphanid,
             m.issueid, m.kind, m.source, m.destination, 'planned', None, when)
            for seq, m in enumerate(moves)]
    if rows:
        db.action(_INSERT, rows, executemany=True)
    return len(rows)


def _mark(db, batch_id, seq, status, error, when):
    db.action('UPDATE orphan_moves SET Status=?, Error=?, WhenDone=?'
              ' WHERE BatchID=? AND Seq=?', [status, error, when, batch_id, seq])


def _note(db, batch_id, seq, error):
    db.action('UPDATE orphan_moves SET Error=? WHERE BatchID=? AND Seq=?',
              [error, batch_id, seq])


def _move_one(source, destination, rename, was_moving=False):
    """None when the file is at destination afterwards, else why not.

    was_moving indicates if the row's Status was already 'moving' when called,
    meaning a rename attempt started before a crash. Only in that case do we
    treat a missing source + existing destination as success (crash recovery).
    """
    if not os.path.lexists(source):
        if os.path.isfile(destination) and was_moving:
            # renamed before a crash could record it; this is recovery
            return None
        return 'The file is no longer at %s' % source
    if os.path.lexists(destination):
        return 'Something is already at %s' % destination
    try:
        parent = os.path.dirname(destination)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        # Check-then-rename window: another writer could create destination between
        # the check above and this rename. POSIX rename replaces atomically, so we
        # accept the race and replace.
        rename(source, destination)
    except OSError as e:
        if e.errno == errno.EXDEV:
            return ('%s is on a different filesystem from %s - refused rather'
                    ' than copying' % (source, destination))
        return str(e)
    return None


def execute(db, batch_id, now, stop=lambda: False, rename=os.rename):
    """Carry out a batch's planned (or previously failed) moves, in order.

    stop is asked before each file; answering True leaves the rest planned for
    a later call to pick up.
    """
    rows = db.select("SELECT Seq, Source, Destination, Status FROM orphan_moves"
                     " WHERE BatchID=? AND Kind IN ('move', 'park')"
                     " AND Status IN ('planned', 'failed', 'moving') ORDER BY Seq", [batch_id])
    done = failed = 0
    for row in rows:
        if stop():
            return MoveResult(done, failed, True)
        was_moving = row['Status'] == 'moving'
        # Mark the row as 'moving' to record that a rename attempt is starting
        _mark(db, batch_id, row['Seq'], 'moving', None, now())
        error = _move_one(row['Source'], row['Destination'], rename, was_moving=was_moving)
        _mark(db, batch_id, row['Seq'], 'failed' if error else 'done', error, now())
        if error:
            failed += 1
        else:
            done += 1
    return MoveResult(done, failed, False)


def _next_seq(db, batch_id):
    row = db.select('SELECT COALESCE(MAX(Seq), -1) + 1 AS n FROM orphan_moves'
                    ' WHERE BatchID=?', [batch_id])[0]
    return row['n']


def remove_empty_dirs(db, batch_id, library_root, now, rmdir=os.rmdir):
    """Remove folders the batch emptied, strictly inside the library.

    A folder still holding anything - a cover.jpg, an .nfo, another comic -
    stays. Each removal is logged so revert can recreate it.
    """
    rows = db.select("SELECT Source, RunID, ComicID FROM orphan_moves WHERE BatchID=?"
                     " AND Status='done' AND Kind IN ('move', 'park')", [batch_id])
    if not rows or not library_root:
        return []
    inside = os.path.join(os.path.abspath(library_root), '')

    candidates = set()
    for row in rows:
        folder = os.path.dirname(os.path.abspath(row['Source']))
        while folder.startswith(inside):
            candidates.add(folder)
            parent = os.path.dirname(folder)
            if parent == folder:
                # dirname(folder) == folder means we hit the root; stop climbing
                break
            folder = parent

    removed = []
    for folder in sorted(candidates, key=lambda p: -p.count(os.sep)):
        try:
            if os.path.isdir(folder) and not os.listdir(folder):
                rmdir(folder)
                removed.append(folder)
        except OSError:
            continue

    if removed:
        first = _next_seq(db, batch_id)
        db.action(_INSERT, [(batch_id, first + i, rows[0]['RunID'], rows[0]['ComicID'],
                             None, None, None, 'rmdir', folder, None, 'done', None, now())
                            for i, folder in enumerate(removed)], executemany=True)
    return removed


def revert(db, batch_id, now, rename=os.rename):
    """Undo a batch's completed rows, last first.

    Never overwrites: a file whose original path has been taken since stays
    where it was filed, and is reported.
    """
    rows = db.select("SELECT Seq, Kind, Source, Destination FROM orphan_moves"
                     " WHERE BatchID=? AND Status='done' ORDER BY Seq DESC", [batch_id])
    reverted = 0
    skipped = []
    for row in rows:
        if row['Kind'] == 'rmdir':
            if not os.path.isdir(row['Source']):
                try:
                    os.makedirs(row['Source'])
                except OSError as e:
                    skipped.append((row['Source'], str(e)))
                    _note(db, batch_id, row['Seq'], str(e))
                    continue
            _mark(db, batch_id, row['Seq'], 'reverted', None, now())
            continue

        source, destination = row['Source'], row['Destination']
        if os.path.lexists(source):
            reason = 'original path is occupied'
        elif not os.path.isfile(destination):
            reason = 'the filed copy is no longer at %s' % destination
        else:
            reason = None
            try:
                parent = os.path.dirname(source)
                if not os.path.isdir(parent):
                    os.makedirs(parent)
                rename(destination, source)
            except OSError as e:
                reason = str(e)

        if reason:
            skipped.append((source, reason))
            _note(db, batch_id, row['Seq'], reason)
        else:
            _mark(db, batch_id, row['Seq'], 'reverted', None, now())
            reverted += 1

    # After restoring all done rows, cancel any remaining rows that are still
    # planned/failed/moving so a later execute() can't re-move files of a
    # reverted batch
    db.action('UPDATE orphan_moves SET Status=? WHERE BatchID=?'
              ' AND Kind IN (\'move\', \'park\') AND Status IN (\'planned\', \'failed\', \'moving\')',
              ['cancelled', batch_id])

    return RevertResult(reverted, tuple(skipped))


def batch_counts(db, batch_id):
    """{status: count} over a batch's file rows (folder removals not counted)."""
    return dict((r['Status'], r['n']) for r in db.select(
        "SELECT Status, COUNT(*) AS n FROM orphan_moves WHERE BatchID=?"
        " AND Kind IN ('move', 'park') GROUP BY Status", [batch_id]))
