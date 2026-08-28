#  This file is part of Mylar.
#
#  Mylar is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Mylar is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Mylar.  If not, see <http://www.gnu.org/licenses/>.

"""Identifying comic files that are not organised to any known standard.

Files whose names defeat the importer's parser end up in librarysync's
failure_list and are otherwise unreachable. This scores ComicVine candidates
against what can be read off such a file - name, embedded ComicInfo.xml, page
count - and reports why each scored as it did.

Scoring uses only the evidence actually available, rescaled against what was
achievable, so a title-only guess cannot reach the same number as a
corroborated match. auto_selection() is deliberately hard to satisfy because
it drives moving files on disk.
"""

import difflib
import math
import re

# Status values for an orphan record.
ORPHAN_STATUSES = frozenset(['new', 'identified', 'filed', 'ignored'])

# Weights per signal. Only the signals actually available are counted, and the
# raw total is rescaled against what was achievable - see score_candidate.
WEIGHTS = {
    'title': 50,
    'year': 20,
    'issue_range': 15,
    'page_count': 10,
    'comicinfo': 5,
    # A run length is nearly identifying on its own for minis: "of 12" rules
    # out every ongoing, which is something no other signal here can do.
    'issue_total': 25,
}

# A single issue is tens of pages; a collection is normally well over a
# hundred. These are deliberately loose - they exist to catch a file labelled
# TPB that is plainly a single issue, not to adjudicate borderline cases.
SINGLE_ISSUE_MAX_PAGES = 65
COLLECTION_MIN_PAGES = 60

COLLECTION_BOOKTYPES = frozenset(['tpb', 'hc', 'gn', 'trade paperback', 'hardcover'])

AUTO_SELECT_MIN_SCORE = 90
AUTO_SELECT_MIN_MARGIN = 10

_PUNCTUATION = re.compile(r"[^a-z0-9]+")
# Articles are dropped wherever they appear, not just leading: the two sources
# disagree about them freely ("Batman and the Outsiders" vs "Batman and
# Outsiders"), and both sides go through this same function.
_ARTICLES = re.compile(r"\b(the|a|an)\b")

# Below this, the title simply does not support the match, and nothing else can
# vouch for identity - a right year and a plausible issue number say nothing
# about *which* series a file belongs to.
TITLE_GATE = 0.5


def normalize_title(text):
    """Fold a series title down to something two sources can be compared on."""
    if not text:
        return ''
    folded = str(text).lower()
    folded = folded.replace("'", "")
    folded = _PUNCTUATION.sub(' ', folded).strip()
    folded = _ARTICLES.sub(' ', folded)
    return re.sub(r'\s+', ' ', folded).strip()


# Bits that filenames carry but ComicVine titles do not. Applied to the search
# query only; the original filename stays visible in the UI.
_QUERY_YEAR = re.compile(r"\(\s*(?:18|19|20)\d{2}\s*\)")
_QUERY_VOLUME = re.compile(r"\b(?:volume|vol\.?|v)\s*\.?\s*\d{1,3}\b", re.I)
_QUERY_ISSUE_WORD = re.compile(r"\bissue\b\s*\d*", re.I)
# A leading number is a reading-order prefix only when a separator follows it -
# otherwise it is part of the title, as in "100 Bullets" or "2000 AD".
_QUERY_ORDER_PREFIX = re.compile(r"^\d{1,4}\s*[-_]\s*")


# "3 (of 12)" and "3 of 12" both occur. The count is bounded to rule out a
# year being read as a run length - "(of 2013)" is not a 2013-issue series.
_ISSUE_TOTAL = re.compile(r"\bof\s+(\d{1,3})\b", re.I)
MAX_PLAUSIBLE_RUN = 999


def parse_issue_total(text):
    """Pull the run length out of a filename, if it declares one."""
    if not text:
        return None
    match = _ISSUE_TOTAL.search(str(text))
    if not match:
        return None
    total = int(match.group(1))
    if total <= 0 or total > MAX_PLAUSIBLE_RUN:
        return None
    return total


def clean_series_query(series):
    """Strip filename debris that would defeat a ComicVine search.

    If stripping would leave nothing, the original is returned - a poor query
    can still be corrected by hand, an empty one cannot.
    """
    if not series:
        return ''

    cleaned = str(series)
    cleaned = _QUERY_ORDER_PREFIX.sub('', cleaned)
    cleaned = _QUERY_YEAR.sub(' ', cleaned)
    cleaned = _QUERY_VOLUME.sub(' ', cleaned)
    cleaned = _QUERY_ISSUE_WORD.sub(' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -_')

    return cleaned or str(series).strip()


