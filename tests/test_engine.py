import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from folderflow.engine import Engine, Changed, NoSpace, Stopped, BLOCK, longpath, safe_path


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.src = self.base / 'source'
        self.src.mkdir()
        self.target = self.base / 'destination'
        self.cfg = {'shares': {'test': {'source': str(self.src), 'target': str(self.target)}}}
        self.eng = Engine(self.base / 'app' / 'data', self.cfg, reserve_fn=lambda p: 0)

    def tearDown(self):
        self.eng.close()
        self.temp.cleanup()

    def write(self, rel, data):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def row(self):
        return self.eng.db.execute('SELECT * FROM files ORDER BY id').fetchone()

    def test_copy_unicode_empty_directory_and_full_audit(self):
        self.write('中文/重要文件.txt', b'important data\x00' * 1000)
        (self.src / 'empty').mkdir()
        self.eng.scan()
        self.eng.backup()
        self.assertEqual((self.target / 'files/中文/重要文件.txt').read_bytes(), b'important data\x00' * 1000)
        self.assertTrue((self.target / 'files/empty').is_dir())
        self.assertTrue(self.eng.audit())
        summary = json.loads((self.base / 'app/reports/summary.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['final_audit_passed'])

    def test_pause_restart_and_resume_verifies_partial(self):
        data = os.urandom(BLOCK * 3 + 123)
        self.write('large.bin', data)
        self.eng.scan()
        def callback(kind, event):
            if kind == 'file' and event['phase'] == '复制':
                self.eng.stop.set()
        self.eng.emit = callback
        with self.assertRaises(Stopped):
            self.eng.backup()
        self.assertTrue((self.target / '_control/parts/1.part').exists())
        self.eng.close()
        self.eng = Engine(self.base / 'app/data', self.cfg, reserve_fn=lambda p: 0)
        phases = []
        self.eng.emit = lambda kind, event: phases.append(event['phase']) if kind == 'file' else None
        self.eng.scan()
        self.eng.backup()
        self.assertIn('核对续传段', phases)
        self.assertEqual((self.target / 'files/large.bin').read_bytes(), data)

    def test_corrupt_partial_is_restarted(self):
        data = os.urandom(BLOCK + 300)
        self.write('large.bin', data)
        self.eng.scan()
        self.eng.prepare_target('test')
        st = os.stat(self.src / 'large.bin')
        part = self.target / '_control/parts/1.part'
        part.write_bytes(b'bad prefix')
        (self.target / '_control/parts/1.json').write_text(json.dumps({
            'source': longpath(self.src / 'large.bin'), 'fingerprint': [st.st_size, st.st_mtime_ns]}))
        self.eng.backup()
        self.assertEqual((self.target / 'files/large.bin').read_bytes(), data)

    def test_changes_keep_old_version_and_source_deletion_keeps_backup(self):
        p = self.write('data.txt', b'first content')
        self.eng.scan()
        self.eng.backup()
        p.write_bytes(b'new longer content')
        self.eng.scan()
        self.eng.backup()
        archive = self.eng.db.execute('SELECT path FROM versions').fetchone()[0]
        self.assertEqual(Path(archive).read_bytes(), b'first content')
        self.assertEqual((self.target / 'files/data.txt').read_bytes(), b'new longer content')
        p.unlink()
        self.eng.scan()
        self.eng.backup()
        self.assertTrue((self.target / 'files/data.txt').exists())

    def test_space_rejection_before_any_copy(self):
        self.write('data', b'important')
        self.eng.scan()
        self.eng.reserve_fn = lambda p: 10 ** 20
        with self.assertRaises(NoSpace):
            self.eng.backup()
        self.assertFalse(self.target.exists())
        self.assertEqual((self.src / 'data').read_bytes(), b'important')

    def test_corrupted_destination_fails_even_same_size_and_timestamp(self):
        self.write('data', b'abcdef')
        self.eng.scan()
        self.eng.backup()
        dst = self.target / 'files/data'
        st = dst.stat()
        dst.write_bytes(b'ghijkl')
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertFalse(self.eng.audit())
        self.assertEqual(self.eng.meta('audit_ok'), '0')

    def test_source_content_changes_same_metadata_detected_by_audit(self):
        p = self.write('data', b'abcdef')
        self.eng.scan()
        self.eng.backup()
        st = p.stat()
        p.write_bytes(b'ghijkl')
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertFalse(self.eng.audit())

    def test_source_changes_during_copy_never_marked_verified(self):
        p = self.write('data', os.urandom(BLOCK + 200))
        self.eng.scan()
        def mutate(kind, event):
            if kind == 'file' and event['phase'] == '复制':
                p.write_bytes(b'changed')
        self.eng.emit = mutate
        self.eng.backup()
        self.assertEqual(self.row()['status'], 'error')
        self.assertFalse((self.target / 'files/data').exists())

    def test_missing_share_blocks_final_audit(self):
        self.write('data', b'a')
        self.eng.scan()
        self.eng.backup()
        self.cfg['shares']['test']['source'] = str(self.base / 'missing')
        with self.assertRaises(ValueError):
            self.eng.audit()
        self.assertEqual(self.eng.meta('audit_ok'), '0')
        self.assertGreater(self.eng.scan_errors(), 0)

    def test_target_change_creates_new_copy_and_preserves_former(self):
        self.write('data', b'original')
        self.eng.scan()
        self.eng.backup()
        self.cfg['shares']['test']['target'] = str(self.base / 'second')
        self.eng.scan()
        self.assertEqual(self.eng.summary()['test']['v'], 0)
        self.eng.backup()
        self.assertEqual((self.base / 'second/files/data').read_bytes(), b'original')
        self.assertEqual((self.target / 'files/data').read_bytes(), b'original')

    def test_target_guard_and_path_traversal(self):
        self.target.mkdir()
        (self.target / 'unrelated').write_text('do not touch')
        with self.assertRaises(ValueError):
            self.eng.prepare_target('test')
        with self.assertRaises(ValueError):
            safe_path(str(self.target), '../outside')
        self.assertEqual((self.target / 'unrelated').read_text(), 'do not touch')

    def test_capacity_combines_same_volume(self):
        self.write('data', b'a' * 1000)
        another = self.base / 'source2'
        another.mkdir()
        (another / 'data').write_bytes(b'b' * 2000)
        self.cfg['shares']['other'] = {'source': str(another), 'target': str(self.base / 'target2')}
        self.eng.scan()
        cap = self.eng.capacity()
        self.assertEqual(len(cap['volumes']), 1)
        self.assertEqual(len(next(iter(cap['volumes'].values()))['shares']), 2)
        self.assertGreaterEqual(next(iter(cap['volumes'].values()))['needed'], 3000)

    def test_transient_network_fault_retries_then_finishes(self):
        self.write('data', b'reconnect successfully')
        self.eng.scan()
        self.eng.retry_seconds = 0
        original = self.eng.copy_file
        attempts = []
        def flaky(row):
            attempts.append(1)
            if len(attempts) == 1:
                e = OSError('simulated disconnected SMB')
                e.winerror = 64
                raise e
            return original(row)
        self.eng.copy_file = flaky
        self.eng.backup()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.row()['status'], 'verified')

    def test_persistent_disconnect_stops_batch_with_report(self):
        self.write('data', b'keep source')
        self.eng.scan()
        self.eng.retry_seconds = 0
        self.eng.retry_limit = 1
        def offline(row):
            e = OSError('simulated disconnected SMB')
            e.winerror = 64
            raise e
        self.eng.copy_file = offline
        with self.assertRaises(OSError):
            self.eng.backup()
        self.assertEqual(self.row()['status'], 'error')
        self.assertEqual((self.src / 'data').read_bytes(), b'keep source')

    def test_junction_target_rejected(self):
        import subprocess
        real = self.base / 'real'
        real.mkdir()
        link = self.base / 'junction'
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)], check=True,
                           capture_output=True)
        else:
            link.symlink_to(real, target_is_directory=True)
        try:
            with self.assertRaises(ValueError):
                safe_path(str(link / 'child'))
        finally:
            if os.name == 'nt':
                link.rmdir()
            else:
                link.unlink()

    def test_target_inside_source_rejected_before_scan_or_copy(self):
        self.cfg['shares']['test']['target'] = str(self.src / 'backup')
        with self.assertRaises(ValueError):
            self.eng.scan()
        self.assertFalse((self.src / 'backup').exists())

    def test_source_inside_target_rejected(self):
        self.cfg['shares']['test']['target'] = str(self.base)
        with self.assertRaises(ValueError):
            self.eng.validate_configuration()

    def test_source_contains_another_tasks_target_rejected(self):
        other = self.base / 'other'
        other.mkdir()
        self.cfg['shares']['other'] = {'source': str(other), 'target': str(self.src / 'other-backup')}
        with self.assertRaises(ValueError):
            self.eng.validate_configuration()

    def test_application_state_in_source_rejected(self):
        self.cfg['shares']['test']['source'] = str(self.base)
        with self.assertRaises(ValueError):
            self.eng.scan()

    def test_target_directories_overlap_rejected(self):
        other = self.base / 'other'
        other.mkdir()
        self.cfg['shares']['other'] = {'source': str(other), 'target': str(self.target / 'nested')}
        with self.assertRaises(ValueError):
            self.eng.validate_configuration()

    def test_no_sources_cannot_pass_an_empty_audit(self):
        self.cfg['shares'].clear()
        with self.assertRaises(ValueError):
            self.eng.audit()
        self.assertEqual(self.eng.meta('audit_ok'), '0')


if __name__ == '__main__':
    unittest.main(verbosity=2)
