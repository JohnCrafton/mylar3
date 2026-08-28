import pytest

from mylar import orphans


# --- title normalisation -------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ('The Amazing Spider-Man', 'amazing spider man'),
    ('Batman: The Killing Joke', 'batman killing joke'),
    ('  X-MEN   ', 'x men'),
    ("Marvel's The Avengers", 'marvels avengers'),
    ('Saga, Vol. 1', 'saga vol 1'),
    (None, ''),
    ('', ''),
])
def test_normalize_title(raw, expected):
    assert orphans.normalize_title(raw) == expected


@pytest.mark.unit
def test_leading_article_is_dropped_but_not_mid_title_the():
    # "The Boys" and "Boys" are the same series; "Kill The Minotaur" is not
    # the same as "Kill Minotaur" losing meaning, but consistency matters more
    # than either - both sides go through the same function
    assert orphans.normalize_title('The Boys') == orphans.normalize_title('Boys')


@pytest.mark.unit
@pytest.mark.parametrize("a,b,floor", [
    ('The Amazing Spider-Man', 'Amazing Spider-Man', 0.99),
    ('Batman', 'Batman', 1.0),
    ('Batman', 'Superman', 0.0),
])
def test_title_similarity_bounds(a, b, floor):
    score = orphans.title_similarity(a, b)
    assert 0.0 <= score <= 1.0
    assert score >= floor


@pytest.mark.unit
def test_title_similarity_ranks_the_better_match_higher():
    assert (orphans.title_similarity('Saga', 'Saga')
            > orphans.title_similarity('Saga', 'Sagas of Asgard'))


# --- scoring -------------------------------------------------------------

def parsed(**kw):
    base = {'series': 'Saga', 'issue': '12', 'year': '2013',
            'booktype': 'issue', 'page_count': 28, 'comicinfo_series': None,
            'issue_total': None}
    base.update(kw)
    return base


def candidate(**kw):
    base = {'comicid': '1', 'name': 'Saga', 'comicyear': '2013',
            'issues': 60, 'publisher': 'Image', 'type': 'Print'}
    base.update(kw)
    return base


@pytest.mark.unit
def test_a_perfect_match_scores_high_and_says_why():
    result = orphans.score_candidate(parsed(), candidate())
    assert result['score'] >= 90
    assert any('title' in r.lower() for r in result['reasons'])
    assert any('year' in r.lower() for r in result['reasons'])


@pytest.mark.unit
def test_a_different_series_scores_low():
    result = orphans.score_candidate(parsed(), candidate(name='Paper Girls'))
    assert result['score'] < 50


@pytest.mark.unit
def test_year_mismatch_costs_but_does_not_disqualify():
    same = orphans.score_candidate(parsed(), candidate())
    off_by_one = orphans.score_candidate(parsed(), candidate(comicyear='2014'))
    way_off = orphans.score_candidate(parsed(), candidate(comicyear='1994'))
    assert same['score'] > off_by_one['score'] > way_off['score']


@pytest.mark.unit
def test_issue_number_beyond_the_run_costs():
    inside = orphans.score_candidate(parsed(issue='12'), candidate(issues=60))
    outside = orphans.score_candidate(parsed(issue='99'), candidate(issues=60))
    assert inside['score'] > outside['score']
    assert any('issue' in r.lower() for r in outside['reasons'])


@pytest.mark.unit
def test_page_count_inconsistent_with_booktype_costs():
    """A 28-page file is not a trade paperback."""
    single = orphans.score_candidate(parsed(booktype='issue', page_count=28), candidate())
    mislabelled = orphans.score_candidate(parsed(booktype='TPB', page_count=28), candidate())
    assert single['score'] > mislabelled['score']


@pytest.mark.unit
def test_comicinfo_agreement_helps():
    without = orphans.score_candidate(parsed(comicinfo_series=None), candidate())
    with_meta = orphans.score_candidate(parsed(comicinfo_series='Saga'), candidate())
    assert with_meta['score'] >= without['score']
    assert any('comicinfo' in r.lower() for r in with_meta['reasons'])


@pytest.mark.unit
def test_score_is_always_within_bounds():
    weird = [
        (parsed(series=None, issue=None, year=None, page_count=None), candidate()),
        (parsed(), candidate(name=None, comicyear=None, issues=None)),
        (parsed(year='not-a-year'), candidate(comicyear='also-not')),
        (parsed(issue='12.5'), candidate(issues='60')),
    ]
    for p, c in weird:
        result = orphans.score_candidate(p, c)
        assert 0 <= result['score'] <= 100, result


@pytest.mark.unit
def test_missing_evidence_is_reported_not_invented():
    """With nothing but a title to go on, the result must not look confident."""
    thin = orphans.score_candidate(
        parsed(series='Saga', issue=None, year=None, page_count=None, booktype=None),
        candidate(comicyear=None, issues=None))
    assert thin['evidence'] == ['title']
    assert thin['confidence'] < 1.0


