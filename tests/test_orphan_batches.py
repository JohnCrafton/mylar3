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


# --- planning a series ------------------------------------------------------

def _issues(*numbers, **located):
    return [{'IssueID': 'i%s' % n, 'Issue_Number': str(n),
             'Location': located.get('n%s' % n)} for n in numbers]


def _name_for(orphan_row, issue):
    return 'Series %03d.cbz' % int(issue['Issue_Number'])


def _never_on_disk(issue):
    return False


@pytest.mark.unit
def test_keeper_is_largest_then_cbz_then_plainest_name():
    rows = [orphan('/l/F/F 001 (1).cbz', FileSize=50),
            orphan('/l/F/F 001.cbr', FileSize=50),
            orphan('/l/F/F 001.cbz', FileSize=50),
            orphan('/l/F/F 001 (2).cbz', FileSize=49)]
    assert [r['FileName'] for r in sorted(rows, key=ob.keeper_rank)] == [
        'F 001.cbz', 'F 001 (1).cbz', 'F 001.cbr', 'F 001 (2).cbz']


@pytest.mark.unit
def test_park_destination_mirrors_the_path_under_the_library():
    assert ob.park_destination('/lib/Fables/Vol 1/F 001 (1).cbz', '/lib') == \
        '/lib/_duplicates/Fables/Vol 1/F 001 (1).cbz'
    # outside the library: keep the whole path rather than collide
    assert ob.park_destination('/elsewhere/x/F.cbz', '/lib') == \
        '/lib/_duplicates/elsewhere/x/F.cbz'


@pytest.mark.unit
def test_plan_series_moves_one_per_issue_and_parks_the_rest():
    files = [orphan('/l/F/F 001.cbz', ParsedIssue='001', FileSize=50),
             orphan('/l/F/F 001 (1).cbz', ParsedIssue='001', FileSize=49),
             orphan('/l/F/F 002.cbz', ParsedIssue='002', FileSize=40)]
    plan = ob.plan_series(files, _issues(1, 2), _never_on_disk, '/l/Fables (2002)',
                          _name_for, '/l')
    assert [(m.kind, m.orphanid, m.destination, m.issueid) for m in plan.moves] == [
        ('move', 'F 001.cbz', '/l/Fables (2002)/Series 001.cbz', 'i1'),
        ('park', 'F 001 (1).cbz', '/l/_duplicates/F/F 001 (1).cbz', 'i1'),
        ('move', 'F 002.cbz', '/l/Fables (2002)/Series 002.cbz', 'i2')]
    assert plan.skipped == ()


@pytest.mark.unit
def test_plan_series_skips_files_it_cannot_map_rather_than_guessing():
    files = [orphan('/l/F/F 001.cbz', ParsedIssue='001'),
             orphan('/l/F/F special.cbz', ParsedIssue=None),
             orphan('/l/F/F 999.cbz', ParsedIssue='999')]
    plan = ob.plan_series(files, _issues(1), _never_on_disk, '/l/F (2002)', _name_for, '/l')
    assert [m.orphanid for m in plan.moves] == ['F 001.cbz']
    assert plan.skipped == ('F special.cbz', 'F 999.cbz')


@pytest.mark.unit
def test_plan_series_parks_every_copy_of_an_issue_mylar_already_has():
    files = [orphan('/l/F/F 001.cbz', ParsedIssue='001')]
    plan = ob.plan_series(files, _issues(1, n1='F 001 (2002).cbz'),
                          lambda issue: True, '/l/F (2002)', _name_for, '/l')
    assert [(m.kind, m.orphanid) for m in plan.moves] == [('park', 'F 001.cbz')]


@pytest.mark.unit
def test_plan_series_ignores_a_recorded_file_that_is_not_on_disk():
    # Mylar's row says issue 1 has a file, but the file is gone - the orphan is
    # the only copy, so it moves in instead of being parked beside a ghost
    files = [orphan('/l/F/F 001.cbz', ParsedIssue='001')]
    plan = ob.plan_series(files, _issues(1, n1='gone.cbz'), _never_on_disk,
                          '/l/F (2002)', _name_for, '/l')
    assert [(m.kind, m.orphanid) for m in plan.moves] == [('move', 'F 001.cbz')]


# --- guards -----------------------------------------------------------------

@pytest.mark.unit
def test_batch_blockers_refuse_a_folder_monitor_over_the_library():
    assert ob.batch_blockers(True, '/media', '/media', False) != []
    assert ob.batch_blockers(True, '/', '/media', False) != []
    assert ob.batch_blockers(True, '/downloads', '/media', False) == []
    assert ob.batch_blockers(False, '/media', '/media', False) == []


@pytest.mark.unit
def test_batch_blockers_refuse_a_second_job_and_a_missing_library():
    assert len(ob.batch_blockers(False, None, '/media', True)) == 1
    assert len(ob.batch_blockers(False, None, None, False)) == 1


@pytest.mark.integration
def test_backup_database_writes_a_consistent_copy_beside_the_original(tmp_path):
    src = str(tmp_path / 'mylar.db')
    conn = sqlite3.connect(src)
    conn.execute('CREATE TABLE t (x)')
    conn.execute('INSERT INTO t VALUES (1)')
    conn.commit()

    path = ob.backup_database(src, '20260930-120000')

    assert path == str(tmp_path / 'mylar.db.orphans-20260930-120000')
    assert sqlite3.connect(path).execute('SELECT x FROM t').fetchall() == [(1,)]