def title_similarity(left, right):
    """0.0 - 1.0 similarity between two series titles."""
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_issue_number(value):
    """Issue numbers are not integers - 12.5, 1A and #0 all occur."""
    if value is None:
        return None
    match = re.search(r'\d+(?:\.\d+)?', str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _estimated_run_years(issue_count):
    """Roughly how many years a run of this many issues would span.

    Twelve a year plus two years of slack for late shipping and cover-date
    drift. Only used to sanity-check a year, so loose is fine.
    """
    total = _as_int(issue_count)
    if total is None or total <= 0:
        return None
    return int(math.ceil(total / 12.0)) + 2


def _score_year(parsed_year, candidate_year, candidate_issues=None):
    """Is the issue year plausible for this series? 0-1, or None if unknowable.

    Compared against the series run rather than its start year: a 1988 issue
    of a series that began in 1963 is not a 25-year mismatch.
    """
    left = _as_int(parsed_year)
    right = _as_int(candidate_year)
    if left is None or right is None:
        return None

    delta = left - right
    if delta < -1:
        # the file predates the series - it cannot belong to it
        return 0.0
    if delta == -1:
        # cover dates run ahead of publication, and CV start years vary by one
        return 0.6

    run = _estimated_run_years(candidate_issues)
    if run is None:
        # no issue count to reason with: same year is strong, later is neutral
        return 1.0 if delta == 0 else 0.5
    if delta <= run:
        return 1.0
    if delta <= run + 5:
        return 0.5
    return 0.0


def _score_issue_range(parsed_issue, candidate_issues):
    """Whether the issue number could exist in a run this long."""
    number = _as_issue_number(parsed_issue)
    total = _as_int(candidate_issues)
    if number is None or total is None or total <= 0:
        return None
    if number <= total:
        return 1.0
    # a little slack: CV issue counts lag ongoing series
    if number <= total + 2:
        return 0.5
    return 0.0


def _score_page_count(booktype, page_count):
    """Whether the page count is consistent with what the file claims to be."""
    pages = _as_int(page_count)
    if pages is None or not booktype:
        return None
    kind = str(booktype).strip().lower()
    if kind in COLLECTION_BOOKTYPES:
        return 1.0 if pages >= COLLECTION_MIN_PAGES else 0.0
    if kind in ('issue', 'one-shot', 'oneshot', 'annual'):
        return 1.0 if pages <= SINGLE_ISSUE_MAX_PAGES else 0.0
    return None


def score_candidate(parsed, candidate):
    """Score one ComicVine candidate against what was read off the file.

    Returns the candidate's fields plus:
        score       0-100, computed over available evidence only
        reasons     human-readable notes, for showing the user why
        evidence    which signals contributed
        confidence  fraction of the total weight that was actually available
    """
    reasons = []
    evidence = []
    earned = 0.0
    available = 0.0

    similarity = title_similarity(parsed.get('series'), candidate.get('name'))
    if parsed.get('series') and candidate.get('name'):
        available += WEIGHTS['title']
        earned += similarity * WEIGHTS['title']
        evidence.append('title')
        reasons.append('Title %d%% similar to "%s"'
                       % (round(similarity * 100), candidate.get('name')))

    year = _score_year(parsed.get('year'), candidate.get('comicyear'),
                       candidate.get('issues'))
    if year is not None:
        available += WEIGHTS['year']
        earned += year * WEIGHTS['year']
        evidence.append('year')
        if year == 1.0:
            reasons.append('Year %s falls within the run of a series starting %s'
                           % (parsed.get('year'), candidate.get('comicyear')))
        elif year > 0:
            reasons.append('Year %s is plausible for a series starting %s'
                           % (parsed.get('year'), candidate.get('comicyear')))
        else:
            reasons.append('Year %s does not fit a series starting %s'
                           % (parsed.get('year'), candidate.get('comicyear')))

    in_range = _score_issue_range(parsed.get('issue'), candidate.get('issues'))
    if in_range is not None:
        available += WEIGHTS['issue_range']
        earned += in_range * WEIGHTS['issue_range']
        evidence.append('issue_range')
        if in_range == 1.0:
            reasons.append('Issue %s fits a run of %s'
                           % (parsed.get('issue'), candidate.get('issues')))
        else:
            reasons.append('Issue %s is beyond the %s issues on record'
                           % (parsed.get('issue'), candidate.get('issues')))

    pages = _score_page_count(parsed.get('booktype'), parsed.get('page_count'))
    if pages is not None:
        available += WEIGHTS['page_count']
        earned += pages * WEIGHTS['page_count']
        evidence.append('page_count')
        if pages == 1.0:
            reasons.append('%s pages is consistent with a %s'
                           % (parsed.get('page_count'), parsed.get('booktype')))
        else:
            reasons.append('%s pages is not consistent with a %s'
                           % (parsed.get('page_count'), parsed.get('booktype')))

    declared_total = _as_int(parsed.get('issue_total'))
    candidate_total = _as_int(candidate.get('issues'))
    if declared_total is not None and candidate_total is not None and candidate_total > 0:
        available += WEIGHTS['issue_total']
        # CV counts drift by an issue or two on ongoing series, so allow a
        # little slack before calling it a mismatch.
        drift = abs(declared_total - candidate_total)
        if drift == 0:
            fit = 1.0
        elif drift <= 2:
            fit = 0.7
        else:
            fit = 0.0
        earned += fit * WEIGHTS['issue_total']
        evidence.append('issue_total')
        if fit == 1.0:
            reasons.append('Declared run of %s issues matches exactly' % declared_total)
        elif fit > 0:
            reasons.append('Declared run of %s issues is close to %s'
                           % (declared_total, candidate_total))
        else:
            reasons.append('Declared run of %s issues does not match %s'
                           % (declared_total, candidate_total))

    if parsed.get('comicinfo_series'):
        meta_similarity = title_similarity(parsed.get('comicinfo_series'),
                                           candidate.get('name'))
        available += WEIGHTS['comicinfo']
        earned += meta_similarity * WEIGHTS['comicinfo']
        evidence.append('comicinfo')
        reasons.append('ComicInfo.xml says "%s"' % parsed.get('comicinfo_series'))

    if available <= 0:
        score = 0
    else:
        score = int(round((earned / available) * 100))

    # Title acts as a gate rather than just another weight. Without it, a
    # different series with a matching year, a plausible issue number and a
    # sensible page count scores respectably - all of which is true of most
    # comics published that year.
    if 'title' in evidence and similarity < TITLE_GATE:
        score = min(score, int(round(similarity * 100)))

    total_weight = float(sum(WEIGHTS.values()))
    result = dict(candidate)
    result.update({
        'score': max(0, min(100, score)),
        'reasons': reasons,
        'evidence': evidence,
        'confidence': round(available / total_weight, 2),
    })
    return result


def rank_candidates(parsed, candidates):
    """Score every candidate, best first."""
    scored = [score_candidate(parsed, c) for c in (candidates or [])]
    scored.sort(key=lambda c: (c['score'], c['confidence']), reverse=True)
    return scored


def auto_selection(ranked):
    """The one candidate confident enough to preselect, or None.

    Requires a high score, corroboration beyond the title, and a clear gap to
    second place, because this drives moving files on disk.
    """
    if not ranked:
        return None

    best = ranked[0]
    if best['score'] < AUTO_SELECT_MIN_SCORE:
        return None
    if len(best['evidence']) < 2:
        return None
    if len(ranked) > 1 and (best['score'] - ranked[1]['score']) < AUTO_SELECT_MIN_MARGIN:
        return None
    return best


# --- reading what a file can tell us about itself -----------------------
# I/O lives here at the edge; everything above this line is pure.

COMIC_EXTENSIONS = ('.cbr', '.cbz', '.cb7', '.pdf')


def _debug(message):
    """Log without ever raising.

    logger.fdebug reads a config value that is not always populated, so an
    unguarded log call inside an except block can defeat the handler.
    """
    try:
        from mylar import logger
        logger.fdebug(message)
    except Exception:
        pass


def probe_archive(path):
    """Read page count and any embedded ComicInfo.xml out of a comic archive.

    Never raises; unreadable values come back as None. A corrupt archive is an
    ordinary outcome here rather than an error.
    """
    from mylar import getimage

    result = {'page_count': None, 'has_comicinfo': False,
              'comicinfo_series': None, 'comicinfo_issue': None,
              'comicinfo_year': None, 'comicinfo_count': None}

    archive = None
    try:
        opened = getimage.open_archive(path)
        if not opened:
            return result
        archive = opened[0]
        result['page_count'] = getimage.page_count(archive)

        names = archive.namelist()
        info_name = next(
            (n for n in names if n.lower().endswith('comicinfo.xml')), None
        )
        if info_name:
            result['has_comicinfo'] = True
            result.update(_parse_comicinfo(archive.read(info_name)))
    except Exception as e:
        _debug('[ORPHANS] Could not read %s: %s' % (path, e))
    finally:
        try:
            if archive is not None:
                archive.close()
        except Exception:
            pass

    return result


def _parse_comicinfo(raw):
    """Pull the few fields worth having out of a ComicInfo.xml blob."""
    from xml.etree import ElementTree

    out = {'comicinfo_series': None, 'comicinfo_issue': None,
           'comicinfo_year': None, 'comicinfo_count': None}
    try:
        root = ElementTree.fromstring(raw)
    except Exception:
        # Malformed metadata is common in files that were never properly
        # tagged; the filename is still usable, so this is not fatal.
        return out

    def text(tag):
        node = root.find(tag)
        if node is None or node.text is None:
            return None
        value = node.text.strip()
        return value or None

    out['comicinfo_series'] = text('Series')
    out['comicinfo_issue'] = text('Number')
    out['comicinfo_year'] = text('Year')
    # <Count> is the series total - the tagged equivalent of "of 12"
    out['comicinfo_count'] = text('Count')
    return out


def describe(path, parse_result=None, probe=None, orphanid=None):
    """Build an orphan record from a path plus what could be read off it.

    parse_result is a FileChecker single-file result, probe a probe_archive()
    result; both optional, and supplied by scan_directory.
    """
    import os

    parse_result = parse_result or {}
    probe = probe or {}

    # ComicInfo.xml first, filename as the fallback. This is the opposite of
    # what the importer does, and deliberately so: the files that end up here
    # are the ones whose *names* defeated the parser. A scanner-named file
    # like "scan_0001.cbz" parses to series "scan", issue 1 - confidently
    # wrong - while its embedded tag says Paper Girls #3. The tag was written
    # by a tagger; the name was written by whatever dumped the file.
    series = probe.get('comicinfo_series') or parse_result.get('series_name')
    # Cleaned at store time so the list shows the best reading of the name and
    # the ComicVine query starts from the same thing the user sees.
    series = clean_series_query(series) or None
    issue = probe.get('comicinfo_issue') or parse_result.get('issue_number')
    year = probe.get('comicinfo_year') or parse_result.get('issue_year')

    # Tag first, then the filename: <Count> was written deliberately, "of 12"
    # is inferred from text.
    total_issues = _as_int(probe.get('comicinfo_count'))
    if total_issues is None:
        total_issues = parse_issue_total(os.path.basename(path))

    try:
        size = os.path.getsize(path)
    except OSError:
        size = None

    return {
        'OrphanID': orphanid,
        'FilePath': path,
        'FileName': os.path.basename(path),
        'FileSize': size,
        'PageCount': probe.get('page_count'),
        'ParsedSeries': series,
        'ParsedIssue': issue,
        'ParsedYear': year,
        'ParsedVolume': parse_result.get('series_volume'),
        'BookType': parse_result.get('booktype'),
        'TotalIssues': total_issues,
        'HasComicInfo': 1 if probe.get('has_comicinfo') else 0,
        'ComicInfoSeries': probe.get('comicinfo_series'),
        'Status': 'new',
        'ComicID': None,
        'IssueID': None,
    }


def to_parsed(record):
    """Shape an orphan record for score_candidate().

    Drops the ComicInfo signal when the series came from ComicInfo, so one
    piece of evidence is not counted twice.
    """
    # Cleaned here too, so scoring compares the same title the search used.
    # Without this, a row recorded before clean_series_query existed searches
    # for "Batman" but is scored against "Batman (1940) Volume01 ", and the
    # real Batman (1940) loses to any series with a longer name.
    series = clean_series_query(record.get('ParsedSeries')) or None
    comicinfo_series = record.get('ComicInfoSeries')
    if comicinfo_series and comicinfo_series == series:
        comicinfo_series = None

    return {
        'series': series,
        'issue': record.get('ParsedIssue'),
        'year': record.get('ParsedYear'),
        'booktype': record.get('BookType'),
        'page_count': record.get('PageCount'),
        'comicinfo_series': comicinfo_series,
        'issue_total': record.get('TotalIssues'),
    }


def scan_directory(root, known_paths=None):
    """Walk a directory and describe every comic file found.

    Read-only. known_paths lets a rescan skip files already recorded, which
    matters because probing means decompressing.
    """
    import os

    known = set(known_paths or [])
    found = []

    if not root or not os.path.isdir(root):
        _debug('[ORPHANS] Not a directory: %s' % root)
        return found

    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in sorted(filenames):
            if not filename.lower().endswith(COMIC_EXTENSIONS):
                continue
            if filename.startswith('.'):
                continue

            path = os.path.join(dirpath, filename)
            if path in known:
                continue

            found.append(describe(path, parse_result=parse_filename(path),
                                  probe=probe_archive(path)))
    return found


def parse_filename(path):
    """Run Mylar's filename parser over a single file.

    Returns {} when the name cannot be parsed, which here is expected rather
    than a failure.
    """
    import os

    try:
        from mylar import filechecker
        result = filechecker.FileChecker(
            dir=os.path.dirname(path), file=os.path.basename(path)
        ).listFiles()
        if result and result.get('parse_status') in ('success', 'match', 'alt_match'):
            return result
        return result or {}
    except Exception as e:
        _debug('[ORPHANS] Could not parse %s: %s' % (path, e))
        return {}


def as_search_issue(value):
    """Whole issue number for findComic's length filter, or None.

    findComic only uses digits, and drops series too short to contain the
    number - so a decimal or lettered issue is passed as None rather than
    rounded into something that would over-filter.
    """
    number = _as_issue_number(value)
    if number is None:
        return None
    if number != int(number):
        return None
    return str(int(number))


# --- filing -------------------------------------------------------------
# Filing never moves: the original stays in the orphans directory so a wrong
# identification costs nothing to undo. Where the destination shares a
# filesystem a hardlink is used, which puts a second name on the same bytes -
# no extra space, and deleting either name leaves the other intact.

import os
import shutil


class FilingRefused(Exception):
    """Raised when filing would do something destructive or ambiguous."""


def choose_placement(source, destination_dir, write_metadata=False):
    """'hardlink' when the two live on one filesystem, otherwise 'copy'.

    Writing metadata forces a copy: a hardlink is a second name for one set of
    bytes, so tagging through it would rewrite the original as well.
    """
    if write_metadata:
        return 'copy'
    try:
        return ('hardlink'
                if os.stat(source).st_dev == os.stat(destination_dir).st_dev
                else 'copy')
    except OSError:
        # cannot tell (missing path, permissions) - copy is always allowed
        return 'copy'


def duplicate_bytes(placement, size):
    """How much disk filing will actually consume. A hardlink costs nothing."""
    if placement == 'hardlink':
        return 0
    return size


def place_file(source, destination, method):
    """Put the file at destination. Returns the method actually used.

    Refuses rather than overwriting - the file in the way would be a comic the
    user already has.
    """
    if os.path.exists(destination):
        raise FilingRefused('Something is already at %s' % destination)

    parent = os.path.dirname(destination)
    if parent and not os.path.isdir(parent):
        raise FilingRefused('Destination folder does not exist: %s' % parent)

    if method == 'hardlink':
        try:
            os.link(source, destination)
            return 'hardlink'
        except OSError as e:
            # some filesystems refuse links even within one device
            _debug('[ORPHANS] Hardlink failed (%s), copying instead' % e)

    shutil.copy2(source, destination)
    return 'copy'


def match_issue(issues, wanted):
    """Find the issue row matching an issue number, or None.

    Compared numerically, so '0153', '153' and '153.0' all match issue 153.
    """
    number = _as_issue_number(wanted)
    if number is None:
        return None
    for row in issues or []:
        if _as_issue_number(row.get('Issue_Number')) == number:
            return row
    return None


def filing_blockers(record, comic, issue):
    """Everything standing between this orphan and being filed.

    Returned rather than raised so the preview can show them all at once, each
    with a code the UI can act on.
    """
    blockers = []

    if comic is None:
        blockers.append({
            'code': 'series_not_watched',
            'message': 'That series is not on your watchlist yet, so there is'
                       ' nowhere to file this. Add it and try again.',
        })
    elif not comic.get('ComicLocation'):
        blockers.append({
            'code': 'no_series_folder',
            'message': 'That series has no folder recorded. Refresh the series'
                       ' in Mylar first.',
        })

    if comic is not None and issue is None:
        blockers.append({
            'code': 'issue_not_found',
            'message': 'No issue with that number exists in the series. Check'
                       ' the issue number, or refresh the series.',
        })

    source = (record or {}).get('FilePath')
    if not source or not os.path.isfile(source):
        blockers.append({
            'code': 'source_missing',
            'message': 'The file is no longer at %s' % source,
        })

    return blockers


def build_plan(record, comic, issue, destination_name, placement):
    """Exactly what filing would do, for the user to confirm before it does."""
    destination_dir = comic.get('ComicLocation')
    return {
        'orphanid': record.get('OrphanID'),
        'source': record.get('FilePath'),
        'source_name': record.get('FileName'),
        'destination_dir': destination_dir,
        'destination_name': destination_name,
        'destination': os.path.join(destination_dir, destination_name),
        'placement': placement,
        'series': comic.get('ComicName'),
        'series_year': comic.get('ComicYear'),
        'comicid': comic.get('ComicID'),
        'issue': issue.get('Issue_Number'),
        'issueid': issue.get('IssueID'),
        # stated explicitly because it is the reassurance that matters here
        'original_kept': True,
    }


# Book types that are not a single issue. Filing one of these against an issue
# number puts a whole collection under one issue.
NOT_SINGLE_ISSUE = ('tpb', 'hc', 'gn', 'one-shot', 'pack')


def issue_source(chosen_issueid, parsed_issue):
    """Where the issue being filed came from.

    'filename' is worth flagging: these files are here because their names
    defeated the parser, so a number taken from one is a guess.
    """
    if chosen_issueid:
        return 'chosen'
    if parsed_issue:
        return 'filename'
    return 'none'


def filing_warnings(record, issue, source):
    """Things worth saying out loud before filing, none of them blocking."""
    warnings = []

    if source == 'filename':
        warnings.append({
            'code': 'issue_guessed',
            'message': 'Issue %s was read off the filename "%s", which is the'
                       ' part that could not be trusted in the first place.'
                       ' Check it before filing.'
                       % ((issue or {}).get('Issue_Number'), record.get('FileName')),
        })

    booktype = (record.get('BookType') or '').lower()
    if any(kind in booktype for kind in NOT_SINGLE_ISSUE):
        warnings.append({
            'code': 'not_a_single_issue',
            'message': 'This file looks like a %s rather than a single issue,'
                       ' so filing it against one issue number is probably'
                       ' wrong.' % record.get('BookType'),
        })

    return warnings


def search_issues(issues, query):
    """Issues whose number or name matches. Empty query returns everything.

    Name matching matters because a story is usually remembered by title
    rather than by issue number.
    """
    rows = list(issues or [])
    text = (query or '').strip().lower()
    if not text:
        return rows

    matched = []
    for row in rows:
        number = str(row.get('Issue_Number') or '').lower()
        name = str(row.get('IssueName') or '').lower()
        if text == number or (name and text in name):
            matched.append(row)
    return matched


def tagging_possible(tag_cr, tag_cbl):
    """Whether there is any tag format to write.

    ENABLE_META is not consulted: it is the default for automatic tagging, not
    a statement about whether tagging can work. manual_metatag ignores it too.
    """
    return bool(tag_cr or tag_cbl)


def should_write_metadata(requested, tagging_possible):
    """Resolve a per-file metadata request."""
    if not tagging_possible:
        return False
    return str(requested) in ('1', 'true', 'True')


def history_row(record, comic, issue, destination, when):
    """A history entry for a file filed out of the orphans directory.

    Status is 'Post-Processed' so "Clear Processed" still reaches these rows;
    clearhistory deletes by exact status. Provider is what marks the origin.
    """
    return {
        'IssueID': issue.get('IssueID'),
        'ComicID': comic.get('ComicID'),
        'ComicName': comic.get('ComicName'),
        'Issue_Number': issue.get('Issue_Number'),
        'Size': record.get('FileSize'),
        'DateAdded': when,
        'Status': 'Post-Processed',
        'Provider': 'Orphan',
        'FolderName': os.path.dirname(destination),
    }
