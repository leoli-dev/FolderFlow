import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from folderflow.engine import BLOCK, Engine, NoSpace, Stopped
from folderflow.progress import OverallProgress


class ConcurrentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.src = self.base/'source'
        self.src.mkdir()
        self.target = self.base/'target'
        self.cfg = {'shares':{'job':{'source':str(self.src),'target':str(self.target),
            'layout':'direct','reserve_gib':0,'scan_workers':4,'copy_workers':4}}}
        self.events = []
        self.engine = Engine(self.base/'app/data',self.cfg,
            lambda kind,value:self.events.append((kind,value)))

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def write(self,name,data=b'content'):
        path = self.src/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        return path

    def test_parallel_scan_is_complete_and_source_read_only(self):
        active,peak = 0,0
        lock = threading.Lock()
        original = os.scandir
        def slow(path):
            nonlocal active,peak
            with lock:
                active += 1
                peak = max(peak,active)
            time.sleep(.01)
            try:
                return original(path)
            finally:
                with lock:
                    active -= 1
        for i in range(24):
            self.write(f'folder-{i}/a')
        (self.src/'empty').mkdir()
        with patch('folderflow.concurrency.os.scandir',slow):
            result = self.engine.scan()
        self.assertGreater(peak,1)
        self.assertLessEqual(peak,4)
        self.assertEqual(result['job']['n'],24)
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM dirs').fetchone()[0],26)
        self.assertFalse(self.target.exists())
        self.assertEqual(len(list(self.src.rglob('a'))),24)

    def test_worker_exception_cannot_mark_scan_complete(self):
        self.write('a')
        with patch('folderflow.concurrency.os.scandir',side_effect=RuntimeError('unexpected')):
            with self.assertRaisesRegex(RuntimeError,'unexpected'):
                self.engine.scan()
        self.assertEqual(self.engine.meta('task:job:scan_complete'),'0')

    def test_pause_scan_drains_bounded_queue_without_marking_complete(self):
        for i in range(1050):
            self.write(f'a-{i}')
        def pause(kind,value):
            if kind == 'scan':
                self.engine.stop.set()
        self.engine.emit = pause
        with self.assertRaises(Stopped):
            self.engine.scan()
        self.assertEqual(self.engine.meta('task:job:scan_complete'),'0')

    def test_transfer_overlaps_and_verified_hashes_are_committed_by_owner(self):
        owner = threading.get_ident()
        callbacks = []
        self.engine.emit = lambda kind,value:callbacks.append(threading.get_ident())
        for i in range(16):
            self.write(f'a-{i}',os.urandom(16000+i))
        self.engine.scan()
        original = Engine.transfer_file
        active,peak = 0,0
        lock = threading.Lock()
        def slow(worker,row):
            nonlocal active,peak
            with lock:
                active += 1
                peak = max(peak,active)
            try:
                time.sleep(.03)
                return original(worker,row)
            finally:
                with lock:
                    active -= 1
        with patch.object(Engine,'transfer_file',slow):
            self.engine.backup()
        self.assertGreater(peak,1)
        self.assertLessEqual(peak,4)
        self.assertTrue(all(t==owner for t in callbacks))
        for row in self.engine.db.execute('SELECT * FROM files'):
            data = (self.src/row['rel']).read_bytes()
            self.assertEqual((self.target/row['rel']).read_bytes(),data)
            self.assertEqual(row['status'],'verified')
            self.assertEqual(row['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(self.engine.overall.snapshot()['percent'],100)
        self.assertTrue(self.engine.audit())

    def test_pause_parallel_transfer_resumes_all_unpublished_partials(self):
        data = os.urandom(BLOCK*3+7)
        for i in range(6):
            self.write(f'a-{i}',data)
        self.engine.scan()
        def pause(kind,value):
            if kind=='file' and value['phase']=='复制':
                self.engine.stop.set()
        self.engine.emit = pause
        with self.assertRaises(Stopped):
            self.engine.backup()
        self.assertLess(self.engine.overall.snapshot()['percent'],100)
        self.assertTrue(list((self.target/'.folderflow/job/parts').glob('*.part')))
        self.engine.close()
        self.engine = Engine(self.base/'app/data',self.cfg)
        self.engine.scan()
        self.engine.backup()
        self.assertEqual(self.engine.summary()['job']['v'],6)
        for i in range(6):
            self.assertEqual((self.target/f'a-{i}').read_bytes(),data)

    def test_corrupt_readback_is_not_published_and_other_files_finish(self):
        self.write('bad',b'abcd')
        self.write('good',b'good')
        self.engine.scan()
        row = self.engine.db.execute("SELECT id FROM files WHERE rel='bad'").fetchone()
        original = Engine.digest
        def corrupt(worker,path,*args,**kwargs):
            if Path(path).name == f'{row[0]}.part':
                with open(path,'r+b') as f:
                    f.write(b'xxxx')
            return original(worker,path,*args,**kwargs)
        with patch.object(Engine,'digest',corrupt):
            self.engine.backup()
        self.assertFalse((self.target/'bad').exists())
        self.assertEqual((self.target/'good').read_bytes(),b'good')
        self.assertEqual(self.engine.summary()['job']['v'],1)
        self.assertEqual(self.engine.overall.snapshot()['state'],'failed')

    def test_space_loss_stops_workers_without_publishing_unverified_files(self):
        for i in range(4):
            self.write(f'a-{i}',b'a'*(BLOCK*2))
        self.engine.scan()
        original = self.engine.ensure_space
        def limited(target,required):
            if required == BLOCK+4096:
                raise NoSpace('space consumed externally')
            original(target,required)
        self.engine.ensure_space = limited
        with self.assertRaises(NoSpace):
            self.engine.backup()
        self.assertEqual(self.engine.summary()['job']['v'],0)
        self.assertFalse(any((self.target/f'a-{i}').exists() for i in range(4)))

    def test_parallel_transient_disconnect_retries_file(self):
        self.write('a')
        self.engine.scan()
        self.cfg['shares']['job']['retry_seconds'] = 0
        original = Engine.transfer_file
        attempts = []
        def flaky(worker,row):
            attempts.append(row['id'])
            if len(attempts)==1:
                e=OSError('disconnected')
                e.winerror=64
                raise e
            return original(worker,row)
        with patch.object(Engine,'transfer_file',flaky):
            self.engine.backup()
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.engine.summary()['job']['v'],1)

    def test_multiple_inflight_progress_does_not_double_count_completion(self):
        tracker = OverallProgress(2,600)
        tracker.select(1,300)
        tracker.update('复制',300,300,1)
        tracker.select(2,300)
        tracker.update('复制',300,300,2)
        self.assertAlmostEqual(tracker.snapshot()['percent'],100/3)
        tracker.complete(300,1)
        self.assertEqual(tracker.snapshot()['active_files'],1)
        self.assertEqual(set(tracker.snapshot()['active_items']),{2})
        self.assertAlmostEqual(tracker.snapshot()['percent'],200/3)
        tracker.finish('paused')
        self.assertEqual(tracker.snapshot()['percent'],50)

    def test_persistent_disconnect_cancels_bounded_remaining_work(self):
        for i in range(40):
            self.write(f'a-{i}')
        self.engine.scan()
        self.cfg['shares']['job'].update(retry_limit=1,retry_seconds=0)
        attempted = set()
        def offline(worker,row):
            attempted.add(row['id'])
            error = OSError('network unavailable')
            error.winerror = 64
            raise error
        with patch.object(Engine,'transfer_file',offline):
            with self.assertRaises(OSError):
                self.engine.backup()
        self.assertLessEqual(len(attempted),4)
        self.assertEqual(self.engine.summary()['job']['v'],0)

    def test_rate_cap_uses_one_worker_for_the_whole_task(self):
        self.write('a',b'a'*1024)
        self.write('b',b'b'*1024)
        self.cfg['shares']['job']['max_mibps'] = .01
        self.engine.scan()
        original = Engine.transfer_file
        owner = threading.get_ident()
        threads = []
        def observed(worker,row):
            threads.append(threading.get_ident())
            return original(worker,row)
        begin = time.monotonic()
        with patch.object(Engine,'transfer_file',observed):
            self.engine.backup()
        self.assertEqual(set(threads),{owner})
        self.assertGreater(time.monotonic()-begin,.15)

    def test_parallel_mirror_preserves_versions_and_recycles_after_success(self):
        self.write('a',b'old source')
        self.write('b')
        self.engine.scan()
        self.engine.backup()
        self.write('a',b'new source content')
        (self.target/'extra').write_bytes(b'target only')
        self.cfg['shares']['job']['mode'] = 'mirror'
        self.engine.scan()
        self.engine.backup()
        versions = [Path(r[0]).read_bytes() for r in self.engine.db.execute('SELECT path FROM versions')]
        self.assertIn(b'old source',versions)
        self.assertIn(b'target only',versions)
        self.assertFalse((self.target/'extra').exists())
        self.assertEqual((self.target/'a').read_bytes(),b'new source content')
        self.assertTrue(self.engine.audit())

    def test_invalid_worker_counts_are_rejected_before_io(self):
        for option in ('scan_workers','copy_workers'):
            for value in (0,-1,1.5,True,float('nan'),100):
                self.cfg['shares']['job'][option] = value
                with self.assertRaises(ValueError):
                    self.engine.scan()
            self.cfg['shares']['job'][option] = 4
        self.assertFalse(self.target.exists())


if __name__=='__main__':
    unittest.main()
