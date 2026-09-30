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

    summary = oj.identify_groups(db, lambda *a: False, _now, _no_sleep, 20)

    assert summary['stopped'] is True
    assert 'ComicVine' in summary['reason']
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
