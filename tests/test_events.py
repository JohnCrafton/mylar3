import queue
import time

import pytest

import mylar
from mylar import events
from mylar.queues import events as events_queue


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(mylar, "CONFIG", mylar.config.Config("./nothing"))
    monkeypatch.setattr(mylar.CONFIG, "ENABLE_ISSUE_EVENTS", True, raising=False)
    monkeypatch.setattr(mylar.CONFIG, "ISSUE_EVENTS_RETENTION_DAYS", 60, raising=False)
    monkeypatch.setattr(mylar, "EVENT_QUEUE", queue.Queue(maxsize=10), raising=False)
    return mylar.EVENT_QUEUE


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(mylar, "CONFIG", mylar.config.Config("./nothing"))
    monkeypatch.setattr(mylar.CONFIG, "ENABLE_ISSUE_EVENTS", False, raising=False)
    monkeypatch.setattr(mylar, "EVENT_QUEUE", queue.Queue(maxsize=10), raising=False)
    return mylar.EVENT_QUEUE


# --- parsing -------------------------------------------------------------
# make_event is the boundary: it either returns a complete, valid row or None.
# Everything downstream may assume the fields are present.

@pytest.mark.unit
def test_make_event_returns_a_complete_row():
    e = events.make_event('123', comicid='456', event='snatched',
                          provider='32P', detail='Some.Release.cbz')
    assert e['IssueID'] == '123'
    assert e['ComicID'] == '456'
    assert e['EventType'] == 'snatched'
    assert e['Provider'] == '32P'
    assert e['Detail'] == 'Some.Release.cbz'
    assert e['EventTime']
    # reserved for the later download-client polling feature
    assert e['Progress'] is None
    assert e['QueuePosition'] is None


@pytest.mark.unit
@pytest.mark.parametrize("event", ['nonsense', '', None, 'SNATCHED', 'drop table'])
def test_make_event_rejects_unknown_event_types(event):
    assert events.make_event('123', event=event) is None


@pytest.mark.unit
@pytest.mark.parametrize("issueid", [None, '', 0])
def test_make_event_requires_an_issueid(issueid):
    assert events.make_event(issueid, event='snatched') is None


@pytest.mark.unit
def test_make_event_coerces_ids_to_strings():
    e = events.make_event(123, comicid=456, event='snatched')
    assert e['IssueID'] == '123'
    assert e['ComicID'] == '456'


@pytest.mark.unit
def test_every_event_type_is_accepted():
    for name in events.EVENT_TYPES:
        assert events.make_event('1', event=name) is not None


# --- recording -----------------------------------------------------------
# record() is called from inside the search/snatch/post-process path. Its one
# hard requirement is that it never raises and never blocks, whatever happens.

@pytest.mark.unit
def test_record_enqueues_when_enabled(enabled):
    assert events.record('123', comicid='456', event='snatched', provider='32P') is True
    assert enabled.qsize() == 1
    assert enabled.get_nowait()['EventType'] == 'snatched'


@pytest.mark.unit
def test_record_is_a_noop_when_disabled(disabled):
    assert events.record('123', comicid='456', event='snatched') is False
    assert disabled.qsize() == 0


@pytest.mark.unit
def test_record_drops_invalid_events_without_raising(enabled):
    assert events.record('123', event='not_a_real_event') is False
    assert events.record(None, event='snatched') is False
    assert enabled.qsize() == 0


@pytest.mark.unit
def test_record_drops_rather_than_blocking_when_the_queue_is_full(monkeypatch):
    monkeypatch.setattr(mylar, "CONFIG", mylar.config.Config("./nothing"))
    monkeypatch.setattr(mylar.CONFIG, "ENABLE_ISSUE_EVENTS", True, raising=False)
    full = queue.Queue(maxsize=1)
    full.put_nowait({'already': 'there'})
    monkeypatch.setattr(mylar, "EVENT_QUEUE", full, raising=False)

    # a blocking put here would stall whichever download thread called it
    assert events.record('123', event='snatched') is False
    assert full.qsize() == 1


@pytest.mark.unit
def test_record_survives_a_broken_config(monkeypatch):
    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError('config is gone')

    monkeypatch.setattr(mylar, "CONFIG", Exploding())
    assert events.record('123', event='snatched') is False