# --- ranking and auto-selection -----------------------------------------

@pytest.mark.unit
def test_rank_candidates_orders_by_score_descending():
    ranked = orphans.rank_candidates(parsed(), [
        candidate(comicid='a', name='Paper Girls'),
        candidate(comicid='b', name='Saga'),
        candidate(comicid='c', name='Sagas of the Nordic'),
    ])
    assert [r['comicid'] for r in ranked][0] == 'b'
    assert [r['score'] for r in ranked] == sorted((r['score'] for r in ranked), reverse=True)


@pytest.mark.unit
def test_rank_candidates_handles_an_empty_list():
    assert orphans.rank_candidates(parsed(), []) == []


@pytest.mark.unit
def test_auto_selection_requires_a_clear_winner():
    twins = orphans.rank_candidates(parsed(), [
        candidate(comicid='a', name='Saga'),
        candidate(comicid='b', name='Saga'),
    ])
    # two identical-scoring candidates: picking one would be a coin flip
    assert orphans.auto_selection(twins) is None


@pytest.mark.unit
def test_auto_selection_picks_an_unambiguous_top_match():
    ranked = orphans.rank_candidates(parsed(), [
        candidate(comicid='a', name='Saga', comicyear='2013', issues=60),
        candidate(comicid='b', name='Paper Girls', comicyear='2015', issues=30),
    ])
    pick = orphans.auto_selection(ranked)
    assert pick is not None and pick['comicid'] == 'a'


@pytest.mark.unit
def test_auto_selection_refuses_on_thin_evidence():
    """Title alone is not enough to move someone's files."""
    ranked = orphans.rank_candidates(
        parsed(series='Saga', issue=None, year=None, page_count=None, booktype=None),
        [candidate(comicid='a', name='Saga', comicyear=None, issues=None)])
    assert orphans.auto_selection(ranked) is None


@pytest.mark.unit
def test_auto_selection_handles_empty():
    assert orphans.auto_selection([]) is None


@pytest.mark.unit
def test_corroborating_signals_cannot_rescue_a_wrong_title():
    """Year, issue number and page count all lining up says little: most
    comics published that year would corroborate equally well."""
    wrong = orphans.score_candidate(
        parsed(series='Saga', issue='12', year='2013', page_count=28, booktype='issue'),
        candidate(name='Paper Girls', comicyear='2013', issues=60))
    assert wrong['score'] <= 40


@pytest.mark.unit
def test_near_miss_titles_are_not_gated_away():
    """The gate must not punish real spelling variance."""
    near = orphans.score_candidate(
        parsed(series='Amazing Spider-Man'),
        candidate(name='The Amazing Spider-Man'))
    assert near['score'] >= 90


@pytest.mark.unit
def test_issue_year_is_matched_against_the_run_not_the_start_year():
    """Amazing Spider-Man #300 is cover-dated 1988 and belongs to the series
    that began in 1963. Comparing issue year to series start year scored that
    as a 25-year mismatch and ranked short-lived same-name series above it."""
    asm = parsed(series='Amazing Spider-Man', issue='300', year='1988',
                 booktype='issue', page_count=36)
    long_run = candidate(comicid='long', name='The Amazing Spider-Man',
                         comicyear='1963', issues=700)
    later_series = candidate(comicid='later', name='Amazing Spider-Man',
                             comicyear='1999', issues=58)
    ranked = orphans.rank_candidates(asm, [later_series, long_run])
    assert ranked[0]['comicid'] == 'long'


@pytest.mark.unit
def test_a_file_cannot_predate_its_series():
    before = orphans.score_candidate(
        parsed(year='1985'), candidate(comicyear='2013', issues=60))
    assert any('does not fit' in r for r in before['reasons'])


# --- reading real archives ----------------------------------------------
# Against genuine zip files rather than mocks: the point of probe_archive is
# that it copes with what is actually on disk, which a mock cannot show.

import io
import os
import zipfile


def make_cbz(path, pages=3, comicinfo=None, extra=None):
    png = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
    with zipfile.ZipFile(path, 'w') as z:
        for n in range(pages):
            z.writestr('%03d.png' % n, png)
        if comicinfo is not None:
            z.writestr('ComicInfo.xml', comicinfo)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return str(path)


@pytest.mark.integration
def test_probe_counts_pages_and_ignores_non_images(tmp_path):
    path = make_cbz(tmp_path / 'a.cbz', pages=5,
                    extra={'readme.txt': b'hello', 'notes/thumbs.db': b'x'})
    info = orphans.probe_archive(path)
    assert info['page_count'] == 5
    assert info['has_comicinfo'] is False


