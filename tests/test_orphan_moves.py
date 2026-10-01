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
    # Simulate crash: mark the row as 'moving' to indicate rename attempt started
    db.action('UPDATE orphan_moves SET Status=? WHERE BatchID=? AND Seq=?',
              ['moving', batch, 0])
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


@pytest.mark.integration
def test_revert_continues_when_removed_dir_creation_fails(tmp_path):
    # Fix 1: revert() should skip rmdir rows that can't be recreated, not abort
    lib = str(tmp_path)
    a = _file(os.path.join(lib, 'F', 'a.cbz'), b'A')
    b = _file(os.path.join(lib, 'G', 'b.cbz'), b'B')
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', a, os.path.join(lib, 'Fables (2002)', 'a.cbz'), 'i1'),
        ob.Move('move', 'o2', b, os.path.join(lib, 'Grendel (2005)', 'b.cbz'), 'i2')])
    om.execute(db, batch, _now)
    om.remove_empty_dirs(db, batch, lib, _now)

    # Replace one removed directory (F) with a regular file to block its recreation
    _file(os.path.join(lib, 'F'), b'blocking_file')

    result = om.revert(db, batch, _now)

    # Should skip the F rmdir, continue, and restore both files successfully
    # (G can be recreated, and G/b.cbz can be restored)
    assert result.reverted >= 1  # At least one file or dir restored
    # The F rmdir should be in skipped
    skipped_paths = [s[0] for s in result.skipped]
    assert os.path.join(lib, 'F') in skipped_paths
    # G should have been recreated successfully
    assert os.path.isdir(os.path.join(lib, 'G'))
    # b.cbz should be restored
    assert open(b, 'rb').read() == b'B'


@pytest.mark.integration
def test_execute_with_crash_recovery_mark_moving_state(tmp_path):
    # Fix 2a: Test updated to set row to 'moving' before execute
    # Simulates a crash mid-rename: row was marked 'moving', then renamed, then process died
    dst = _file(str(tmp_path / 'S' / 'a.cbz'))
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', str(tmp_path / 'a.cbz'), dst, 'i1')])

    # Simulate crash: mark the row as 'moving' to indicate rename attempt started
    db.action('UPDATE orphan_moves SET Status=? WHERE BatchID=? AND Seq=?',
              ['moving', batch, 0])

    result = om.execute(db, batch, _now)

    # Should treat destination-exists as done (file was already moved before crash)
    assert result == om.MoveResult(1, 0, False)
    assert _statuses(db) == [('move', 'done')]


@pytest.mark.integration
def test_execute_fails_when_row_planned_and_foreign_file_at_destination(tmp_path):
    # Fix 2b: If row is 'planned' (no rename attempt yet) and source is gone but
    # destination has a foreign file, that's a failure, not success
    src = str(tmp_path / 'a.cbz')
    dst = _file(str(tmp_path / 'S' / 'a.cbz'), b'foreign')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, dst, 'i1')])

    # Don't create the source file; it's gone
    # But destination exists with a foreign file

    result = om.execute(db, batch, _now)

    # Should fail because source is gone but this wasn't a 'moving' row (no crash recovery)
    assert result == om.MoveResult(0, 1, False)
    assert _statuses(db) == [('move', 'failed')]
    assert 'no longer' in db.select('SELECT Error FROM orphan_moves')[0]['Error']
    # Foreign file should remain untouched
    assert open(dst, 'rb').read() == b'foreign'


@pytest.mark.integration
def test_remove_empty_dirs_with_root_slash_does_not_hang(tmp_path):
    # Fix 4: library_root='/' should not hang when climbing
    src = str(tmp_path / 'a.cbz')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, str(tmp_path / 'S' / 'a.cbz'), 'i1')])

    # Create source and execute
    _file(src)
    om.execute(db, batch, _now)

    # Record calls to rmdir to verify it's not called on '/'
    rmdir_calls = []
    def stub_rmdir(path):
        rmdir_calls.append(path)

    # Call with library_root='/' - should not hang, and should not call rmdir on '/'
    removed = om.remove_empty_dirs(db, batch, '/', _now, rmdir=stub_rmdir)

    assert '/' not in rmdir_calls
    # Should return successfully without hanging


