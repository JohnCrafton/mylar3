import sqlite3

import pytest

from mylar import orphan_batches as ob
from tests.orphan_fakes import orphan


# --- schema -----------------------------------------------------------------

@pytest.mark.unit
def test_apply_schema_creates_tables_and_adds_groupid_to_an_existing_orphans_table():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE orphans (OrphanID TEXT, FilePath TEXT)')
    ob.apply_schema(conn.cursor())
    ob.apply_schema(conn.cursor())  # a second startup must be a no-op

    def columns(table):
        return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)]

    assert 'GroupID' in columns('orphans')
    assert columns('orphan_groups') == ['GroupID', 'Folder', 'Volume', 'Evidence',
                                        'Candidates', 'Tier', 'Failed', 'ComicID',
                                        'Status', 'IdentifiedDate']
    assert columns('orphan_moves') == ['BatchID', 'Seq', 'RunID', 'ComicID', 'GroupID',
                                       'OrphanID', 'IssueID', 'Kind', 'Source',
                                       'Destination', 'Status', 'Error', 'WhenDone']


# --- grouping ---------------------------------------------------------------
# A folder is not a series: Usagi Yojimbo/ held v1, v2 and v3 - three
# ComicVine series. Grouping by folder alone matched all 188 files to v3.

@pytest.mark.unit
def test_group_orphans_splits_one_folder_by_volume():
    rows = [orphan('/lib/Usagi/Usagi v1 #1.cbz', ParsedVolume='v1'),
            orphan('/lib/Usagi/Usagi v1 #2.cbz', ParsedVolume='v1'),
            orphan('/lib/Usagi/Usagi v3 #1.cbz', ParsedVolume='v3'),
            orphan('/lib/Usagi/Usagi #9.cbz')]
    groups = ob.group_orphans(rows)
    shapes = sorted((g.folder, g.volume or '', len(g.orphans)) for g in groups.values())
    assert shapes == [('/lib/Usagi', '', 1), ('/lib/Usagi', 'v1', 2),
                      ('/lib/Usagi', 'v3', 1)]


@pytest.mark.unit
def test_group_id_is_stable_and_distinguishes_volume():
    assert ob.group_id('/lib/A', 'v1') == ob.group_id('/lib/A', 'v1')
    assert ob.group_id('/lib/A', 'v1') != ob.group_id('/lib/A', None)
    assert len(ob.group_id('/lib/A', None)) == 12


# --- evidence ---------------------------------------------------------------

def _group(folder, rows):
    return ob.Group(ob.group_id(folder, None), folder, None, tuple(rows))


@pytest.mark.unit
def test_evidence_takes_the_year_from_the_folder_over_the_files():
    g = _group('/lib/Fables/Volume 01 (2002)', [
        orphan('/lib/Fables/Volume 01 (2002)/a.cbz', ParsedSeries='Fables',
               ParsedIssue='001', ParsedYear='2002'),
        orphan('/lib/Fables/Volume 01 (2002)/b.cbz', ParsedSeries='Fables',
               ParsedIssue='150', ParsedYear='2015'),
        orphan('/lib/Fables/Volume 01 (2002)/c.cbz', ParsedSeries='Fables',
               ParsedIssue='151', ParsedYear='2015')])
    ev = ob.evidence(g)
    assert ev.series == 'Fables'
    assert ev.share == 1.0
    assert ev.numbers == (1.0, 150.0, 151.0)
    assert (ev.year, ev.folder_year, ev.files) == ('2002', '2002', 3)


@pytest.mark.unit
def test_evidence_falls_back_to_the_commonest_file_year():
    g = _group('/lib/Usagi Yojimbo', [
        orphan('/lib/Usagi Yojimbo/a.cbz', ParsedSeries='Usagi Yojimbo', ParsedYear='1996'),
        orphan('/lib/Usagi Yojimbo/b.cbz', ParsedSeries='Usagi Yojimbo', ParsedYear='1996'),
        orphan('/lib/Usagi Yojimbo/c.cbz', ParsedSeries='Usagi Yojimbo', ParsedYear='1987')])
    ev = ob.evidence(g)
    assert (ev.year, ev.folder_year) == ('1996', None)