@pytest.mark.integration
def test_probe_reads_comicinfo(tmp_path):
    xml = b"""<?xml version="1.0"?>
    <ComicInfo><Series>Saga</Series><Number>12</Number><Year>2013</Year></ComicInfo>"""
    path = make_cbz(tmp_path / 'b.cbz', pages=2, comicinfo=xml)
    info = orphans.probe_archive(path)
    assert info['has_comicinfo'] is True
    assert info['comicinfo_series'] == 'Saga'
    assert info['comicinfo_issue'] == '12'
    assert info['comicinfo_year'] == '2013'


@pytest.mark.integration
def test_probe_survives_malformed_comicinfo(tmp_path):
    """Badly tagged files are exactly the ones that end up unfiled."""
    path = make_cbz(tmp_path / 'c.cbz', pages=2, comicinfo=b'<ComicInfo><Series>oops')
    info = orphans.probe_archive(path)
    assert info['has_comicinfo'] is True
    assert info['comicinfo_series'] is None
    assert info['page_count'] == 2


@pytest.mark.integration
def test_probe_survives_a_file_that_is_not_an_archive(tmp_path):
    path = tmp_path / 'not-a-comic.cbz'
    path.write_bytes(b'this is not a zip')
    info = orphans.probe_archive(str(path))
    assert info == {'page_count': None, 'has_comicinfo': False,
                    'comicinfo_series': None, 'comicinfo_issue': None,
                    'comicinfo_year': None, 'comicinfo_count': None}


@pytest.mark.integration
def test_probe_survives_a_missing_file(tmp_path):
    info = orphans.probe_archive(str(tmp_path / 'nope.cbz'))
    assert info['page_count'] is None


@pytest.mark.integration
def test_probe_finds_comicinfo_in_a_subdirectory(tmp_path):
    xml = b'<ComicInfo><Series>Paper Girls</Series></ComicInfo>'
    path = tmp_path / 'd.cbz'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('Paper Girls 01/000.png', b'\x89PNG\r\n\x1a\n')
        z.writestr('Paper Girls 01/ComicInfo.xml', xml)
    info = orphans.probe_archive(str(path))
    assert info['comicinfo_series'] == 'Paper Girls'


# --- record building -----------------------------------------------------

@pytest.mark.integration
def test_describe_prefers_comicinfo_over_the_filename(tmp_path):
    """These are the files whose names defeated the parser, so the embedded
    tag is the better source: "scan_0001.cbz" parses to series "scan" issue 1
    while its tag says Paper Girls #3."""
    path = make_cbz(tmp_path / 'scan_0001.cbz', pages=30)
    record = orphans.describe(
        path,
        parse_result={'series_name': 'scan', 'issue_number': '0001',
                      'issue_year': None, 'series_volume': None,
                      'booktype': 'issue'},
        probe={'page_count': 30, 'has_comicinfo': True,
               'comicinfo_series': 'Paper Girls', 'comicinfo_issue': '3',
               'comicinfo_year': '2015'})
    assert record['ParsedSeries'] == 'Paper Girls'
    assert record['ParsedIssue'] == '3'
    assert record['ParsedYear'] == '2015'
    assert record['HasComicInfo'] == 1


@pytest.mark.integration
def test_describe_uses_the_filename_when_there_is_no_tag(tmp_path):
    path = make_cbz(tmp_path / 'Saga 012 (2013).cbz', pages=24)
    record = orphans.describe(
        path,
        parse_result={'series_name': 'Saga', 'issue_number': '12',
                      'issue_year': '2013', 'booktype': 'issue'},
        probe={'page_count': 24, 'has_comicinfo': False})
    assert (record['ParsedSeries'], record['ParsedIssue']) == ('Saga', '12')


@pytest.mark.unit
def test_comicinfo_does_not_corroborate_itself():
    """When the series came from the tag, counting the tag again as separate
    evidence inflates the score with one fact used twice."""
    record = {'ParsedSeries': 'Paper Girls', 'ComicInfoSeries': 'Paper Girls',
              'ParsedIssue': '3', 'ParsedYear': '2015', 'BookType': 'issue',
              'PageCount': 30}
    assert orphans.to_parsed(record)['comicinfo_series'] is None

    disagreeing = dict(record, ParsedSeries='Something Else')
    assert orphans.to_parsed(disagreeing)['comicinfo_series'] == 'Paper Girls'


@pytest.mark.integration
def test_describe_falls_back_to_comicinfo_when_the_name_is_useless(tmp_path):
    path = make_cbz(tmp_path / 'scan_0001.cbz', pages=30)
    record = orphans.describe(
        path,
        parse_result={},
        probe={'page_count': 30, 'has_comicinfo': True,
               'comicinfo_series': 'Paper Girls', 'comicinfo_issue': '3',
               'comicinfo_year': '2015'})
    assert record['ParsedSeries'] == 'Paper Girls'
    assert record['ParsedIssue'] == '3'
    assert record['Status'] == 'new'


