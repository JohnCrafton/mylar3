import errno
import os

import pytest

from mylar import orphan_batches as ob
from mylar import orphan_moves as om
from tests.orphan_fakes import FakeDB


def _now():
    return '2026-09-30 12:00:00'


def _file(path, data=b'comic'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def _plan(db, moves, batch='b1'):
    om.record_plan(db, 'run1', batch, 'c1', {}, moves, _now())
    return batch


def _statuses(db, batch='b1'):
    return [(r['Kind'], r['Status']) for r in db.select(
        'SELECT Kind, Status FROM orphan_moves WHERE BatchID=? ORDER BY Seq', [batch])]


@pytest.mark.integration
def test_record_then_execute_moves_and_parks(tmp_path):
    lib = str(tmp_path)
    a = _file(os.path.join(lib, 'F', 'F 001.cbz'))
    b = _file(os.path.join(lib, 'F', 'F 001 (1).cbz'))
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', a, os.path.join(lib, 'Fables (2002)', 'Fables 001.cbz'), 'i1'),
        ob.Move('park', 'o2', b, ob.park_destination(b, lib), 'i1')])
    assert _statuses(db) == [('move', 'planned'), ('park', 'planned')]

    result = om.execute(db, batch, _now)

    assert result == om.MoveResult(2, 0, False)
    assert os.path.isfile(os.path.join(lib, 'Fables (2002)', 'Fables 001.cbz'))
    assert os.path.isfile(os.path.join(lib, '_duplicates', 'F', 'F 001 (1).cbz'))
    assert not os.path.exists(a) and not os.path.exists(b)
    assert _statuses(db) == [('move', 'done'), ('park', 'done')]


@pytest.mark.integration
def test_execute_refuses_to_overwrite(tmp_path):
    src = _file(str(tmp_path / 'a.cbz'), b'new')
    dst = _file(str(tmp_path / 'S' / 'a.cbz'), b'existing')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, dst, 'i1')])
    assert om.execute(db, batch, _now) == om.MoveResult(0, 1, False)
    assert open(dst, 'rb').read() == b'existing'
    assert os.path.isfile(src)
    assert 'already' in db.select('SELECT Error FROM orphan_moves')[0]['Error']


@pytest.mark.integration
def test_execute_refuses_across_filesystems_instead_of_copying(tmp_path):
    src = _file(str(tmp_path / 'a.cbz'))
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, str(tmp_path / 'S' / 'a.cbz'), 'i1')])

    def cross_device(a, b):
        raise OSError(errno.EXDEV, 'Invalid cross-device link')

    assert om.execute(db, batch, _now, rename=cross_device) == om.MoveResult(0, 1, False)
    assert os.path.isfile(src)
    assert 'filesystem' in db.select('SELECT Error FROM orphan_moves')[0]['Error']


@pytest.mark.integration
def test_execute_treats_an_already_moved_file_as_done(tmp_path):
    # the rename happened, then the process died before the log said so
    dst = _file(str(tmp_path / 'S' / 'a.cbz'))
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', str(tmp_path / 'a.cbz'), dst, 'i1')])
    assert om.execute(db, batch, _now) == om.MoveResult(1, 0, False)
    assert _statuses(db) == [('move', 'done')]


@pytest.mark.integration
def test_execute_fails_a_row_whose_source_vanished_and_carries_on(tmp_path):
    kept = _file(str(tmp_path / 'b.cbz'))
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', str(tmp_path / 'gone.cbz'), str(tmp_path / 'S' / 'gone.cbz'), 'i1'),
        ob.Move('move', 'o2', kept, str(tmp_path / 'S' / 'b.cbz'), 'i2')])
    assert om.execute(db, batch, _now) == om.MoveResult(1, 1, False)
    assert _statuses(db) == [('move', 'failed'), ('move', 'done')]
    assert om.batch_counts(db, batch) == {'failed': 1, 'done': 1}


