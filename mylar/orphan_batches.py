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

"""Orphans a folder at a time: grouping, tiering, and planning moves.

A per-file workbench does not scale to a library of thousands of orphans, but
most of them sit in folders that already say what they are. This groups them,
decides which groups are safe to file without a person looking, and works out
exactly what filing a series would do.

Pure: no disk, no network, no database connection. orphan_moves does the disk,
orphan_jobs does the rest.
"""

import collections
import hashlib
import os
import re
import shutil
import sqlite3

from mylar import orphans

# Below this share of files agreeing on one series, a folder is a collection
# (a reading order, an arc) rather than a run, and is left to the per-file
# workbench.
MIXED_SHARE = 0.6

# The auto tier. Each threshold was measured against a real library of 5,409
# orphans before it was written down; see the design notes.
AUTO_SHARE = 0.95
AUTO_NUMBERED = 0.95
AUTO_IN_RUN = 0.95
AUTO_TITLE = 0.9

# ComicVine allows 200 requests an hour per resource and bans past that; one
# search every 20 seconds stays under it with room for Mylar's own calls.
IDENTIFY_MIN_PACE = 20
MAX_SEARCH_FAILURES = 3

DUPLICATES_DIR = '_duplicates'

_FOLDER_YEAR = re.compile(r'\(((?:18|19|20)\d{2})\)')
_COPY_SUFFIX = re.compile(r'\s*\(\d+\)$')

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS orphan_groups (GroupID TEXT PRIMARY KEY, Folder TEXT,'
    ' Volume TEXT, Evidence TEXT, Candidates TEXT, Tier TEXT, Failed TEXT,'
    ' ComicID TEXT, Status TEXT, IdentifiedDate TEXT)',
    'CREATE TABLE IF NOT EXISTS orphan_moves (BatchID TEXT, Seq INTEGER, RunID TEXT,'
    ' ComicID TEXT, GroupID TEXT, OrphanID TEXT, IssueID TEXT, Kind TEXT, Source TEXT,'
    ' Destination TEXT, Status TEXT, Error TEXT, WhenDone TEXT,'
    ' PRIMARY KEY (BatchID, Seq))',
    'CREATE INDEX IF NOT EXISTS idx_orphan_moves_status ON orphan_moves (BatchID, Status)',
    'CREATE INDEX IF NOT EXISTS idx_orphan_groups_status ON orphan_groups (Status, Tier)',
)

ADDED_COLUMNS = (
    ('orphans', 'GroupID', 'TEXT'),
)

Group = collections.namedtuple('Group', 'groupid folder volume orphans')
Evidence = collections.namedtuple('Evidence', 'series share numbers year folder_year files')


def apply_schema(cursor):
    """Create the batch tables and add columns to existing ones. Idempotent."""
    for ddl in SCHEMA:
        cursor.execute(ddl)
    for table, column, kind in ADDED_COLUMNS:
        try:
            cursor.execute('SELECT %s FROM %s LIMIT 1' % (column, table))
        except sqlite3.OperationalError:
            cursor.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, kind))


def group_id(folder, volume):
    """Stable across rescans: the same folder and volume is the same group."""
    key = '%s\0%s' % (folder, volume or '')
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


def group_orphans(rows):
    """Orphan rows grouped by (folder, parsed volume), as {groupid: Group}."""
    buckets = collections.OrderedDict()
    for row in rows:
        folder = os.path.dirname(row['FilePath'])
        volume = row.get('ParsedVolume') or None
        gid = group_id(folder, volume)
        buckets.setdefault(gid, (folder, volume, []))[2].append(row)
    return dict((gid, Group(gid, folder, volume, tuple(members)))
                for gid, (folder, volume, members) in buckets.items())


def evidence(group):
    """What a group's files say about themselves, taken together."""
    files = group.orphans
    names = collections.Counter(orphans.clean_series_query(r.get('ParsedSeries'))
                                for r in files if r.get('ParsedSeries'))
    names.pop('', None)
    names.pop(None, None)
    series, agreeing = names.most_common(1)[0] if names else (None, 0)

    numbers = tuple(orphans._as_issue_number(r.get('ParsedIssue')) for r in files)

    # "Batman/Volume 01 (1940)" names the run; the files' own years are cover
    # dates spread across it
    found = _FOLDER_YEAR.findall(group.folder)
    folder_year = found[-1] if found else None
    years = collections.Counter(r.get('ParsedYear') for r in files if r.get('ParsedYear'))
    year = folder_year or (years.most_common(1)[0][0] if years else None)

    return Evidence(series, float(agreeing) / len(files), numbers, year,
                    folder_year, len(files))


def _issue_text(number):
    if number is None:
        return None
    return '%d' % number if number == int(number) else str(number)


def search_parsed(ev):
    """The group as score_candidate expects a single file.

    The highest issue number stands in for the file's own, so the search drops
    series too short to contain it.
    """
    known = [n for n in ev.numbers if n is not None]
    return {
        'series': ev.series,
        'issue': _issue_text(max(known)) if known else None,
        'year': ev.year,
        'booktype': None,
        'page_count': None,
        'comicinfo_series': None,
        'issue_total': None,
    }