@pytest.mark.integration
def test_describe_records_the_real_file_size(tmp_path):
    path = make_cbz(tmp_path / 'x.cbz', pages=2)
    record = orphans.describe(path)
    assert record['FileSize'] == os.path.getsize(path)
    assert record['FileName'] == 'x.cbz'


@pytest.mark.unit
def test_describe_survives_a_path_that_does_not_exist():
    record = orphans.describe('/nowhere/at/all.cbz')
    assert record['FileSize'] is None
    assert record['Status'] == 'new'


@pytest.mark.unit
def test_to_parsed_round_trips_into_scoring():
    record = orphans.describe('/x/Saga 12.cbz',
                              parse_result={'series_name': 'Saga',
                                            'issue_number': '12',
                                            'issue_year': '2013',
                                            'booktype': 'issue'},
                              probe={'page_count': 28})
    result = orphans.score_candidate(orphans.to_parsed(record), candidate())
    assert result['score'] >= 90


# --- scanning ------------------------------------------------------------

@pytest.mark.integration
def test_scan_directory_finds_comics_and_ignores_everything_else(tmp_path):
    make_cbz(tmp_path / 'Saga 012 (2013).cbz', pages=24)
    (tmp_path / 'nested').mkdir()
    make_cbz(tmp_path / 'nested' / 'x.cbz', pages=10)
    (tmp_path / 'notes.txt').write_bytes(b'nope')
    (tmp_path / '.hidden.cbz').write_bytes(b'nope')

    found = orphans.scan_directory(str(tmp_path))
    names = sorted(r['FileName'] for r in found)
    assert names == ['Saga 012 (2013).cbz', 'x.cbz']


@pytest.mark.integration
def test_scan_directory_skips_paths_already_known(tmp_path):
    a = make_cbz(tmp_path / 'a.cbz', pages=3)
    make_cbz(tmp_path / 'b.cbz', pages=3)
    found = orphans.scan_directory(str(tmp_path), known_paths=[a])
    assert [r['FileName'] for r in found] == ['b.cbz']


@pytest.mark.unit
def test_scan_directory_returns_empty_for_a_bad_root(tmp_path):
    assert orphans.scan_directory(str(tmp_path / 'nope')) == []
    assert orphans.scan_directory(None) == []
    assert orphans.scan_directory('') == []


@pytest.mark.integration
def test_scan_reads_page_counts_from_the_files_it_finds(tmp_path):
    make_cbz(tmp_path / 'a.cbz', pages=7)
    found = orphans.scan_directory(str(tmp_path))
    assert found[0]['PageCount'] == 7


# --- cleaning the search query ------------------------------------------
# "Batman (1940) Volume 01 Issue 014.cbr" parses to the series
# 'Batman (1940) Volume01 ', which ComicVine returns nothing for.

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ('Batman (1940) Volume01 ', 'Batman'),
    ('Batman (1940) Volume 01', 'Batman'),
    ('Saga', 'Saga'),
    ('Amazing Spider-Man', 'Amazing Spider-Man'),
    ('068 - BPRD - The Black Flame', 'BPRD - The Black Flame'),
    ('000 - Marvel Official Checklist', 'Marvel Official Checklist'),
    ('X-Men v2', 'X-Men'),
    ('Fables Vol. 3', 'Fables'),
    ('   Spaced   Out   ', 'Spaced Out'),
    ('', ''),
    (None, ''),
])
def test_clean_series_query(raw, expected):
    assert orphans.clean_series_query(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ['100 Bullets', '2000 AD', '30 Days of Night'])
def test_clean_series_query_keeps_numbers_that_are_part_of_the_title(raw):
    """A leading number is only a reading-order prefix when a separator
    follows it - '100 Bullets' is the series name."""
    assert orphans.clean_series_query(raw) == raw


@pytest.mark.unit
def test_clean_series_query_never_empties_a_real_title():
    """Stripping must not leave nothing behind - a bad query is recoverable,
    an empty one just fails."""
    for raw in ['(1940)', 'Volume 01', 'v2', '068 -']:
        assert orphans.clean_series_query(raw) == raw.strip()


@pytest.mark.unit
def test_describe_stores_a_searchable_series_name():
    record = orphans.describe(
        '/x/Batman (1940) Volume 01 Issue 014.cbr',
        parse_result={'series_name': 'Batman (1940) Volume01 ',
                      'issue_number': '014', 'issue_year': '1940'})
    assert record['ParsedSeries'] == 'Batman'