@pytest.mark.integration
def test_revert_cancels_planned_failed_rows_after_restoring_done_rows(tmp_path):
    # Fix 5: After revert, remaining 'planned'/'failed'/'moving' rows should become 'cancelled'
    # This prevents a later execute() from re-moving files of a batch the user undid
    a = _file(str(tmp_path / 'a.cbz'))
    b = _file(str(tmp_path / 'b.cbz'))
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', a, str(tmp_path / 'S' / 'a.cbz'), 'i1'),
        ob.Move('move', 'o2', b, str(tmp_path / 'S' / 'b.cbz'), 'i2')])

    calls = []
    def stop_after_one():
        calls.append(1)
        return len(calls) > 1

    # Execute only the first move
    om.execute(db, batch, _now, stop=stop_after_one)
    assert _statuses(db) == [('move', 'done'), ('move', 'planned')]

    # Revert the batch
    om.revert(db, batch, _now)

    # After revert, the second row should be 'cancelled', not 'planned'
    second_row_status = db.select('SELECT Status FROM orphan_moves WHERE BatchID=? AND Seq=1', [batch])[0]['Status']
    assert second_row_status == 'cancelled'

    # Execute again should do nothing (the cancelled row is not executed)
    result = om.execute(db, batch, _now)
    assert result == om.MoveResult(0, 0, False)
    assert os.path.isfile(b)  # Second file was never moved


@pytest.mark.integration
def test_revert_puts_back_a_moving_row_whose_rename_happened(tmp_path):
    # the process died after os.rename but before the row was marked done
    a = _file(str(tmp_path / 'F' / 'a.cbz'), b'A')
    b = _file(str(tmp_path / 'F' / 'b.cbz'), b'B')
    db = FakeDB()
    batch = _plan(db, [
        ob.Move('move', 'o1', a, str(tmp_path / 'S' / 'a.cbz'), 'i1'),
        ob.Move('move', 'o2', b, str(tmp_path / 'S' / 'b.cbz'), 'i2')])

    def rename_then_die(source, destination):
        os.rename(source, destination)
        raise KeyboardInterrupt('died before the done mark')

    with pytest.raises(KeyboardInterrupt):
        om.execute(db, batch, _now, rename=rename_then_die)
    assert _statuses(db) == [('move', 'moving'), ('move', 'planned')]

    result = om.revert(db, batch, _now)

    assert result == om.RevertResult(1, ())
    assert open(a, 'rb').read() == b'A'
    assert not os.path.exists(str(tmp_path / 'S' / 'a.cbz'))
    assert _statuses(db) == [('move', 'reverted'), ('move', 'cancelled')]


@pytest.mark.integration
def test_revert_cancels_a_moving_row_whose_rename_never_happened(tmp_path):
    a = _file(str(tmp_path / 'F' / 'a.cbz'), b'A')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, str(tmp_path / 'S' / 'a.cbz'), 'i1')])
    db.action("UPDATE orphan_moves SET Status='moving' WHERE BatchID=?", [batch])

    result = om.revert(db, batch, _now)

    assert result == om.RevertResult(0, ())
    assert open(a, 'rb').read() == b'A'
    assert _statuses(db) == [('move', 'cancelled')]


class _UnrecordingDB(FakeDB):
    """FakeDB whose writes fail the way mylar.db's do: action returns None."""
    failing = False

    def action(self, query, args=None, executemany=False):
        if self.failing:
            return None
        return super(_UnrecordingDB, self).action(query, args, executemany)


@pytest.mark.integration
def test_execute_refuses_to_move_when_the_log_cannot_be_written(tmp_path):
    a = _file(str(tmp_path / 'F' / 'a.cbz'))
    db = _UnrecordingDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, str(tmp_path / 'S' / 'a.cbz'), 'i1')])
    db.failing = True

    with pytest.raises(RuntimeError, match='Could not record'):
        om.execute(db, batch, _now)

    assert os.path.isfile(a)
    assert not os.path.exists(str(tmp_path / 'S' / 'a.cbz'))


