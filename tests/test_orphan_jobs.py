import json
import threading
import time

import pytest

from mylar import orphan_jobs as oj
from tests.orphan_fakes import FakeDB, orphan


def _now():
    return '2026-09-30 12:00:00'


def _no_sleep(seconds):
    pass


def _fables(n=3, folder='/lib/Fables/Volume 01 (2002)'):
    return [orphan('%s/Fables %03d.cbz' % (folder, i), OrphanID='%s#%d' % (folder, i),
                   ParsedSeries='Fables', ParsedIssue='%03d' % i, ParsedYear='2002')
            for i in range(1, n + 1)]


def _fables_result():
    return [{'comicid': '25543', 'name': 'Fables', 'comicyear': '2002', 'issues': '161',
             'publisher': 'Vertigo', 'comicimage': 'http://x/f.jpg'}]


def _groups(db):
    return dict((r['Folder'], dict(r)) for r in db.select('SELECT * FROM orphan_groups'))


@pytest.mark.integration
def test_identify_records_tiers_and_links_orphans_to_their_group():
    db = FakeDB()
    db.add_orphans(_fables() + [
        orphan('/lib/Arc/a.cbz', OrphanID='m1', ParsedSeries='Superman'),
        orphan('/lib/Arc/b.cbz', OrphanID='m2', ParsedSeries='Doomsday')])
    searched = []

    def search(series):
        searched.append(series)
        return _fables_result()

    summary = oj.identify_groups(db, search, _now, _no_sleep, 20)

    assert summary == {'auto': 1, 'review': 0, 'mixed': 1, 'stopped': False, 'reason': None}
    # issue and year rank the candidates; filtering on them dropped real series
    assert searched == ['Fables']                         # mixed groups cost no search
    groups = _groups(db)
    fables = groups['/lib/Fables/Volume 01 (2002)']
    assert (fables['Tier'], fables['Status'], fables['ComicID']) == ('auto', 'identified', '25543')
    assert json.loads(fables['Candidates'])[0]['name'] == 'Fables'
    assert json.loads(fables['Evidence'])['files'] == 3
    assert (groups['/lib/Arc']['Tier'], groups['/lib/Arc']['ComicID']) == ('mixed', None)
    linked = db.select("SELECT COUNT(*) AS n FROM orphans WHERE GroupID=?", [fables['GroupID']])
    assert linked[0]['n'] == 3


@pytest.mark.integration
def test_identify_paces_only_after_a_search():
    db = FakeDB()
    db.add_orphans(_fables() + [orphan('/lib/Arc/a.cbz', OrphanID='m1')])
    slept = []
    oj.identify_groups(db, lambda *a: _fables_result(), _now, slept.append, 20)
    assert slept == [20]


@pytest.mark.integration
def test_identify_stops_after_consecutive_failures_and_resumes():
    db = FakeDB()
    db.add_orphans(_fables(folder='/lib/A (2002)') + _fables(folder='/lib/B (2002)')
                   + _fables(folder='/lib/C (2002)') + _fables(folder='/lib/D (2002)'))

    searched = []
    slept = []
    summary = oj.identify_groups(db, lambda *a: (searched.append(a), False)[1], _now, slept.append, 20)

    assert summary['stopped'] is True
    assert 'ComicVine' in summary['reason']
    assert len(searched) == 3                      # exactly MAX_SEARCH_FAILURES
    assert slept == [20, 20]                       # 2 sleeps before stop on 3rd failure
    assert _groups(db) == {}                      # nothing half-recorded

    summary = oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20)
    assert summary['stopped'] is False
    assert len(_groups(db)) == 4


@pytest.mark.integration
def test_identify_skips_groups_already_identified():
    db = FakeDB()
    db.add_orphans(_fables())
    oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20)
    calls = []
    oj.identify_groups(db, lambda *a: calls.append(a) or _fables_result(), _now, _no_sleep, 20)
    assert calls == []


@pytest.mark.integration
def test_identify_asks_again_for_groups_whose_search_found_nothing():
    db = FakeDB()
    db.add_orphans(_fables(folder='/lib/A (2002)') + _fables(folder='/lib/B (2002)')
                   + [orphan('/lib/Arc/a.cbz', OrphanID='m1', ParsedSeries='Superman'),
                      orphan('/lib/Arc/b.cbz', OrphanID='m2', ParsedSeries='Doomsday')])
    answers = iter([[], _fables_result()])         # same size, so A is asked first
    oj.identify_groups(db, lambda series: next(answers), _now, _no_sleep, 20)
    assert json.loads(_groups(db)['/lib/A (2002)']['Candidates']) == []

    calls = []
    oj.identify_groups(db, lambda series: calls.append(series) or _fables_result(),
                       _now, _no_sleep, 20)

    assert calls == ['Fables']                     # only the empty one; not B, not mixed
    assert json.loads(_groups(db)['/lib/A (2002)']['Candidates'])[0]['name'] == 'Fables'