@pytest.mark.unit
def test_scoring_uses_the_same_cleaned_title_as_the_search():
    """Scoring must use the cleaned title too: matching candidates against
    'Batman (1940) Volume01 ' ranks Batman (1940) below every series with a
    longer name."""
    record = {'ParsedSeries': 'Batman (1940) Volume01 ', 'ParsedIssue': '014',
              'ParsedYear': '1940', 'BookType': 'issue', 'PageCount': 54,
              'ComicInfoSeries': None}
    assert orphans.to_parsed(record)['series'] == 'Batman'

    right = orphans.score_candidate(orphans.to_parsed(record),
                                    {'comicid': '1', 'name': 'Batman',
                                     'comicyear': '1940', 'issues': 716})
    wrong = orphans.score_candidate(orphans.to_parsed(record),
                                    {'comicid': '2', 'name': 'Batman Album',
                                     'comicyear': '1976', 'issues': 20})
    assert right['score'] > wrong['score']


# --- issue totals ("3 of 12") -------------------------------------------
# A mini-series' length is one of the strongest signals available: a file that
# says "of 12" cannot belong to a 700-issue run.

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ('Fables 003 (of 12) (2002).cbz', 12),
    ('Some Series 01 (of 7).cbr', 7),
    ('Watchmen 03 of 12.cbz', 12),
    ('Batman 404.cbz', None),
    ('Series (2013) 05.cbz', None),
    ('', None),
    (None, None),
])
def test_parse_issue_total(raw, expected):
    assert orphans.parse_issue_total(raw) == expected


@pytest.mark.unit
def test_parse_issue_total_ignores_a_year_that_looks_like_a_count():
    assert orphans.parse_issue_total('Series 01 (of 2013).cbz') is None


@pytest.mark.integration
def test_probe_reads_the_issue_count_from_comicinfo(tmp_path):
    xml = b'<ComicInfo><Series>Fables</Series><Number>3</Number><Count>12</Count></ComicInfo>'
    path = make_cbz(tmp_path / 'f.cbz', pages=4, comicinfo=xml)
    assert orphans.probe_archive(path)['comicinfo_count'] == '12'


@pytest.mark.unit
def test_describe_prefers_the_tagged_count_then_the_filename():
    tagged = orphans.describe('/x/Fables 003 (of 12).cbz',
                              probe={'comicinfo_count': '12'})
    assert tagged['TotalIssues'] == 12

    from_name = orphans.describe('/x/Fables 003 (of 12).cbz', probe={})
    assert from_name['TotalIssues'] == 12

    neither = orphans.describe('/x/Batman 404.cbz', probe={})
    assert neither['TotalIssues'] is None


# --- scoring on the issue total -----------------------------------------

@pytest.mark.unit
def test_a_matching_issue_count_is_strong_evidence():
    mini = parsed(series='Fables', issue='3', year='2002', issue_total=12)
    right = orphans.score_candidate(mini, candidate(name='Fables', comicyear='2002', issues=12))
    wrong = orphans.score_candidate(mini, candidate(name='Fables', comicyear='2002', issues=150))
    assert right['score'] > wrong['score']
    assert any('12' in r for r in right['reasons'])


@pytest.mark.unit
def test_issue_total_is_optional_and_absent_from_evidence_when_unknown():
    without = orphans.score_candidate(parsed(issue_total=None), candidate())
    assert 'issue_total' not in without['evidence']

    with_total = orphans.score_candidate(parsed(issue_total=60), candidate(issues=60))
    assert 'issue_total' in with_total['evidence']


@pytest.mark.unit
def test_a_long_run_cannot_be_a_twelve_issue_mini():
    """Fables 3 'of 12' should not match a 150-issue ongoing just because
    issue 3 fits inside it."""
    mini = parsed(series='Fables', issue='3', year='2002', issue_total=12)
    ranked = orphans.rank_candidates(mini, [
        candidate(comicid='ongoing', name='Fables', comicyear='2002', issues=150),
        candidate(comicid='mini', name='Fables', comicyear='2002', issues=12),
    ])
    assert ranked[0]['comicid'] == 'mini'


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ('12', '12'), (12, '12'), ('012', '12'), (' 7 ', '7'),
    ('12.5', None),      # findComic only uses whole numbers
    ('15A', '15'),
    (None, None), ('', None), ('abc', None),
])
def test_as_search_issue(raw, expected):
    assert orphans.as_search_issue(raw) == expected


# --- filing: choosing how to place the file -----------------------------
# Never a move: the original stays in the orphans directory. A hardlink when
# the two paths share a filesystem (no extra bytes, both names point at the
# same data), a copy when they cannot.

@pytest.mark.integration
def test_placement_prefers_a_hardlink_on_the_same_filesystem(tmp_path):
    src = make_cbz(tmp_path / 'src.cbz', pages=2)
    dst_dir = tmp_path / 'series'
    dst_dir.mkdir()
    assert orphans.choose_placement(src, str(dst_dir)) == 'hardlink'