@pytest.mark.unit
def test_record_survives_a_broken_queue(monkeypatch):
    class Exploding:
        def put_nowait(self, item):
            raise RuntimeError('queue is gone')

    monkeypatch.setattr(mylar, "CONFIG", mylar.config.Config("./nothing"))
    monkeypatch.setattr(mylar.CONFIG, "ENABLE_ISSUE_EVENTS", True, raising=False)
    monkeypatch.setattr(mylar, "EVENT_QUEUE", Exploding(), raising=False)
    assert events.record('123', event='snatched') is False


@pytest.mark.unit
def test_record_survives_a_missing_queue(monkeypatch):
    monkeypatch.setattr(mylar, "CONFIG", mylar.config.Config("./nothing"))
    monkeypatch.setattr(mylar.CONFIG, "ENABLE_ISSUE_EVENTS", True, raising=False)
    monkeypatch.setattr(mylar, "EVENT_QUEUE", None, raising=False)
    assert events.record('123', event='snatched') is False


# --- the worker ----------------------------------------------------------

@pytest.mark.unit
def test_drain_takes_everything_currently_queued():
    q = queue.Queue()
    for i in range(5):
        q.put_nowait({'n': i})
    assert events_queue.drain(q, limit=10) == [{'n': i} for i in range(5)]
    assert q.qsize() == 0


@pytest.mark.unit
def test_drain_respects_its_limit():
    q = queue.Queue()
    for i in range(5):
        q.put_nowait({'n': i})
    assert len(events_queue.drain(q, limit=2)) == 2
    assert q.qsize() == 3


@pytest.mark.unit
def test_drain_stops_at_the_exit_sentinel():
    q = queue.Queue()
    q.put_nowait({'n': 0})
    q.put_nowait('exit')
    q.put_nowait({'n': 1})
    batch = events_queue.drain(q, limit=10)
    assert batch == [{'n': 0}, 'exit']


# --- persistence ---------------------------------------------------------
# Against a real sqlite file rather than a fake: the retention window is
# expressed in SQL, so a mock would only prove the mock works.

@pytest.fixture
def db_conn(monkeypatch, tmp_path):
    from mylar import db
    monkeypatch.setattr(mylar, "DATA_DIR", str(tmp_path), raising=False)
    conn = db.DBConnection()
    conn.action(
        'CREATE TABLE IF NOT EXISTS issue_events('
        'EventID INTEGER PRIMARY KEY AUTOINCREMENT, IssueID TEXT, ComicID TEXT, '
        'EventType TEXT, EventTime TEXT, Provider TEXT, Detail TEXT, '
        'Progress INTEGER, QueuePosition INTEGER, Extra TEXT)'
    )
    return conn


@pytest.mark.integration
def test_write_events_persists_a_batch(db_conn):
    batch = [
        events.make_event('1', comicid='9', event='search_started'),
        events.make_event('1', comicid='9', event='snatched', provider='32P'),
        events.make_event('1', comicid='9', event='download_complete'),
    ]
    assert events_queue.write_events(db_conn, batch) == 3

    rows = db_conn.select('SELECT * FROM issue_events ORDER BY EventID')
    assert [r['EventType'] for r in rows] == [
        'search_started', 'snatched', 'download_complete'
    ]
    assert rows[1]['Provider'] == '32P'
    assert rows[0]['Progress'] is None


@pytest.mark.integration
def test_write_events_handles_an_empty_batch(db_conn):
    assert events_queue.write_events(db_conn, []) == 0


@pytest.mark.integration
def test_prune_drops_only_events_past_the_window(db_conn):
    def insert(issueid, when):
        db_conn.action(
            'INSERT INTO issue_events (IssueID, EventType, EventTime) VALUES (?, ?, ?)',
            [issueid, 'snatched', when],
        )

    insert('old', "2001-01-01 00:00:00")
    db_conn.action(
        "INSERT INTO issue_events (IssueID, EventType, EventTime) "
        "VALUES ('recent', 'snatched', datetime('now', '-1 days'))"
    )

    events_queue.prune(db_conn, 60)

    remaining = [r['IssueID'] for r in db_conn.select('SELECT IssueID FROM issue_events')]
    assert remaining == ['recent']


@pytest.mark.integration
@pytest.mark.parametrize("bad", [None, 'sixty', -1, 0])
def test_prune_ignores_a_nonsense_retention_and_deletes_nothing(db_conn, bad):
    db_conn.action(
        "INSERT INTO issue_events (IssueID, EventType, EventTime) "
        "VALUES ('keep', 'snatched', '2001-01-01 00:00:00')"
    )
    events_queue.prune(db_conn, bad)
    assert len(db_conn.select('SELECT IssueID FROM issue_events')) == 1


