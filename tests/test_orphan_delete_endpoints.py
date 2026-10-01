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
    assert reply == {'status': 'success', 'files': 0, 'bytes': 0, 'kept': []}