@pytest.mark.integration
def test_placement_falls_back_to_copy_across_filesystems(tmp_path, monkeypatch):
    src = make_cbz(tmp_path / 'src.cbz', pages=2)
    dst_dir = tmp_path / 'series'
    dst_dir.mkdir()

    real_stat = os.stat

    class FakeStat:
        def __init__(self, dev):
            self.st_dev = dev

    def fake_stat(path, *a, **k):
        return FakeStat(1) if str(path).endswith('src.cbz') else FakeStat(2)

    monkeypatch.setattr(orphans.os, 'stat', fake_stat)
    assert orphans.choose_placement(src, str(dst_dir)) == 'copy'


@pytest.mark.integration
def test_placement_says_copy_when_it_cannot_tell(tmp_path):
    assert orphans.choose_placement('/nowhere/x.cbz', str(tmp_path)) == 'copy'


@pytest.mark.integration
def test_place_file_hardlinks_without_consuming_space(tmp_path):
    src = make_cbz(tmp_path / 'src.cbz', pages=3)
    dst = tmp_path / 'series' / 'Filed 001.cbz'
    (tmp_path / 'series').mkdir()

    used = orphans.place_file(src, str(dst), 'hardlink')
    assert used == 'hardlink'
    assert os.path.exists(src), 'the original must stay put'
    assert os.stat(src).st_ino == os.stat(dst).st_ino


@pytest.mark.integration
def test_place_file_copies_when_asked(tmp_path):
    src = make_cbz(tmp_path / 'src.cbz', pages=3)
    dst = tmp_path / 'series' / 'Filed 001.cbz'
    (tmp_path / 'series').mkdir()

    used = orphans.place_file(src, str(dst), 'copy')
    assert used == 'copy'
    assert os.path.exists(src)
    assert os.stat(src).st_ino != os.stat(dst).st_ino
    assert os.path.getsize(src) == os.path.getsize(dst)


@pytest.mark.integration
def test_place_file_falls_back_to_copy_if_linking_fails(tmp_path, monkeypatch):
    """Some filesystems refuse hardlinks even within one device."""
    src = make_cbz(tmp_path / 'src.cbz', pages=2)
    dst = tmp_path / 'series' / 'x.cbz'
    (tmp_path / 'series').mkdir()

    def refuse(*a, **k):
        raise OSError('cross-device link')

    monkeypatch.setattr(orphans.os, 'link', refuse)
    assert orphans.place_file(src, str(dst), 'hardlink') == 'copy'
    assert os.path.exists(dst)


@pytest.mark.integration
def test_place_file_refuses_to_overwrite(tmp_path):
    src = make_cbz(tmp_path / 'src.cbz', pages=2)
    dst_dir = tmp_path / 'series'
    dst_dir.mkdir()
    dst = dst_dir / 'x.cbz'
    dst.write_bytes(b'existing file')

    with pytest.raises(orphans.FilingRefused):
        orphans.place_file(src, str(dst), 'hardlink')
    assert dst.read_bytes() == b'existing file'


# --- filing: working out what would happen ------------------------------

def issue_row(**kw):
    base = {'IssueID': 'i1', 'Issue_Number': '153', 'Int_IssueNumber': 153000,
            'ComicID': 'c1'}
    base.update(kw)
    return base


def comic_row(**kw):
    base = {'ComicID': 'c1', 'ComicName': 'Fables', 'ComicYear': '2002',
            'ComicLocation': '/comics/Fables'}
    base.update(kw)
    return base


@pytest.mark.unit
@pytest.mark.parametrize("wanted,expected", [
    ('153', 'i1'), (153, 'i1'), ('0153', 'i1'), ('153.0', 'i1'),
    ('154', 'i2'), ('999', None), (None, None), ('', None), ('abc', None),
])
def test_match_issue(wanted, expected):
    issues = [issue_row(), issue_row(IssueID='i2', Issue_Number='154', Int_IssueNumber=154000)]
    match = orphans.match_issue(issues, wanted)
    assert (match or {}).get('IssueID') == expected


@pytest.mark.unit
def test_match_issue_handles_an_empty_series():
    assert orphans.match_issue([], '1') is None


@pytest.mark.unit
def test_blockers_reports_an_unwatched_series():
    blockers = orphans.filing_blockers({'FilePath': __file__}, None, None)
    assert any(b['code'] == 'series_not_watched' for b in blockers)


@pytest.mark.unit
def test_blockers_reports_a_missing_issue():
    blockers = orphans.filing_blockers({'FilePath': __file__}, comic_row(), None)
    assert any(b['code'] == 'issue_not_found' for b in blockers)