@pytest.mark.unit
def test_record_plan_raises_when_the_plan_cannot_be_written(tmp_path):
    db = _UnrecordingDB()
    db.failing = True
    with pytest.raises(RuntimeError, match='Could not record'):
        _plan(db, [ob.Move('move', 'o1', str(tmp_path / 'a.cbz'),
                           str(tmp_path / 'S' / 'a.cbz'), 'i1')])


@pytest.mark.integration
def test_revert_raises_when_a_note_cannot_be_written(tmp_path):
    a = _file(str(tmp_path / 'F' / 'a.cbz'))
    db = _UnrecordingDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, str(tmp_path / 'S' / 'a.cbz'), 'i1')])
    om.execute(db, batch, _now)
    _file(a, b'someone else')          # the original path is taken: revert notes it
    db.failing = True

    with pytest.raises(RuntimeError, match='Could not record'):
        om.revert(db, batch, _now)


@pytest.mark.integration
def test_execute_counts_a_keeper_already_at_its_filed_path_as_done(tmp_path):
    # the orphan already sits where the series would file it
    a = _file(str(tmp_path / 'Fables (2002)' / 'Fables 001 (2002).cbz'), b'A')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, a, 'i1')])

    assert om.execute(db, batch, _now) == om.MoveResult(1, 0, False)
    assert open(a, 'rb').read() == b'A'
    assert _statuses(db) == [('move', 'done')]


@pytest.mark.integration
def test_execute_fails_a_same_path_keeper_that_is_not_on_disk(tmp_path):
    # the same-path shortcut must not mark a missing file as filed
    a = str(tmp_path / 'Fables (2002)' / 'Fables 001 (2002).cbz')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, a, 'i1')])

    assert om.execute(db, batch, _now) == om.MoveResult(0, 1, False)
    assert _statuses(db) == [('move', 'failed')]


@pytest.mark.integration
def test_revert_of_a_keeper_already_at_its_filed_path_leaves_it_and_is_not_skipped(tmp_path):
    a = _file(str(tmp_path / 'Fables (2002)' / 'Fables 001 (2002).cbz'), b'A')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', a, a, 'i1')])
    om.execute(db, batch, _now)

    assert om.revert(db, batch, _now) == om.RevertResult(1, ())
    assert open(a, 'rb').read() == b'A'
    assert _statuses(db) == [('move', 'reverted')]


@pytest.mark.integration
def test_execute_syncs_both_folders_before_marking_a_move_done(tmp_path):
    # a crash after the row says done but before the rename reaches the disk
    # leaves a done row whose file never moved; sync first, then mark
    src = _file(str(tmp_path / 'F' / 'F 001.cbz'))
    dst = str(tmp_path / 'S' / 'S 001.cbz')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, dst, 'i1')])
    synced = []

    def sync(*folders):
        synced.append((sorted(folders), _statuses(db)[0][1]))

    assert om.execute(db, batch, _now, sync=sync) == om.MoveResult(1, 0, False)
    assert synced == [(sorted([os.path.dirname(src), os.path.dirname(dst)]), 'moving')]
    assert _statuses(db) == [('move', 'done')]


@pytest.mark.integration
def test_execute_leaves_a_row_moving_when_sync_fails(tmp_path):
    # the rename may not be durable, so the row stays 'moving' for crash
    # recovery to settle instead of claiming done
    src = _file(str(tmp_path / 'a.cbz'))
    dst = str(tmp_path / 'S' / 'a.cbz')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, dst, 'i1')])

    def sync(*folders):
        raise OSError(errno.EIO, 'I/O error')

    with pytest.raises(OSError):
        om.execute(db, batch, _now, sync=sync)
    assert _statuses(db) == [('move', 'moving')]


