"""Issue-event queue worker.

Batches writes onto one thread so the search/snatch/post-process paths never
touch the database to record an event. Mirrors the shape of the other workers
in this package: block on the queue, honour the 'exit' sentinel, hold a single
DBConnection for the life of the thread.
"""

import time

import mylar
from .. import logger

from mylar import db

INSERT = (
    'INSERT INTO issue_events '
    '(IssueID, ComicID, EventType, EventTime, Provider, Detail, Progress, QueuePosition, Extra) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
)

COLUMNS = ('IssueID', 'ComicID', 'EventType', 'EventTime', 'Provider',
           'Detail', 'Progress', 'QueuePosition', 'Extra')

# How many events to take in one pass. Keeps a burst (a mass-search snatching
# fifty issues) to a couple of transactions instead of fifty.
BATCH_LIMIT = 200

# Prune on startup and every N batches thereafter. Deliberately not per-insert:
# a retention DELETE scans the table, and doing that on every snatch is how a
# feature meant to be invisible becomes the reason the database is busy.
PRUNE_EVERY = 50


def _debug(message):
    """Log, but never let logging be the thing that kills the writer.

    logger.fdebug consults config for the log level and raises if that is not
    populated, which would take down the thread from inside the very handler
    meant to keep it alive.
    """
    try:
        logger.fdebug(message)
    except Exception:
        pass


def drain(queue, limit=BATCH_LIMIT):
    """Take up to `limit` items already sitting in the queue.

    Stops early on the 'exit' sentinel and includes it, so the caller can see
    the shutdown request without losing the events queued ahead of it.
    """
    batch = []
    while len(batch) < limit:
        try:
            item = queue.get_nowait()
        except Exception:
            break
        batch.append(item)
        if item == 'exit':
            break
    return batch


def write_events(myDB, entries):
    """Insert a batch. Returns how many rows were written."""
    rows = [tuple(entry[column] for column in COLUMNS) for entry in entries]
    if not rows:
        return 0
    myDB.action(INSERT, rows, executemany=True)
    return len(rows)


def prune(myDB, retention_days):
    """Drop events past the retention window."""
    try:
        days = int(retention_days)
    except (TypeError, ValueError):
        return
    if days <= 0:
        return
    myDB.action(
        "DELETE FROM issue_events WHERE EventTime < datetime('now', ?)",
        ['-%d days' % days],
    )


def event_monitor(queue):
    myDB = db.DBConnection()
    batches = 0

    try:
        prune(myDB, mylar.CONFIG.ISSUE_EVENTS_RETENTION_DAYS)
    except Exception as e:
        _debug('[ISSUE-EVENTS] Could not prune on startup: %s' % e)

    while True:
        if queue.qsize() == 0:
            time.sleep(1)
            continue

        batch = drain(queue)
        shutting_down = 'exit' in batch
        entries = [item for item in batch if item != 'exit']

        if entries:
            try:
                write_events(myDB, entries)
            except Exception as e:
                # Losing events is acceptable; taking the thread down is not,
                # because record() would then silently fill a bounded queue.
                _debug('[ISSUE-EVENTS] Could not write %s events: %s'
                       % (len(entries), e))

            batches += 1
            if batches % PRUNE_EVERY == 0:
                try:
                    prune(myDB, mylar.CONFIG.ISSUE_EVENTS_RETENTION_DAYS)
                except Exception as e:
                    _debug('[ISSUE-EVENTS] Could not prune: %s' % e)

        if shutting_down:
            try:
                logger.info('[ISSUE-EVENTS] Cleaning up workers for shutdown')
            except Exception:
                pass
            break
