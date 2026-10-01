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
        _written(db.action(_INSERT, rows, executemany=True), 'the plan for batch %s' % batch_id)
    return len(rows)


def _written(result, what):
    # mylar.db returns None rather than raising when a write fails; a file moved
    # without its row is a file revert cannot find, so stop here instead
    if result is None:
        raise RuntimeError('Could not record %s' % what)
    return result


def _mark(db, batch_id, seq, status, error, when):
    _written(db.action('UPDATE orphan_moves SET Status=?, Error=?, WhenDone=?'
                       ' WHERE BatchID=? AND Seq=?', [status, error, when, batch_id, seq]),
             'move %s of batch %s as %s' % (seq, batch_id, status))


def _note(db, batch_id, seq, error):
    _written(db.action('UPDATE orphan_moves SET Error=? WHERE BatchID=? AND Seq=?',
                       [error, batch_id, seq]),
             'a note on move %s of batch %s' % (seq, batch_id))


def sync_folders(*folders):
    """Flush each folder's entries to disk.

    A rename lives in the folders, not the file; until they are flushed a
    crash can undo it after the log already says it happened.
    """
    for folder in sorted(set(folders)):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


REASONS = {
    'missing': 'not at logged path',
    'no_keeper_record': 'issue has no recorded file',
    'keeper_missing': 'kept file missing',
    'same_file': 'is the kept file',
    'outside': 'outside _duplicates',
}


def deletable(destination, keeper, duplicates_root):
    """None when a parked copy may be deleted, else the reason it stays.

    keeper is the file Mylar records for the issue the copy duplicates, or
    None when it records none. Checked against the disk as it is now: the log
    can be out of date (another tool may have renamed either file since).
    """
    if not os.path.isfile(destination) or os.path.islink(destination):
        return REASONS['missing']
    inside = os.path.join(os.path.realpath(duplicates_root), '')
    if not os.path.realpath(destination).startswith(inside):
        return REASONS['outside']
    if keeper is None:
        return REASONS['no_keeper_record']
    if not os.path.isfile(keeper):
        return REASONS['keeper_missing']
    try:
        if (os.path.realpath(keeper) == os.path.realpath(destination)
                or os.path.samefile(keeper, destination)):
            return REASONS['same_file']
    except OSError:
        # If samefile or realpath raises (e.g. keeper vanished between isfile check
        # and samefile), fail closed: treat keeper as missing
        return REASONS['keeper_missing']
    return None


DeleteResult = collections.namedtuple('DeleteResult', 'deleted kept bytes stopped')


def _parked_rows(db, batch_ids):
    marks = ', '.join('?' * len(batch_ids))
    return db.select("SELECT BatchID, Seq, OrphanID, IssueID, Destination, Status"
                     " FROM orphan_moves WHERE BatchID IN (%s) AND Kind='park'"
                     " AND Status IN ('done', 'deleting') ORDER BY BatchID, Seq" % marks,
                     list(batch_ids))


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def preview_delete(db, batch_ids, keeper_for, duplicates_root):
    """What delete_parked would do now, without doing it."""
    files = size = 0
    kept = []
    for row in _parked_rows(db, batch_ids):
        destination = row['Destination']
        if row['Status'] == 'deleting' and not os.path.lexists(destination):
            files += 1
            continue
        reason = deletable(destination, keeper_for(row), duplicates_root)
        if reason:
            kept.append([destination, reason])
        else:
            files += 1
            size += _size(destination)
    return {'files': files, 'bytes': size, 'kept': kept}


def delete_parked(db, batch_id, now, keeper_for, duplicates_root, stop=lambda: False,
                  unlink=os.unlink, sync=sync_folders):
    """Delete a batch's parked copies that pass deletable, logging each first.

    A 'deleting' row is one a crash interrupted: gone means finish it, still
    there means check and delete it again. A failed sync raises and leaves
    the row 'deleting' for the next run to settle.
    """
    deleted = size = 0
    kept = []
    for row in _parked_rows(db, [batch_id]):
        if stop():
            return DeleteResult(deleted, tuple(kept), size, True)
        destination = row['Destination']
        if not (row['Status'] == 'deleting' and not os.path.lexists(destination)):
            reason = deletable(destination, keeper_for(row), duplicates_root)
            if reason:
                kept.append((destination, reason))
                continue
            _mark(db, batch_id, row['Seq'], 'deleting', None, now())
            freed = _size(destination)
            unlink(destination)
            sync(os.path.dirname(destination))
            size += freed
        _mark(db, batch_id, row['Seq'], 'deleted', None, now())
        _written(db.action("UPDATE orphans SET Status='deleted' WHERE OrphanID=?",
                           [row['OrphanID']]), 'orphan %s as deleted' % row['OrphanID'])
        deleted += 1
    return DeleteResult(deleted, tuple(kept), size, False)