@pytest.mark.integration
def test_revert_syncs_both_folders_before_marking_reverted(tmp_path):
    src = _file(str(tmp_path / 'F' / 'a.cbz'))
    dst = str(tmp_path / 'S' / 'a.cbz')
    db = FakeDB()
    batch = _plan(db, [ob.Move('move', 'o1', src, dst, 'i1')])
    om.execute(db, batch, _now)
    synced = []

    def sync(*folders):
        synced.append((sorted(folders), _statuses(db)[0][1]))

    om.revert(db, batch, _now, sync=sync)
    assert synced == [(sorted([os.path.dirname(src), os.path.dirname(dst)]), 'done')]
    assert _statuses(db) == [('move', 'reverted')]
    assert os.path.isfile(src)


@pytest.mark.integration
def test_sync_folders_flushes_real_directories(tmp_path):
    a = tmp_path / 'a'
    a.mkdir()
    om.sync_folders(str(a), str(tmp_path))


# --- deleting parked duplicates -------------------------------------------

def _dupes(tmp_path):
    return str(tmp_path / '_duplicates')


@pytest.mark.integration
def test_deletable_allows_a_parked_copy_with_its_keeper_on_disk(tmp_path):
    park = _file(str(tmp_path / '_duplicates' / 'F' / 'F 001.cbz'))
    keep = _file(str(tmp_path / 'Fables (2002)' / 'Fables 001.cbz'))
    assert om.deletable(park, keep, _dupes(tmp_path)) is None


@pytest.mark.integration
def test_deletable_keeps_a_park_not_at_its_logged_path(tmp_path):
    keep = _file(str(tmp_path / 'S' / 'a.cbz'))
    missing = str(tmp_path / '_duplicates' / 'F' / 'a.cbr')
    assert om.deletable(missing, keep, _dupes(tmp_path)) == om.REASONS['missing']


@pytest.mark.integration
def test_deletable_keeps_a_park_whose_issue_has_no_recorded_file(tmp_path):
    park = _file(str(tmp_path / '_duplicates' / 'a.cbz'))
    assert om.deletable(park, None, _dupes(tmp_path)) == om.REASONS['no_keeper_record']


@pytest.mark.integration
def test_deletable_keeps_a_park_whose_kept_file_is_gone(tmp_path):
    park = _file(str(tmp_path / '_duplicates' / 'a.cbz'))
    gone = str(tmp_path / 'S' / 'a.cbr')
    assert om.deletable(park, gone, _dupes(tmp_path)) == om.REASONS['keeper_missing']


@pytest.mark.integration
def test_deletable_refuses_when_keeper_is_the_parked_file_via_symlink(tmp_path):
    park = _file(str(tmp_path / '_duplicates' / 'a.cbz'))
    os.makedirs(str(tmp_path / 'S'))
    link = str(tmp_path / 'S' / 'a.cbz')
    os.symlink(park, link)
    assert om.deletable(park, link, _dupes(tmp_path)) == om.REASONS['same_file']


@pytest.mark.integration
def test_deletable_refuses_when_keeper_is_the_parked_file_via_hardlink(tmp_path):
    park = _file(str(tmp_path / '_duplicates' / 'a.cbz'))
    os.makedirs(str(tmp_path / 'S'))
    link = str(tmp_path / 'S' / 'a.cbz')
    os.link(park, link)
    assert om.deletable(park, link, _dupes(tmp_path)) == om.REASONS['same_file']


@pytest.mark.integration
def test_deletable_refuses_a_file_outside_duplicates(tmp_path):
    stray = _file(str(tmp_path / 'S' / 'b.cbz'))
    keep = _file(str(tmp_path / 'S' / 'a.cbz'))
    assert om.deletable(stray, keep, _dupes(tmp_path)) == om.REASONS['outside']


@pytest.mark.integration
def test_deletable_refuses_a_path_escaping_duplicates_by_symlink(tmp_path):
    real = _file(str(tmp_path / 'S' / 'b.cbz'))
    keep = _file(str(tmp_path / 'S' / 'a.cbz'))
    os.makedirs(str(tmp_path / '_duplicates'))
    os.symlink(str(tmp_path / 'S'), str(tmp_path / '_duplicates' / 'S'))
    escaped = str(tmp_path / '_duplicates' / 'S' / 'b.cbz')
    assert om.deletable(escaped, keep, _dupes(tmp_path)) == om.REASONS['outside']