@pytest.mark.unit
def test_evidence_measures_how_many_files_agree_on_the_series():
    g = _group('/lib/Mixed', [
        orphan('/lib/Mixed/a.cbz', ParsedSeries='Superman'),
        orphan('/lib/Mixed/b.cbz', ParsedSeries='Superman'),
        orphan('/lib/Mixed/c.cbz', ParsedSeries='Doomsday'),
        orphan('/lib/Mixed/d.cbz')])
    ev = ob.evidence(g)
    assert ev.series == 'Superman'
    assert ev.share == 0.5


@pytest.mark.unit
def test_search_parsed_asks_for_the_highest_issue():
    ev = ob.Evidence('Fables', 1.0, (1.0, 162.0, None), '2002', '2002', 3)
    parsed = ob.search_parsed(ev)
    assert parsed['series'] == 'Fables'
    assert parsed['issue'] == '162'
    assert parsed['year'] == '2002'


# --- tiering ----------------------------------------------------------------

def _ranked(name='Fables', year='2002', issues='161', score=100, runner_up=None,
            evidence=('title', 'year')):
    ranked = [{'comicid': '1', 'name': name, 'comicyear': year, 'issues': issues,
               'publisher': 'Vertigo', 'score': score, 'evidence': list(evidence),
               'comicimage': 'http://x/1.jpg'}]
    if runner_up is not None:
        ranked.append({'comicid': '2', 'name': name, 'comicyear': year, 'issues': issues,
                       'publisher': 'X', 'score': runner_up, 'evidence': list(evidence),
                       'comicimage': None})
    return ranked


def _ev(numbers=(1.0, 2.0, 3.0), share=1.0, folder_year='2002', series='Fables'):
    return ob.Evidence(series, share, tuple(numbers), folder_year or '2002',
                       folder_year, len(numbers))


@pytest.mark.unit
def test_a_clean_folder_is_auto():
    assert ob.tier(_ev(), _ranked()) == ('auto', [])


@pytest.mark.unit
def test_low_agreement_is_mixed_without_checks():
    assert ob.tier(_ev(share=0.59), _ranked()) == ('mixed', [])
    assert ob.tier(_ev(series=None), []) == ('mixed', [])


@pytest.mark.unit
@pytest.mark.parametrize('ev,ranked,failed', [
    (_ev(), [], ['auto_selection', 'title', 'year', 'run']),
    (_ev(), _ranked(runner_up=95), ['auto_selection']),
    (_ev(), _ranked(name='Fables: The Wolf Among Us'), ['title']),
    (_ev(folder_year='1963'), _ranked(year='1951'), ['year']),
    (_ev(numbers=[1.0] * 19 + [200.0]), _ranked(issues='161'), []),       # 95% in run
    (_ev(numbers=[1.0] * 18 + [200.0, 201.0]), _ranked(issues='161'), ['run']),
    (_ev(share=0.94), _ranked(), ['share']),
    # unnumbered files are outside any run too
    (_ev(numbers=[1.0] * 18 + [None, None]), _ranked(), ['run', 'numbered']),
])
def test_each_auto_check_sends_a_group_to_review(ev, ranked, failed):
    assert ob.tier(ev, ranked) == ('review' if failed else 'auto', failed)


@pytest.mark.unit
def test_no_folder_year_skips_the_year_check():
    assert ob.failed_checks(_ev(folder_year=None), _ranked(year='1999')) == []


@pytest.mark.unit
def test_candidate_summary_keeps_what_the_page_shows():
    summary = ob.candidate_summary(_ranked(runner_up=80), n=1)
    assert summary == [{'comicid': '1', 'name': 'Fables', 'comicyear': '2002',
                        'issues': '161', 'publisher': 'Vertigo', 'score': 100,
                        'comicimage': 'http://x/1.jpg'}]