def failed_checks(ev, ranked):
    """The auto-tier checks this group fails, by name. Empty means auto."""
    top = ranked[0] if ranked else None
    run = orphans._as_int(top.get('issues')) if top else None
    in_run = (sum(1 for n in ev.numbers if n is not None and n <= run)
              / float(ev.files)) if run else 0.0
    numbered = sum(1 for n in ev.numbers if n is not None) / float(ev.files)

    checks = (
        ('auto_selection', orphans.auto_selection(ranked) is not None),
        ('title', bool(top) and
         orphans.title_similarity(ev.series, top.get('name')) >= AUTO_TITLE),
        # Strange Tales #101 is cover-dated 1962 but the series began in 1951;
        # a folder that says 1963 is naming something else, or naming it wrong
        ('year', ev.folder_year is None or
         (bool(top) and str(top.get('comicyear')) == ev.folder_year)),
        # a share, not the maximum: one revival issue past the listed run must
        # not send 280 good files to review
        ('run', in_run >= AUTO_IN_RUN),
        ('share', ev.share >= AUTO_SHARE),
        ('numbered', numbered >= AUTO_NUMBERED),
    )
    return [name for name, ok in checks if not ok]


def tier(ev, ranked):
    """('auto' | 'review' | 'mixed', failed check names)."""
    if not ev.series or ev.share < MIXED_SHARE:
        return 'mixed', []
    failed = failed_checks(ev, ranked)
    return ('auto' if not failed else 'review'), failed


def candidate_summary(ranked, n=5):
    """What the page needs of the top candidates, and nothing else."""
    keys = ('comicid', 'name', 'comicyear', 'issues', 'publisher', 'score', 'comicimage')
    return [dict((k, c.get(k)) for k in keys) for c in (ranked or [])[:n]]


Move = collections.namedtuple('Move', 'kind orphanid source destination issueid')
SeriesPlan = collections.namedtuple('SeriesPlan', 'moves skipped')


def keeper_rank(row):
    """Sort key for copies of one issue; the first is the one kept.

    Largest first (the fuller scan), then .cbz over .cbr, then the name
    without a "(1)" copy suffix, then the shorter name.
    """
    name = row.get('FileName') or os.path.basename(row['FilePath'])
    stem, ext = os.path.splitext(name)
    return (-(row.get('FileSize') or 0),
            0 if ext.lower() == '.cbz' else 1,
            1 if _COPY_SUFFIX.search(stem) else 0,
            len(name),
            name)


def park_destination(source, library_root):
    """Where a duplicate goes: its own path, mirrored under _duplicates/."""
    inside = source.startswith(os.path.join(library_root, ''))
    relative = (os.path.relpath(source, library_root) if inside
                else source.lstrip(os.sep))
    return os.path.join(library_root, DUPLICATES_DIR, relative)


def tracked_path(issue, series_dir):
    """The file Mylar records for an issue, normalised; None if it has none."""
    if not issue.get('Location'):
        return None
    return os.path.normpath(os.path.join(series_dir, issue['Location']))


def plan_series(files, issues, has_file, destination_dir, name_for, library_root):
    """Every move filing these files into one series would make.

    files:    orphan rows for the series (across all its groups)
    issues:   Mylar's issue rows for the series (IssueID, Issue_Number, Location)
    has_file: issue -> whether Mylar's recorded file for it is really on disk
    name_for: (orphan, issue) -> the filed file name

    A file whose number matches no issue is skipped - left an orphan - rather
    than guessed at. So is a file that is the one Mylar records for its issue:
    it is already filed, and parking it would take the issue's file away.
    """
    by_issue = collections.OrderedDict()
    skipped = []
    for row in files:
        issue = orphans.match_issue(issues, row.get('ParsedIssue'))
        if issue is None or os.path.normpath(row['FilePath']) == tracked_path(
                issue, destination_dir):
            skipped.append(row['OrphanID'])
            continue
        by_issue.setdefault(issue['IssueID'], (issue, []))[1].append(row)

    moves = []
    for issueid, (issue, rows) in by_issue.items():
        ranked = sorted(rows, key=keeper_rank)
        # an issue Mylar already has on disk keeps that file; every orphan copy
        # of it is a duplicate
        keeper = None if has_file(issue) else ranked[0]
        for row in ranked:
            if row is keeper:
                destination = os.path.join(destination_dir, name_for(row, issue))
                moves.append(Move('move', row['OrphanID'], row['FilePath'],
                                  destination, issueid))
            else:
                moves.append(Move('park', row['OrphanID'], row['FilePath'],
                                  park_destination(row['FilePath'], library_root),
                                  issueid))
    return SeriesPlan(tuple(moves), tuple(skipped))


def batch_blockers(enable_check_folder, check_folder, library_root, job_running):
    """Reasons not to start moving files, for the user to read. Empty means go."""
    blockers = []
    if job_running:
        blockers.append('Another orphan job is already running.')
    if not library_root:
        blockers.append('No Comic Location is set, so there is nowhere to file to.')
    elif enable_check_folder and check_folder:
        watched = os.path.join(os.path.abspath(check_folder), '')
        library = os.path.join(os.path.abspath(library_root), '')
        if library.startswith(watched):
            blockers.append(
                'The folder monitor watches %s, which contains the library. It'
                ' would post-process files while they are being moved. Point it'
                ' at your downloads folder first.' % check_folder)
    return blockers


def backup_database(path, stamp):
    """Copy the database beside itself before a run moves anything.

    sqlite's backup API, not a file copy: Mylar may be writing while it runs.
    Refuses unless there is room for two copies, and removes a half-written
    copy rather than leave it beside the real database.
    """
    target = '%s.orphans-%s' % (path, stamp)
    size = os.path.getsize(path)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
    if free < 2 * size:
        raise RuntimeError('Not enough free space to back up the database: %d bytes'
                           ' free beside %s, %d needed.' % (free, path, 2 * size))
    existed = os.path.exists(target)
    try:
        source = sqlite3.connect(path)
        try:
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
    except Exception:
        if not existed and os.path.exists(target):
            os.remove(target)
        raise
    return target