@pytest.mark.unit
def test_blockers_reports_a_series_with_no_folder():
    blockers = orphans.filing_blockers({'FilePath': __file__},
                                       comic_row(ComicLocation=None), issue_row())
    assert any(b['code'] == 'no_series_folder' for b in blockers)


@pytest.mark.unit
def test_blockers_reports_a_source_that_has_gone_away():
    blockers = orphans.filing_blockers({'FilePath': '/nowhere/gone.cbz'},
                                       comic_row(), issue_row())
    assert any(b['code'] == 'source_missing' for b in blockers)


@pytest.mark.unit
def test_no_blockers_when_everything_lines_up():
    assert orphans.filing_blockers({'FilePath': __file__}, comic_row(), issue_row()) == []


@pytest.mark.unit
def test_build_plan_describes_the_whole_operation():
    plan = orphans.build_plan(
        record={'OrphanID': 'o1', 'FilePath': '/orphans/DCP_batch_07.cbz',
                'FileName': 'DCP_batch_07.cbz'},
        comic=comic_row(), issue=issue_row(),
        destination_name='Fables 153 (2002).cbz', placement='hardlink')

    assert plan['source'] == '/orphans/DCP_batch_07.cbz'
    assert plan['destination'] == '/comics/Fables/Fables 153 (2002).cbz'
    assert plan['placement'] == 'hardlink'
    assert plan['series'] == 'Fables'
    assert plan['issue'] == '153'
    assert plan['issueid'] == 'i1'
    assert plan['original_kept'] is True


# --- filing: metadata forces a copy -------------------------------------

@pytest.mark.integration
def test_writing_metadata_forces_a_copy_even_on_one_filesystem(tmp_path):
    """A hardlink is a second name for the same bytes, so tagging the filed
    file would rewrite the original too. Metadata means copy, always."""
    src = make_cbz(tmp_path / 'src.cbz', pages=2)
    dst_dir = tmp_path / 'series'
    dst_dir.mkdir()

    assert orphans.choose_placement(src, str(dst_dir)) == 'hardlink'
    assert orphans.choose_placement(src, str(dst_dir), write_metadata=True) == 'copy'


@pytest.mark.integration
def test_tagging_a_hardlink_would_have_modified_the_original(tmp_path):
    """With a hardlink there is only one set of bytes, so a write through
    either name changes both."""
    src = make_cbz(tmp_path / 'src.cbz', pages=2)
    linked = tmp_path / 'linked.cbz'
    os.link(src, linked)

    before = os.path.getsize(src)
    with open(linked, 'ab') as handle:
        handle.write(b'metadata would go here')

    assert os.path.getsize(src) != before, 'writing through the link changed the original'


@pytest.mark.unit
@pytest.mark.parametrize("placement,size,expected", [
    ('copy', 1000, 1000),
    ('hardlink', 1000, 0),
    ('copy', None, None),
    ('hardlink', None, 0),
])
def test_duplicate_bytes(placement, size, expected):
    """What filing actually costs in disk - zero for a hardlink."""
    assert orphans.duplicate_bytes(placement, size) == expected


# --- which issue, and how confident are we -------------------------------
# Identify picks the series. The issue must not be silently inherited from a
# filename: for DCP_batch_07.cbz the "07" is a scanner batch number.

@pytest.mark.unit
@pytest.mark.parametrize("chosen,parsed,expected", [
    ('i153', '07', 'chosen'),
    (None, '07', 'filename'),
    (None, None, 'none'),
])
def test_issue_source_is_reported(chosen, parsed, expected):
    assert orphans.issue_source(chosen, parsed) == expected


@pytest.mark.unit
def test_a_guessed_issue_is_warned_about():
    warnings = orphans.filing_warnings(
        {'BookType': 'issue', 'FileName': 'DCP_batch_07.cbz'},
        issue_row(Issue_Number='7'), 'filename')
    assert any(w['code'] == 'issue_guessed' for w in warnings)


@pytest.mark.unit
def test_a_chosen_issue_is_not_warned_about():
    warnings = orphans.filing_warnings(
        {'BookType': 'issue', 'FileName': 'DCP_batch_07.cbz'},
        issue_row(), 'chosen')
    assert not any(w['code'] == 'issue_guessed' for w in warnings)


@pytest.mark.unit
@pytest.mark.parametrize("booktype", ['TPB', 'HC', 'GN', 'TPB/GN/HC/One-Shot'])
def test_filing_a_collection_as_one_issue_is_warned_about(booktype):
    """A trade or a pack is not a single issue, and filing it as one puts a
    whole collection under one issue number."""
    warnings = orphans.filing_warnings(
        {'BookType': booktype, 'FileName': 'x.cbz'}, issue_row(), 'chosen')
    assert any(w['code'] == 'not_a_single_issue' for w in warnings)