@pytest.mark.integration
def test_a_recorded_event_survives_the_whole_round_trip(enabled, db_conn):
    events.record('42', comicid='7', event='snatched', provider='DDL',
                  detail='Batman 001.cbz')
    batch = events_queue.drain(mylar.EVENT_QUEUE)
    events_queue.write_events(db_conn, batch)

    row = db_conn.select('SELECT * FROM issue_events')[0]
    assert (row['IssueID'], row['ComicID'], row['EventType'],
            row['Provider'], row['Detail']) == ('42', '7', 'snatched', 'DDL', 'Batman 001.cbz')


@pytest.mark.integration
def test_worker_thread_writes_then_exits_on_the_sentinel(monkeypatch, db_conn, enabled):
    """The worker is a daemon thread in production; this runs the real loop."""
    import threading

    q = mylar.EVENT_QUEUE
    for n in range(3):
        events.record(str(n), comicid='9', event='snatched', provider='32P')

    thread = threading.Thread(target=events_queue.event_monitor, args=(q,), daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        if len(db_conn.select('SELECT EventID FROM issue_events')) == 3:
            break
        time.sleep(0.1)

    assert len(db_conn.select('SELECT EventID FROM issue_events')) == 3

    q.put_nowait('exit')
    thread.join(timeout=10)
    assert thread.is_alive() is False


@pytest.mark.integration
def test_worker_survives_a_write_failure_and_keeps_running(monkeypatch, db_conn, enabled):
    """A dead writer is worse than a lost event: record() would then quietly
    fill the bounded queue and every later event would be dropped too."""
    import threading

    calls = {'n': 0}
    real_write = events_queue.write_events

    def flaky(myDB, entries):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('database went away')
        return real_write(myDB, entries)

    monkeypatch.setattr(events_queue, 'write_events', flaky)

    q = mylar.EVENT_QUEUE
    thread = threading.Thread(target=events_queue.event_monitor, args=(q,), daemon=True)
    thread.start()

    events.record('1', event='snatched')       # this batch is lost
    deadline = time.time() + 5
    while time.time() < deadline and calls['n'] < 1:
        time.sleep(0.05)

    events.record('2', event='snatched')       # this one must still land
    deadline = time.time() + 10
    while time.time() < deadline:
        if db_conn.select("SELECT EventID FROM issue_events WHERE IssueID='2'"):
            break
        time.sleep(0.1)

    assert db_conn.select("SELECT EventID FROM issue_events WHERE IssueID='2'")
    assert thread.is_alive() is True

    q.put_nowait('exit')
    thread.join(timeout=10)


@pytest.mark.unit
def test_every_event_type_has_a_label():
    """A type without a label renders as its raw slug in the timeline."""
    assert set(events.EVENT_TYPES) == set(events.EVENT_LABELS)


# --- activity feed sorting ----------------------------------------------
# iSortCol_0 comes off the query string and lands inside ORDER BY.

@pytest.mark.unit
@pytest.mark.parametrize("col,expected", [
    ('0', 'e.EventTime'), ('1', 'c.ComicName'), ('2', 'e.EventType'), ('3', 'e.Provider'),
])
def test_activity_order_by_maps_known_columns(col, expected):
    assert events.activity_order_by(col, 'desc').startswith(expected + ' DESC')


@pytest.mark.unit
@pytest.mark.parametrize("col", ['9', '', None, 'ComicName', '1; DROP TABLE issue_events'])
def test_activity_order_by_rejects_anything_unrecognised(col):
    assert events.activity_order_by(col, 'desc') == 'e.EventTime DESC, e.EventID DESC'


@pytest.mark.unit
@pytest.mark.parametrize("direction,expected", [
    ('asc', 'ASC'), ('ASC', 'ASC'), ('desc', 'DESC'), ('nonsense', 'DESC'), (None, 'DESC'),
])
def test_activity_order_by_only_ever_emits_asc_or_desc(direction, expected):
    clause = events.activity_order_by('0', direction)
    assert clause == 'e.EventTime %s, e.EventID %s' % (expected, expected)


@pytest.mark.unit
def test_activity_order_by_always_breaks_ties_on_eventid():
    """Events for one issue can share a timestamp; without a tiebreak their
    order flips between queries and the feed looks unstable."""
    for col in events.ACTIVITY_SORT_COLUMNS:
        assert events.activity_order_by(col, 'desc').endswith('e.EventID DESC')