@pytest.mark.integration
def test_stop_leaves_the_rest_planned_and_execute_resumes(tmp_path):
    a = _file(str(tmp_path / 'a.cbz'))
    b = _file(str(tmp_path / 'b.cbz'))
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, str(tmp_path / 'S' / 'a.cbz'), 'i1'),
                       ob.Move('move', 'o2', b, str(tmp_path / 'S' / 'b.cbz'), 'i2')])
    calls = []

    def stop_after_one():
        calls.append(1)
        return len(calls) > 1

    assert om.execute(db, batch, _now, stop=stop_after_one) == om.MoveResult(1, 0, True)
    assert _statuses(db) == [('move', 'done'), ('move', 'planned')]
    assert om.execute(db, batch, _now) == om.MoveResult(1, 0, False)
    assert _statuses(db) == [('move', 'done'), ('move', 'done')]


@pytest.mark.integration
def test_remove_empty_dirs_keeps_nonempty_dirs_and_the_root(tmp_path):
    lib = str(tmp_path / 'lib')
    a = _file(os.path.join(lib, 'Fables', 'Vol 1', 'a.cbz'))
    b = _file(os.path.join(lib, 'Iron Man', 'b.cbz'))
    _file(os.path.join(lib, 'Iron Man', 'cover.jpg'))
    c = _file(os.path.join(lib, 'c.cbz'))
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', a, os.path.join(lib, 'Fables (2002)', 'a.cbz'), 'i1'),
        ob.Move('move', 'o2', b, os.path.join(lib, 'Iron Man (1968)', 'b.cbz'), 'i2'),
        ob.Move('move', 'o3', c, os.path.join(lib, 'C (2000)', 'c.cbz'), 'i3')])
    om.execute(db, batch, _now)

    removed = om.remove_empty_dirs(db, batch, lib, _now)

    assert sorted(removed) == [os.path.join(lib, 'Fables'), os.path.join(lib, 'Fables', 'Vol 1')]
    assert os.path.isdir(os.path.join(lib, 'Iron Man'))
    assert os.path.isdir(lib)
    assert _statuses(db)[-2:] == [('rmdir', 'done'), ('rmdir', 'done')]


@pytest.mark.integration
def test_revert_puts_everything_back_including_removed_dirs(tmp_path):
    lib = str(tmp_path)
    a = _file(os.path.join(lib, 'F', 'Vol 1', 'a.cbz'), b'A')
    b = _file(os.path.join(lib, 'F', 'Vol 1', 'a (1).cbz'), b'B')
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', a, os.path.join(lib, 'Fables (2002)', 'Fables 001.cbz'), 'i1'),
        ob.Move('park', 'o2', b, ob.park_destination(b, lib), 'i1')])
    om.execute(db, batch, _now)
    om.remove_empty_dirs(db, batch, lib, _now)
    assert not os.path.exists(os.path.join(lib, 'F'))

    result = om.revert(db, batch, _now)

    assert result == om.RevertResult(2, ())
    assert open(a, 'rb').read() == b'A' and open(b, 'rb').read() == b'B'
    assert not os.path.exists(os.path.join(lib, 'Fables (2002)', 'Fables 001.cbz'))
    assert om.batch_counts(db, batch) == {'reverted': 2}


@pytest.mark.integration
def test_revert_skips_a_file_whose_original_path_is_taken(tmp_path):
    a = _file(str(tmp_path / 'a.cbz'), b'A')
    db = FakeDB()
    dst = str(tmp_path / 'S' / 'a.cbz')
    batch = _plan(db, [ob.Move('move', 'o1', a, dst, 'i1')])
    om.execute(db, batch, _now)
    _file(a, b'someone else')

    result = om.revert(db, batch, _now)

    assert result.reverted == 0
    assert result.skipped == ((a, 'original path is occupied'),)
    assert open(a, 'rb').read() == b'someone else'
    assert open(dst, 'rb').read() == b'A'