@pytest.mark.integration
def test_identify_honours_stop():
    db = FakeDB()
    db.add_orphans(_fables(folder='/lib/A (2002)') + [
        orphan('/lib/B (2002)/x.cbz', OrphanID='x', ParsedSeries='Fables', ParsedIssue='1')])
    summary = oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20,
                                 stop=lambda: True)
    assert summary['stopped'] is True
    assert _groups(db) == {}


@pytest.mark.integration
def test_identify_resets_failures_on_successful_search():
    db = FakeDB()
    db.add_orphans(_fables(folder='/lib/A (2002)') + _fables(folder='/lib/B (2002)')
                   + _fables(folder='/lib/C (2002)') + _fables(folder='/lib/D (2002)')
                   + _fables(folder='/lib/E (2002)'))

    search_results = [False, False, True, False, False]
    search_index = [0]

    def search(*a):
        result = search_results[search_index[0]]
        search_index[0] += 1
        return _fables_result() if result else False

    summary = oj.identify_groups(db, search, _now, _no_sleep, 20)

    assert summary['stopped'] is False
    groups = _groups(db)
    assert len(groups) == 1                       # only the successful one recorded
    assert groups['/lib/C (2002)']['Tier'] == 'auto'


class _FailingGroupInsertDB(FakeDB):
    """FakeDB that returns None when inserting orphan_groups rows."""
    def action(self, query, args=None, executemany=False):
        if query.startswith('INSERT OR REPLACE INTO orphan_groups'):
            return None
        return super(_FailingGroupInsertDB, self).action(query, args, executemany)


@pytest.mark.integration
def test_identify_raises_on_group_record_failure():
    db = _FailingGroupInsertDB()
    db.add_orphans(_fables())

    with pytest.raises(RuntimeError, match='Could not record group'):
        oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20)

    # Orphan groups should stay empty (no half-recorded group)
    assert _groups(db) == {}

    # Orphans should be linked (the UPDATE happened before the failed INSERT)
    linked = db.select("SELECT COUNT(*) AS n FROM orphans WHERE GroupID IS NOT NULL")
    assert linked[0]['n'] == 3

    # Subsequent run with normal DB should record the group successfully
    db2 = FakeDB()
    db2.add_orphans(_fables())
    summary = oj.identify_groups(db2, lambda *a: _fables_result(), _now, _no_sleep, 20)
    assert summary['stopped'] is False
    assert len(_groups(db2)) == 1


@pytest.mark.unit
def test_job_state_allows_one_job_at_a_time():
    state = oj.JobState()
    assert state.begin('identify') is True
    assert state.begin('file') is False
    state.request_stop()
    assert state.should_stop() is True
    state.finish('done')
    assert state.snapshot()['running'] is False
    assert state.snapshot()['message'] == 'done'
    assert state.begin('file') is True
    assert state.should_stop() is False


@pytest.mark.unit
def test_job_state_sleep_wakes_when_stop_is_requested():
    state = oj.JobState()
    state.begin('identify')
    threading.Timer(0.05, state.request_stop).start()

    started = time.monotonic()
    state.sleep(5)

    assert time.monotonic() - started < 1


def _identified(db):
    db.add_orphans(_fables(folder='/lib/A (2002)') + _fables(folder='/lib/B (2002)') + [
        # one series, but half the files carry no number: review, not mixed
        orphan('/lib/C/c1.cbz', OrphanID='c1', ParsedSeries='Fables', ParsedIssue='1'),
        orphan('/lib/C/c2.cbz', OrphanID='c2', ParsedSeries='Fables', ParsedIssue=None)])
    oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20)
    return dict((g['Folder'], g['GroupID']) for g in
                [dict(r) for r in db.select('SELECT Folder, GroupID FROM orphan_groups')])


@pytest.mark.integration
def test_approve_auto_approves_everything_not_excluded():
    db = FakeDB()
    ids = _identified(db)
    assert oj.approve_auto(db, [ids['/lib/B (2002)']]) == 1
    status = dict((r['Folder'], r['Status']) for r in db.select('SELECT Folder, Status FROM orphan_groups'))
    assert status == {'/lib/A (2002)': 'approved', '/lib/B (2002)': 'identified',
                      '/lib/C': 'identified'}