@pytest.mark.unit
def test_a_normal_issue_produces_no_warnings():
    assert orphans.filing_warnings(
        {'BookType': 'issue', 'FileName': 'x.cbz'}, issue_row(), 'chosen') == []


# --- finding the issue by name, not just number -------------------------

@pytest.mark.unit
def test_search_issues_matches_number_or_name():
    issues = [
        issue_row(IssueID='a', Issue_Number='153', IssueName='The Black Forest'),
        issue_row(IssueID='b', Issue_Number='7', IssueName='Bag o Bones'),
        issue_row(IssueID='c', Issue_Number='154', IssueName='Forest Fire'),
    ]
    assert [i['IssueID'] for i in orphans.search_issues(issues, 'black forest')] == ['a']
    assert [i['IssueID'] for i in orphans.search_issues(issues, 'forest')] == ['a', 'c']
    assert [i['IssueID'] for i in orphans.search_issues(issues, '153')] == ['a']
    assert [i['IssueID'] for i in orphans.search_issues(issues, '')] == ['a', 'b', 'c']
    assert orphans.search_issues(issues, 'nothing here') == []


@pytest.mark.unit
def test_search_issues_survives_missing_names():
    issues = [issue_row(IssueID='a', IssueName=None)]
    assert orphans.search_issues(issues, 'anything') == []
    assert [i['IssueID'] for i in orphans.search_issues(issues, '153')] == ['a']


@pytest.mark.integration
def test_a_hardlink_can_carry_a_completely_different_name(tmp_path):
    """Renaming is free with a hardlink: the name lives in the directory
    entry, the bytes live in the inode. Only changing contents needs a copy.
    """
    src = make_cbz(tmp_path / 'DCP_batch_07.cbz', pages=3)
    dst_dir = tmp_path / 'Fables'
    dst_dir.mkdir()
    dst = dst_dir / 'Fables 153 (2022).cbz'

    assert orphans.place_file(src, str(dst), 'hardlink') == 'hardlink'
    assert os.path.basename(src) != os.path.basename(str(dst))
    assert os.stat(src).st_ino == os.stat(dst).st_ino, 'same bytes, two names'
    assert os.stat(src).st_nlink == 2


# --- metadata as a per-file choice --------------------------------------
# ENABLE_META is the default, not a gate - manual_metatag ignores it too,
# because tagging one file on purpose differs from tagging everything.

@pytest.mark.unit
@pytest.mark.parametrize("cr,cbl,expected", [
    (True, False, True),
    (False, True, True),
    (True, True, True),
    (False, False, False),
])
def test_tagging_possible_needs_a_tag_format(cr, cbl, expected):
    """With neither format selected there is nothing to write."""
    assert orphans.tagging_possible(cr, cbl) is expected


@pytest.mark.unit
@pytest.mark.parametrize("requested,possible,expected", [
    ('1', True, True),
    ('0', True, False),
    (None, True, False),
    ('1', False, False),      # asked for, but no format configured
    ('true', True, True),
    ('True', True, True),
    ('anything else', True, False),
])
def test_should_write_metadata(requested, possible, expected):
    assert orphans.should_write_metadata(requested, possible) is expected


@pytest.mark.unit
def test_the_global_setting_does_not_veto_a_per_file_choice():
    """ENABLE_META off must not stop someone tagging this one file."""
    assert orphans.should_write_metadata('1', tagging_possible=True) is True


# --- history line for a rescued orphan ----------------------------------

@pytest.mark.unit
def test_history_row_describes_the_rescue():
    row = orphans.history_row(
        record={'FilePath': '/orphans/DCP_batch_07.cbz', 'FileSize': 126027},
        comic=comic_row(), issue=issue_row(), destination='/comics/Fables/Fables 153 (2022).cbz',
        when='2026-08-27 12:00:00')

    assert row['IssueID'] == 'i1'
    assert row['ComicID'] == 'c1'
    assert row['ComicName'] == 'Fables'
    assert row['Issue_Number'] == '153'
    assert row['Size'] == 126027
    assert row['DateAdded'] == '2026-08-27 12:00:00'
    # Provider is where it came from, which for these files is the orphan pile
    assert row['Provider'] == 'Orphan'
    # Post-Processed rather than a bespoke status so the existing
    # "Clear Processed" button still reaches these rows
    assert row['Status'] == 'Post-Processed'
    assert row['FolderName'] == '/comics/Fables'


@pytest.mark.unit
def test_history_row_survives_a_missing_size():
    row = orphans.history_row({'FilePath': '/x.cbz', 'FileSize': None}, comic_row(),
                              issue_row(), '/comics/Fables/x.cbz', 'now')
    assert row['Size'] is None