def remove_empty_folders(paths, root, rmdir=os.rmdir):
    """Remove folders emptied under root, deepest first; never root itself."""
    inside = os.path.join(os.path.abspath(root), '')
    candidates = set()
    for path in paths:
        folder = os.path.dirname(os.path.abspath(path))
        while folder.startswith(inside):
            candidates.add(folder)
            folder = os.path.dirname(folder)
    removed = []
    for folder in sorted(candidates, key=lambda p: (-p.count(os.sep), p)):
        try:
            if os.path.isdir(folder) and not os.listdir(folder):
                rmdir(folder)
                removed.append(folder)
        except OSError:
            continue
    return removed


def _move_one(source, destination, rename, was_moving=False):
    """None when the file is at destination afterwards, else why not.

    was_moving indicates if the row's Status was already 'moving' when called,
    meaning a rename attempt started before a crash. Only in that case do we
    treat a missing source + existing destination as success (crash recovery).
    """
    if (os.path.abspath(source) == os.path.abspath(destination)
            and os.path.isfile(source)):
        return None           # already where it would be filed
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


def execute(db, batch_id, now, stop=lambda: False, rename=os.rename, sync=sync_folders):
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
        if not error:
            # a failure here leaves the row 'moving' for crash recovery to settle
            sync(os.path.dirname(row['Source']), os.path.dirname(row['Destination']))
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


def revert(db, batch_id, now, rename=os.rename, sync=sync_folders):
    """Undo a batch's completed rows, last first.

    Never overwrites: a file whose original path has been taken since stays
    where it was filed, and is reported.

    A 'moving' row is one a crash interrupted. If its file is still at the
    source the rename never happened and the row is cancelled with the
    planned ones; otherwise it is put back like a done row.
    """
    rows = db.select("SELECT Seq, Kind, Source, Destination, Status FROM orphan_moves"
                     " WHERE BatchID=? AND Status IN ('done', 'moving') ORDER BY Seq DESC",
                     [batch_id])
    reverted = 0
    skipped = []
    for row in rows:
        if row['Status'] == 'moving' and os.path.lexists(row['Source']):
            continue          # never renamed; cancelled with the planned rows
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
        if os.path.abspath(source) == os.path.abspath(destination):
            reason = None     # filed where it already was; nothing to move back
        elif os.path.lexists(source):
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
            else:
                # outside the try: the file did move, so a failed sync must not
                # be reported as a file that could not be put back
                sync(os.path.dirname(source), os.path.dirname(destination))

        if reason:
            skipped.append((source, reason))
            _note(db, batch_id, row['Seq'], reason)
        else:
            _mark(db, batch_id, row['Seq'], 'reverted', None, now())
            reverted += 1

    # After restoring all done rows, cancel any remaining rows that are still
    # planned/failed/moving so a later execute() can't re-move files of a
    # reverted batch
    _written(db.action('UPDATE orphan_moves SET Status=? WHERE BatchID=?'
                       ' AND Kind IN (\'move\', \'park\')'
                       ' AND Status IN (\'planned\', \'failed\', \'moving\')',
                       ['cancelled', batch_id]),
             'the cancelled moves of batch %s' % batch_id)

    return RevertResult(reverted, tuple(skipped))


def batch_counts(db, batch_id):
    """{status: count} over a batch's file rows (folder removals not counted)."""
    return dict((r['Status'], r['n']) for r in db.select(
        "SELECT Status, COUNT(*) AS n FROM orphan_moves WHERE BatchID=?"
        " AND Kind IN ('move', 'park') GROUP BY Status", [batch_id]))