@pytest.mark.integration
def test_pick_series_approves_a_review_group_but_not_a_filed_one():
    db = FakeDB()
    ids = _identified(db)
    assert oj.pick_series(db, ids['/lib/C'], '99') is True
    row = db.select('SELECT ComicID, Status FROM orphan_groups WHERE GroupID=?', [ids['/lib/C']])[0]
    assert (row['ComicID'], row['Status']) == ('99', 'approved')
    db.action("UPDATE orphan_groups SET Status='filed' WHERE GroupID=?", [ids['/lib/A (2002)']])
    assert oj.pick_series(db, ids['/lib/A (2002)'], '99') is False
    assert oj.pick_series(db, 'nope', '99') is False


@pytest.mark.integration
def test_skip_group():
    db = FakeDB()
    ids = _identified(db)
    assert oj.skip_group(db, ids['/lib/C']) is True
    assert db.select('SELECT Status FROM orphan_groups WHERE GroupID=?',
                     [ids['/lib/C']])[0]['Status'] == 'skipped'


@pytest.mark.integration
def test_load_groups_splits_by_tier_and_decodes():
    db = FakeDB()
    _identified(db)
    groups = oj.load_groups(db)
    assert sorted(groups) == ['auto', 'mixed', 'review']
    assert [g['Folder'] for g in groups['auto']] == ['/lib/A (2002)', '/lib/B (2002)']
    first = groups['auto'][0]
    assert first['Files'] == 3
    assert first['Candidates'][0]['comicid'] == '25543'
    assert first['Failed'] == []
    assert first['FirstOrphanID'] is not None


