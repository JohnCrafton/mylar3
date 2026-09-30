"""A stand-in for mylar.db.DBConnection over an in-memory database.

Only action() and select() - the two calls the orphan batch code makes -
with the same shapes: action commits, select returns sqlite3.Row objects.
"""
import os
import sqlite3

from mylar import orphan_batches

# Copied from mylar/__init__.py dbcheck() so these tests exercise the real
# columns; GroupID is added by apply_schema, as it is in production.
ORPHANS_DDL = (
    'CREATE TABLE orphans(OrphanID TEXT UNIQUE, FilePath TEXT UNIQUE, FileName TEXT,'
    ' FileSize INTEGER, PageCount INTEGER, ParsedSeries TEXT, ParsedIssue TEXT,'
    ' ParsedYear TEXT, ParsedVolume TEXT, BookType TEXT, HasComicInfo INTEGER,'
    ' ComicInfoSeries TEXT, TotalIssues INTEGER, CVSuggestions TEXT, Status TEXT,'
    ' ScanDate TEXT, ComicID TEXT, IssueID TEXT)')


class FakeDB(object):
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        cursor = self.connection.cursor()
        cursor.execute(ORPHANS_DDL)
        cursor.execute('CREATE TABLE comics (ComicID TEXT, ComicName TEXT,'
                       ' ComicYear TEXT, ComicLocation TEXT, Status TEXT)')
        cursor.execute('CREATE TABLE issues (IssueID TEXT, ComicID TEXT,'
                       ' Issue_Number TEXT, Location TEXT)')
        cursor.execute('CREATE TABLE snatched (IssueID TEXT, ComicID TEXT,'
                       ' ComicName TEXT, Issue_Number TEXT, Size INTEGER,'
                       ' DateAdded TEXT, Status TEXT, Provider TEXT, FolderName TEXT)')
        orphan_batches.apply_schema(cursor)
        self.connection.commit()

    def action(self, query, args=None, executemany=False):
        if executemany:
            result = self.connection.executemany(query, args)
        else:
            result = self.connection.execute(query, args or [])
        self.connection.commit()
        return result

    def select(self, query, args=None):
        return self.connection.execute(query, args or []).fetchall()

    def add_orphans(self, rows):
        for row in rows:
            columns = sorted(row)
            self.action('INSERT INTO orphans (%s) VALUES (%s)' % (
                ', '.join(columns), ', '.join(['?'] * len(columns))),
                [row[c] for c in columns])


def orphan(path, **fields):
    """An orphan row as the scan records it, with only what a test cares about."""
    row = {
        'OrphanID': fields.pop('OrphanID', os.path.basename(path)),
        'FilePath': path,
        'FileName': os.path.basename(path),
        'FileSize': 0,
        'ParsedSeries': None,
        'ParsedIssue': None,
        'ParsedYear': None,
        'ParsedVolume': None,
        'Status': 'new',
    }
    row.update(fields)
    return row
