import json

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

    def search(series, issue, year):
        searched.append((series, issue, year))
        return _fables_result()

    summary = oj.identify_groups(db, search, _now, _no_sleep, 20)

    assert summary == {'auto': 1, 'review': 0, 'mixed': 1, 'stopped': False, 'reason': None}
    assert searched == [('Fables', '3', '2002')]          # mixed groups cost no search
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