@pytest.mark.integration
def test_load_batches_counts_and_orders_newest_first():
    db = FakeDB()
    # batch b1 (RunID r1, ComicID 100): Kind 'move' rows — 2 'done', 1 'failed', 1 'moving'; plus one Kind 'rmdir' 'done' row (must NOT be counted)
    db.action("INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, Kind, Status, WhenDone) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ['b1', 1, 'r1', '100', 'move', 'done', '2026-09-30 10:00:00'])
    db.action("INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, Kind, Status, WhenDone) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ['b1', 2, 'r1', '100', 'move', 'done', '2026-09-30 10:00:01'])
    db.action("INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, Kind, Status, WhenDone) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ['b1', 3, 'r1', '100', 'move', 'failed', '2026-09-30 10:00:02'])
    db.action("INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, Kind, Status, WhenDone) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ['b1', 4, 'r1', '100', 'move', 'moving', '2026-09-30 10:00:03'])
    db.action("INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, Kind, Status, WhenDone) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ['b1', 5, 'r1', '100', 'rmdir', 'done', '2026-09-30 10:00:04'])
    # batch b2 (RunID r1, ComicID 200): one Kind 'park' 'done' row with WhenDone 2026-09-30 11:00:00 (newer)
    db.action("INSERT INTO orphan_moves (BatchID, Seq, RunID, ComicID, Kind, Status, WhenDone) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ['b2', 1, 'r1', '200', 'park', 'done', '2026-09-30 11:00:00'])

    result = oj.load_batches(db)

    # Assert order is ['b2', 'b1'] (newest first)
    assert [b['BatchID'] for b in result] == ['b2', 'b1']

    # b2 checks: done=1 and failed/planned/reverted/moving/cancelled all 0
    b2 = result[0]
    assert b2['BatchID'] == 'b2'
    assert b2['RunID'] == 'r1'
    assert b2['ComicID'] == '200'
    assert b2['done'] == 1
    assert b2['failed'] == 0
    assert b2['planned'] == 0
    assert b2['reverted'] == 0
    assert b2['moving'] == 0
    assert b2['cancelled'] == 0

    # b1 checks: done=2 failed=1 moving=1 planned=0 reverted=0 cancelled=0
    b1 = result[1]
    assert b1['BatchID'] == 'b1'
    assert b1['RunID'] == 'r1'
    assert b1['ComicID'] == '100'
    assert b1['done'] == 2
    assert b1['failed'] == 1
    assert b1['moving'] == 1
    assert b1['planned'] == 0
    assert b1['reverted'] == 0
    assert b1['cancelled'] == 0


import os

from mylar import orphan_moves as om


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)
    return path


class _Deps(object):
    """FileDeps built over a FakeDB, recording what Mylar would have been asked."""

    def __init__(self, db, lib, missing_series=False):
        self.db, self.lib = db, lib
        self.rescans, self.history, self.logs = [], [], []
        self.missing_series = missing_series
        self.stop_at = None
        self.stop_calls = 0
        self.history_failures = 0

    def stop(self):
        # counts calls: the job asks once before the series, then execute asks
        # before each file
        self.stop_calls += 1
        return self.stop_at is not None and self.stop_calls >= self.stop_at

    def ensure_series(self, comicid):
        if self.missing_series:
            return None
        rows = self.db.select('SELECT * FROM comics WHERE ComicID=?', [comicid])
        comic = dict(rows[0])
        comic['issues'] = [dict(r) for r in self.db.select(
            'SELECT IssueID, Issue_Number, Location FROM issues WHERE ComicID=?', [comicid])]
        return comic

    def record_history(self, o, c, i, d):
        if self.history_failures:
            self.history_failures -= 1
            raise RuntimeError('history down')
        self.history.append((o['OrphanID'], d))

    def deps(self):
        return oj.FileDeps(
            ensure_series=self.ensure_series,
            name_for=lambda comic, o, i: 'Fables %03d (2002).cbz' % int(i['Issue_Number']),
            rescan=self.rescans.append,
            record_history=self.record_history,
            now=_now, clock=lambda: 0.0, stop=self.stop,
            library_root=self.lib, progress=lambda d: None, log=self.logs.append)


def _library(tmp_path):
    lib = str(tmp_path / 'lib')
    db = FakeDB()
    db.action("INSERT INTO comics VALUES ('25543', 'Fables', '2002', ?, 'Active')",
              [os.path.join(lib, 'Fables (2002)')])
    db.action("INSERT INTO issues (IssueID, ComicID, Issue_Number, Location) VALUES ('i1', '25543', '1', NULL), ('i2', '25543', '2', NULL),"
              " ('i3', '25543', '3', NULL)")
    folder = os.path.join(lib, 'Fables', 'Volume 01 (2002)')
    rows = []
    for name, issue, size in (('Fables 001.cbz', '001', 50), ('Fables 001 (1).cbz', '001', 49),
                              ('Fables 002.cbz', '002', 40), ('Fables extra.cbz', None, 10)):
        _write(os.path.join(folder, name), size)
        rows.append(orphan(os.path.join(folder, name), OrphanID=name, ParsedSeries='Fables',
                           ParsedIssue=issue, ParsedYear='2002', FileSize=size))
    db.add_orphans(rows)
    oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20)
    # one unnumbered file of four puts this group in review; approve it as a
    # person would, by picking the series
    db.action("UPDATE orphan_groups SET Status='approved', ComicID='25543'")
    return db, lib, folder


def _orphan_status(db):
    return dict((r['OrphanID'], r['Status']) for r in db.select('SELECT OrphanID, Status FROM orphans'))


@pytest.mark.integration
def test_run_file_job_files_a_series_and_updates_everything(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)

    results = oj.run_file_job(db, fake.deps(), 'run1')

    assert [(r.moved, r.parked, r.skipped, r.failed, r.stopped) for r in results] == [(2, 1, 1, 0, False)]
    series = os.path.join(lib, 'Fables (2002)')
    assert sorted(os.listdir(series)) == ['Fables 001 (2002).cbz', 'Fables 002 (2002).cbz']
    assert os.path.getsize(os.path.join(series, 'Fables 001 (2002).cbz')) == 50
    assert os.path.isfile(os.path.join(lib, '_duplicates', 'Fables', 'Volume 01 (2002)',
                                       'Fables 001 (1).cbz'))
    assert _orphan_status(db) == {'Fables 001.cbz': 'filed', 'Fables 001 (1).cbz': 'parked',
                                  'Fables 002.cbz': 'filed', 'Fables extra.cbz': 'new'}
    assert fake.rescans == ['25543']
    assert sorted(o for o, _ in fake.history) == ['Fables 001.cbz', 'Fables 002.cbz']
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'filed'
    assert any('2 moved, 1 parked, 1 skipped, 0 failed' in line for line in fake.logs)
    # the unmapped file keeps its folder alive
    assert os.path.isdir(folder)


@pytest.mark.integration
def test_a_series_that_cannot_be_added_moves_nothing(tmp_path):
    db, lib, folder = _library(tmp_path)
    results = oj.run_file_job(db, _Deps(db, lib, missing_series=True).deps(), 'run1')
    assert results[0].error and results[0].moved == 0
    assert len(os.listdir(folder)) == 4
    assert db.select('SELECT COUNT(*) AS n FROM orphan_moves')[0]['n'] == 0


@pytest.mark.integration
def test_stop_then_resume_batch_finishes_without_duplicate_history(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    fake.stop_at = 3          # series check, first file, then stop before the second
    first = oj.run_file_job(db, fake.deps(), 'run1')
    assert first[0].stopped is True
    assert (first[0].moved, first[0].parked) == (1, 0)
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'partial'
    assert [o for o, _ in fake.history] == ['Fables 001.cbz']

    fake.stop_at = None
    resumed = oj.resume_batch(db, first[0].batch_id, fake.deps())

    assert (resumed.moved, resumed.parked, resumed.stopped) == (2, 1, False)
    assert sorted(o for o, _ in fake.history) == ['Fables 001.cbz', 'Fables 002.cbz']
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'filed'


@pytest.mark.integration
def test_revert_run_restores_files_and_rows(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    oj.run_file_job(db, fake.deps(), 'run1')

    results = oj.revert_run(db, 'run1', fake.deps())

    assert [r.reverted for r in results] == [3]
    assert sorted(os.listdir(folder)) == ['Fables 001 (1).cbz', 'Fables 001.cbz',
                                          'Fables 002.cbz', 'Fables extra.cbz']
    assert _orphan_status(db) == {'Fables 001.cbz': 'identified', 'Fables 001 (1).cbz': 'identified',
                                  'Fables 002.cbz': 'identified', 'Fables extra.cbz': 'new'}
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'reverted'
    assert fake.rescans == ['25543', '25543']


@pytest.mark.unit
def test_wait_for_series_adds_once_then_waits_until_ready():
    db = FakeDB()
    added, slept = [], []

    def add(comicid):
        added.append(comicid)

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 1:
            db.action("INSERT INTO comics VALUES ('7', 'X', '2000', '/lib/X (2000)', 'Loading')")
        elif len(slept) == 2:
            db.action("UPDATE comics SET Status='Active'")
            db.action("INSERT INTO issues (IssueID, ComicID, Issue_Number, Location) VALUES ('i1', '7', '1', NULL)")

    comic = oj.wait_for_series(db, '7', add, sleep, timeout=60)
    assert added == ['7']
    assert comic['ComicLocation'] == '/lib/X (2000)'
    assert comic['issues'] == [{'IssueID': 'i1', 'Issue_Number': '1', 'Location': None}]


@pytest.mark.unit
def test_wait_for_series_gives_up_after_timeout():
    db = FakeDB()
    assert oj.wait_for_series(db, '7', lambda c: None, lambda s: None, timeout=10, poll=5) is None


@pytest.mark.unit
def test_wait_for_series_honours_stop():
    db = FakeDB()
    slept, calls = [], []

    def stop():
        calls.append(1)
        return len(calls) >= 2

    result = oj.wait_for_series(db, '7', lambda c: None, slept.append, timeout=900, stop=stop)

    assert result is None
    assert len(slept) <= 1


@pytest.mark.integration
def test_resume_refuses_reverted_batch(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    results = oj.run_file_job(db, fake.deps(), 'run1')
    batch_id = results[0].batch_id

    oj.revert_run(db, 'run1', fake.deps())

    result = oj.resume_batch(db, batch_id, fake.deps())
    assert result.error == 'Nothing left to file in this batch.'
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'reverted'
    assert sorted(os.listdir(folder)) == ['Fables 001 (1).cbz', 'Fables 001.cbz',
                                          'Fables 002.cbz', 'Fables extra.cbz']


@pytest.mark.integration
def test_revert_twice_leaves_refiled_orphans_alone(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    results = oj.run_file_job(db, fake.deps(), 'run1')
    batch_id = results[0].batch_id

    oj.revert_batch(db, batch_id, fake.deps())
    # Simulate a later batch refiling one orphan
    db.action("UPDATE orphans SET Status='filed', IssueID='i1' WHERE OrphanID='Fables 001.cbz'")

    result = oj.revert_batch(db, batch_id, fake.deps())
    # The orphan that was refiled should stay filed
    assert db.select('SELECT Status FROM orphans WHERE OrphanID=?',
                     ['Fables 001.cbz'])[0]['Status'] == 'filed'


@pytest.mark.integration
def test_revert_with_occupied_source_marks_group_partial(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    results = oj.run_file_job(db, fake.deps(), 'run1')
    batch_id = results[0].batch_id

    # Create a file at one of the source paths to block revert
    done_row = db.select(
        "SELECT Source FROM orphan_moves WHERE BatchID=? AND Status='done' AND Kind='move' LIMIT 1",
        [batch_id])[0]
    _write(done_row['Source'], 10)

    result = oj.revert_batch(db, batch_id, fake.deps())
    assert result.skipped  # Should have skipped at least one
    assert db.select('SELECT Status FROM orphan_groups WHERE GroupID IN'
                     ' (SELECT DISTINCT GroupID FROM orphan_moves WHERE BatchID=?)',
                     [batch_id])[0]['Status'] == 'partial'


@pytest.mark.integration
def test_history_failure_is_retried_on_resume(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    fake.history_failures = 1

    results = oj.run_file_job(db, fake.deps(), 'run1')
    batch_id = results[0].batch_id

    # After first run, one moved orphan should not be filed due to history failure
    moved_count = db.select('SELECT COUNT(*) AS n FROM orphan_moves WHERE BatchID=? AND Kind=? AND Status=?',
                            [batch_id, 'move', 'done'])[0]['n']
    filed_count = db.select('SELECT COUNT(*) AS n FROM orphans WHERE Status=?', ['filed'])[0]['n']
    assert filed_count == moved_count - 1  # One less filed than moved
    assert len(fake.history) == moved_count - 1

    # Group should be 'partial' because not all moved orphans are filed
    group_status = db.select('SELECT Status FROM orphan_groups WHERE GroupID IN'
                             ' (SELECT DISTINCT GroupID FROM orphan_moves WHERE BatchID=?)',
                             [batch_id])[0]['Status']
    assert group_status == 'partial'

    # Resume the batch
    resumed = oj.resume_batch(db, batch_id, fake.deps())

    # Resume should succeed
    assert resumed.error is None

    # All moved orphans should now be filed
    filed_after = db.select('SELECT COUNT(*) AS n FROM orphans WHERE Status=?', ['filed'])[0]['n']
    assert filed_after == moved_count

    # No duplicate histories (each orphan recorded exactly once)
    assert len(fake.history) == moved_count
    assert len(set(o for o, _ in fake.history)) == len(fake.history)

    # Group should now be 'filed'
    group_status_after = db.select('SELECT Status FROM orphan_groups WHERE GroupID IN'
                                   ' (SELECT DISTINCT GroupID FROM orphan_moves WHERE BatchID=?)',
                                   [batch_id])[0]['Status']
    assert group_status_after == 'filed'




@pytest.mark.integration
def test_a_file_mylar_already_tracks_is_marked_filed_and_never_moved(tmp_path):
    db, lib, folder = _library(tmp_path)
    series = os.path.join(lib, 'Fables (2002)')
    tracked = _write(os.path.join(series, 'Fables 001 (2002).cbz'), 60)
    db.action("UPDATE issues SET Location='Fables 001 (2002).cbz' WHERE IssueID='i1'")
    # a scan listed Mylar's own file as an orphan, and its group was approved
    db.add_orphans([orphan(tracked, OrphanID='tracked', ParsedSeries='Fables',
                           ParsedIssue='001', ParsedYear='2002', FileSize=60)])
    oj.identify_groups(db, lambda *a: _fables_result(), _now, _no_sleep, 20)
    db.action("UPDATE orphan_groups SET Status='approved', ComicID='25543'")
    fake = _Deps(db, lib)

    results = oj.run_file_job(db, fake.deps(), 'run1')

    assert [(r.moved, r.parked, r.skipped) for r in results] == [(1, 2, 2)]
    assert os.path.getsize(tracked) == 60
    assert db.select("SELECT COUNT(*) AS n FROM orphan_moves WHERE OrphanID='tracked'")[0]['n'] == 0
    row = db.select("SELECT Status, IssueID FROM orphans WHERE OrphanID='tracked'")[0]
    assert (row['Status'], row['IssueID']) == ('filed', 'i1')
    assert 'tracked' not in [o for o, _ in fake.history]


@pytest.mark.integration
def test_a_crash_after_moving_leaves_the_batch_to_resume_not_to_file_again(tmp_path, monkeypatch):
    from mylar import orphan_moves as om
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    real = om.remove_empty_dirs

    def died(*a, **k):
        raise RuntimeError('process died')

    monkeypatch.setattr(om, 'remove_empty_dirs', died)
    with pytest.raises(RuntimeError):
        oj.run_file_job(db, fake.deps(), 'run1')
    monkeypatch.setattr(om, 'remove_empty_dirs', real)

    # the files moved but nothing after that was recorded
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'partial'
    [batch] = oj.load_batches(db)
    assert batch['resumable'] is True

    # File approved again must not plan the moved files a second time
    assert oj.run_file_job(db, fake.deps(), 'run2') == []
    assert len(oj.load_batches(db)) == 1

    resumed = oj.resume_batch(db, batch['BatchID'], fake.deps())

    assert resumed.error is None
    assert _orphan_status(db) == {'Fables 001.cbz': 'filed', 'Fables 001 (1).cbz': 'parked',
                                  'Fables 002.cbz': 'filed', 'Fables extra.cbz': 'new'}
    assert sorted(o for o, _ in fake.history) == ['Fables 001.cbz', 'Fables 002.cbz']
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'filed'
    assert oj.load_batches(db)[0]['resumable'] is False


@pytest.mark.integration
def test_load_batches_marks_only_unfinished_batches_resumable(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    fake.stop_at = 3
    oj.run_file_job(db, fake.deps(), 'run1')
    assert [b['resumable'] for b in oj.load_batches(db)] == [True]

    fake.stop_at = None
    oj.resume_batch(db, oj.load_batches(db)[0]['BatchID'], fake.deps())
    assert [b['resumable'] for b in oj.load_batches(db)] == [False]


def _issue_rows(db):
    return dict((r['IssueID'], (r['Status'], r['Location'])) for r in db.select(
        'SELECT IssueID, Status, Location FROM issues'))


@pytest.mark.integration
def test_revert_resets_the_issues_it_had_filed_before_rescanning(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    oj.run_file_job(db, fake.deps(), 'run1')
    # what Mylar's rescan and history made of the filed files, plus rows the
    # batch never touched
    db.action("UPDATE issues SET Status='Downloaded', Location='Fables 001 (2002).cbz'"
              " WHERE IssueID='i1'")
    db.action("UPDATE issues SET Status='Archived', Location='Fables 002 (2002).cbz'"
              " WHERE IssueID='i2'")
    db.action("UPDATE issues SET Status='Downloaded', Location='elsewhere.cbz'"
              " WHERE IssueID='i3'")
    db.action("INSERT INTO snatched (IssueID, ComicID, Status, Provider) VALUES"
              " ('i1', '25543', 'Post-Processed', 'Orphan'),"
              " ('i2', '25543', 'Post-Processed', 'Orphan'),"
              " ('i1', '25543', 'Downloaded', 'GetComics'),"
              " ('i3', '25543', 'Post-Processed', 'Orphan')")
    seen = []
    deps = fake.deps()._replace(rescan=lambda comicid: seen.append(_issue_rows(db)))

    oj.revert_run(db, 'run1', deps)

    expected = {'i1': ('Skipped', None), 'i2': ('Skipped', None),
                'i3': ('Downloaded', 'elsewhere.cbz')}
    assert seen == [expected]              # reset before Mylar looks at the disk
    assert _issue_rows(db) == expected
    assert sorted((r['IssueID'], r['Provider']) for r in db.select(
        'SELECT IssueID, Provider FROM snatched')) == [('i1', 'GetComics'), ('i3', 'Orphan')]


@pytest.mark.integration
def test_revert_of_an_already_reverted_batch_leaves_its_groups_alone(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    batch_id = oj.run_file_job(db, fake.deps(), 'run1')[0].batch_id
    oj.revert_batch(db, batch_id, fake.deps())
    # the group was approved and filed again by a later batch
    db.action("UPDATE orphan_groups SET Status='filed'")

    oj.revert_batch(db, batch_id, fake.deps())

    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'filed'


@pytest.mark.integration
def test_revert_of_a_batch_that_never_moved_frees_its_groups(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    fake.stop_at = 2          # series check, then stop before the first file
    batch_id = oj.run_file_job(db, fake.deps(), 'run1')[0].batch_id
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'partial'

    oj.revert_batch(db, batch_id, fake.deps())

    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'reverted'
    assert len(os.listdir(folder)) == 4


def _filed_with_a_park(tmp_path):
    db, lib, folder = _library(tmp_path)
    fake = _Deps(db, lib)
    batch_id = oj.run_file_job(db, fake.deps(), 'run1')[0].batch_id
    db.action("UPDATE issues SET Location='Fables 001 (2002).cbz' WHERE IssueID='i1'")
    return db, lib, folder, fake, batch_id


@pytest.mark.integration
def test_revert_batch_with_deleted_parks_restores_the_rest(tmp_path):
    db, lib, folder, fake, batch_id = _filed_with_a_park(tmp_path)
    oj.delete_parked_job(db, [batch_id], fake.deps())

    result = oj.revert_batch(db, batch_id, fake.deps())

    assert [reason for _, reason in result.skipped] == [
        'deleted on %s; cannot be put back' % _now()]
    assert sorted(os.listdir(folder)) == ['Fables 001.cbz', 'Fables 002.cbz', 'Fables extra.cbz']
    assert _orphan_status(db)['Fables 001 (1).cbz'] == 'deleted'
    assert _orphan_status(db)['Fables 001.cbz'] == 'identified'
    assert db.select('SELECT Status FROM orphan_groups')[0]['Status'] == 'partial'


@pytest.mark.integration
def test_keeper_path_is_the_issue_file_in_the_series_folder(tmp_path):
    db = FakeDB()
    db.action("INSERT INTO comics (ComicID, ComicLocation, Status) VALUES ('c1', ?, 'Active')",
              [str(tmp_path / 'S')])
    db.action("INSERT INTO issues (IssueID, ComicID, Location) VALUES ('i1', 'c1', 'a.cbz'),"
              " ('i2', 'c1', NULL)")
    assert oj.keeper_path(db, 'i1') == os.path.realpath(str(tmp_path / 'S' / 'a.cbz'))
    assert oj.keeper_path(db, 'i2') is None
    assert oj.keeper_path(db, 'nope') is None


@pytest.mark.integration
def test_delete_parked_job_deletes_and_clears_empty_folders(tmp_path):
    db, lib, folder, fake, batch_id = _filed_with_a_park(tmp_path)
    parked = os.path.join(lib, '_duplicates', 'Fables', 'Volume 01 (2002)', 'Fables 001 (1).cbz')
    assert os.path.isfile(parked)

    results = oj.delete_parked_job(db, [batch_id], fake.deps())

    assert [r.deleted for r in results] == [1]
    assert not os.path.exists(os.path.join(lib, '_duplicates', 'Fables'))
    assert os.path.isdir(os.path.join(lib, '_duplicates'))
    assert os.path.isfile(os.path.join(lib, 'Fables (2002)', 'Fables 001 (2002).cbz'))
    assert any(batch_id in m and '1 deleted' in m for m in fake.logs)


@pytest.mark.integration
def test_delete_parked_job_keeps_parks_whose_issue_has_no_file(tmp_path):
    db, lib, folder, fake, batch_id = _filed_with_a_park(tmp_path)
    db.action("UPDATE issues SET Location=NULL WHERE IssueID='i1'")
    results = oj.delete_parked_job(db, [batch_id], fake.deps())
    assert [r.deleted for r in results] == [0]
    assert [reason for _, reason in results[0].kept] == ['issue has no recorded file']


@pytest.mark.integration
def test_load_batches_counts_parked_and_deleted(tmp_path):
    from mylar import orphan_batches as ob, orphan_moves as om
    db = FakeDB()
    db.add_orphans([orphan('/l/F/a.cbz', OrphanID='o1', FileSize=10, Status='parked'),
                    orphan('/l/F/b.cbz', OrphanID='o2', FileSize=32, Status='parked')])
    om.record_plan(db, 'r1', 'b1', 'c1', {}, [
        ob.Move('park', 'o1', '/l/F/a.cbz', '/l/_duplicates/F/a.cbz', 'i1'),
        ob.Move('park', 'o2', '/l/F/b.cbz', '/l/_duplicates/F/b.cbz', 'i2')], _now())
    db.action("UPDATE orphan_moves SET Status='done', WhenDone=?", [_now()])
    db.action("UPDATE orphan_moves SET Status='deleted' WHERE OrphanID='o1'")
    batch = oj.load_batches(db)[0]
    assert (batch['parked'], batch['parked_bytes'], batch['deleted']) == (1, 32, 1)


@pytest.mark.integration
def test_delete_parked_job_goes_on_to_the_next_batch_when_unlink_is_refused(tmp_path, monkeypatch):
    db, lib, folder, fake, batch_id = _filed_with_a_park(tmp_path)
    real = oj.moves.delete_parked
    calls = []

    def refusing_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            def refuse(path):
                raise PermissionError(13, 'Permission denied')
            kwargs['unlink'] = refuse
        return real(*args, **kwargs)

    monkeypatch.setattr(oj.moves, 'delete_parked', refusing_first)
    # the same batch twice stands in for two batches: the first pass is refused
    results = oj.delete_parked_job(db, [batch_id, batch_id], fake.deps())

    assert [r.deleted for r in results] == [0, 1]
    assert [reason for _, reason in results[0].kept] == ['could not delete: Permission denied']
