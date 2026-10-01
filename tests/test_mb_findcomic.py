"""findComic's strict mode: a ComicVine error reply is a failed search.

ComicVine answers a rate limit or bad key with status_code other than 1 and
no results. Without strict, findComic returns an empty list for that, which
reads as "no series by that name".
"""
import types
from xml.dom.minidom import parseString

import pytest


def _reply(status, total='0'):
    return parseString('<response><error>%s</error><status_code>%s</status_code>'
                       '<number_of_total_results>%s</number_of_total_results>'
                       '<results/></response>'
                       % ('OK' if status == '1' else 'Rate limit exceeded', status, total))


@pytest.fixture
def mb(monkeypatch):
    import mylar
    from mylar import mb
    monkeypatch.setattr(mylar, 'CONFIG', types.SimpleNamespace(COMICVINE_API='key'),
                        raising=False)
    monkeypatch.setattr(mylar, 'LOG_LEVEL', 0, raising=False)
    monkeypatch.setattr(mb, 'listLibrary', lambda: {})
    return mb


@pytest.mark.unit
def test_strict_findcomic_fails_on_a_comicvine_error_reply(mb, monkeypatch):
    monkeypatch.setattr(mb, 'pullsearch', lambda *a: _reply('107'))
    assert mb.findComic('Nyx', mode='series', issue=None, strict=True) is False


@pytest.mark.unit
def test_findcomic_without_strict_keeps_returning_empty_on_error_reply(mb, monkeypatch):
    monkeypatch.setattr(mb, 'pullsearch', lambda *a: _reply('107'))
    assert mb.findComic('Nyx', mode='series', issue=None) == []


@pytest.mark.unit
def test_strict_findcomic_returns_empty_when_comicvine_found_nothing(mb, monkeypatch):
    monkeypatch.setattr(mb, 'pullsearch', lambda *a: _reply('1'))
    assert mb.findComic('Nyx', mode='series', issue=None, strict=True) == []
