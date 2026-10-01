"""The delete endpoints refuse unless both orphan settings are on, and never
overlap another orphan job. WebInterface is exercised directly; mylar.CONFIG
and db.DBConnection are swapped for fakes."""
import json
import types

import pytest

from tests.orphan_fakes import FakeDB


@pytest.fixture
def web(monkeypatch):
    import mylar
    from mylar import webserve, orphan_jobs
    config = types.SimpleNamespace(ENABLE_ORPHANS=True, ORPHANS_ALLOW_DELETE=True,
                                   ENABLE_CHECK_FOLDER=False, CHECK_FOLDER=None,
                                   DESTINATION_DIR='/lib')
    monkeypatch.setattr(mylar, 'CONFIG', config, raising=False)
    fake = FakeDB()
    monkeypatch.setattr(webserve.db, 'DBConnection', lambda: fake)
    orphan_jobs.STATE.finish('')
    return webserve.WebInterface(), config


@pytest.mark.integration
@pytest.mark.parametrize('flag', ['ENABLE_ORPHANS', 'ORPHANS_ALLOW_DELETE'])
def test_delete_endpoints_refuse_when_a_setting_is_off(web, flag):
    page, config = web
    setattr(config, flag, False)
    assert json.loads(page.orphanDeleteParkedPreview())['status'] == 'failure'
    assert json.loads(page.orphanDeleteParked())['status'] == 'failure'


@pytest.mark.integration
def test_delete_endpoint_refuses_while_a_job_runs(web):
    from mylar import orphan_jobs
    page, config = web
    assert orphan_jobs.STATE.begin('file')
    try:
        reply = json.loads(page.orphanDeleteParked())
    finally:
        orphan_jobs.STATE.finish('')
    assert reply['status'] == 'failure'


@pytest.mark.integration
def test_preview_with_nothing_parked_is_empty(web):
    page, config = web
    reply = json.loads(page.orphanDeleteParkedPreview())
    assert reply == {'status': 'success', 'files': 0, 'bytes': 0, 'kept': [], 'batch_ids': []}


@pytest.mark.integration
def test_parse_batch_ids_prefers_the_preview_list():
    from mylar import orphan_jobs as oj
    everything = lambda: ['x', 'y', 'z']
    assert oj.parse_batch_ids(None, 'a,b', everything) == ['a', 'b']
    assert oj.parse_batch_ids('q', ' a , b,', everything) == ['a', 'b']
    assert oj.parse_batch_ids('q', None, everything) == ['q']
    assert oj.parse_batch_ids(None, None, everything) == ['x', 'y', 'z']
    assert oj.parse_batch_ids(None, '', everything) == []      # never widens to all


@pytest.mark.integration
def test_preview_reports_the_batches_it_covered(web):
    page, config = web
    reply = json.loads(page.orphanDeleteParkedPreview(BatchIDs='a,b'))
    assert reply['batch_ids'] == ['a', 'b']
    assert json.loads(page.orphanDeleteParkedPreview(BatchID='a'))['batch_ids'] == ['a']


@pytest.mark.integration
def test_delete_with_empty_batch_ids_is_refused_not_widened(web):
    page, config = web
    assert json.loads(page.orphanDeleteParked(BatchIDs=''))['status'] == 'failure'
