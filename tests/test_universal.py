import json
import os
from pathlib import Path
import tempfile
import unittest
from folderflow.engine import Engine, NoSpace


class UniversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.src = self.base/'source'
        self.dst = self.base/'destination'
        self.src.mkdir()
        self.cfg = {'shares': {'job': {'source':str(self.src), 'target':str(self.dst),
            'layout':'direct', 'reserve_gib':0, 'mode':'backup', 'keep_versions':True}}}
        self.engine = Engine(self.base/'app/data',self.cfg)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def write(self, rel, data=b'important'):
        path = self.src/rel
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        return path

    def backup(self):
        self.engine.scan()
        self.engine.backup()

    def test_direct_copy_and_existing_target_contents_preserved(self):
        self.write('docs/中文.txt')
        self.dst.mkdir()
        (self.dst/'unrelated.txt').write_bytes(b'keep me')
        self.backup()
        self.assertEqual((self.dst/'docs/中文.txt').read_bytes(),b'important')
        self.assertEqual((self.dst/'unrelated.txt').read_bytes(),b'keep me')
        self.assertFalse((self.dst/'files').exists())
        self.assertTrue((self.dst/'.folderflow/job/task.json').exists())
        self.assertTrue(self.engine.audit())

    def test_zero_reserve_for_small_target_is_respected(self):
        self.write('a')
        self.engine.scan()
        group = next(iter(self.engine.capacity()['volumes'].values()))
        self.assertEqual(group['reserve'],0)
        self.engine.backup()

    def test_filter_include_and_directory_exclude(self):
        self.write('keep.txt')
        self.write('nested/keep.txt')
        self.write('secret.bin')
        self.write('cache/a.txt')
        self.write('nested/a.tmp')
        self.cfg['shares']['job']['include'] = ['*.txt']
        self.cfg['shares']['job']['exclude'] = ['cache/**','*.tmp']
        self.backup()
        self.assertTrue((self.dst/'keep.txt').exists())
        self.assertTrue((self.dst/'nested/keep.txt').exists())
        self.assertFalse((self.dst/'cache').exists())
        self.assertFalse((self.dst/'secret.bin').exists())
        self.assertEqual(self.engine.summary()['job']['n'],2)

    def test_mirror_recycles_extras_and_empty_directories(self):
        self.write('current.txt')
        self.dst.mkdir()
        (self.dst/'old').mkdir()
        (self.dst/'old/a.txt').write_bytes(b'recoverable')
        (self.dst/'empty-extra').mkdir()
        self.cfg['shares']['job']['mode'] = 'mirror'
        self.engine.scan()
        self.assertEqual(self.engine.capacity()['tasks']['job']['extra'],1)
        self.engine.backup()
        self.assertFalse((self.dst/'old').exists())
        self.assertFalse((self.dst/'empty-extra').exists())
        rows = self.engine.db.execute('SELECT rel,path FROM versions').fetchall()
        old = next(row for row in rows if row['rel']==os.path.join('old','a.txt'))
        self.assertEqual(Path(old['path']).read_bytes(),b'recoverable')
        self.assertTrue(self.engine.audit())

    def test_mirror_does_not_recycle_when_source_scan_has_errors(self):
        self.write('a')
        self.dst.mkdir()
        (self.dst/'keep').write_bytes(b'do not lose')
        self.cfg['shares']['job']['mode'] = 'mirror'
        self.engine.scan()
        self.engine.issue('scan','job','unreadable','simulated access denied')
        self.engine.backup()
        self.assertTrue((self.dst/'keep').exists())

    def test_mirror_excluded_target_files_are_preserved(self):
        self.write('a.txt')
        self.dst.mkdir()
        (self.dst/'keep.tmp').write_bytes(b'outside filter')
        self.cfg['shares']['job'].update(mode='mirror',exclude=['*.tmp'])
        self.backup()
        self.assertTrue((self.dst/'keep.tmp').exists())
        self.assertTrue(self.engine.audit())

    def test_mirror_verification_fails_if_extra_file_reappears(self):
        self.write('a')
        self.cfg['shares']['job']['mode']='mirror'
        self.backup()
        (self.dst/'new-extra').write_bytes(b'changed externally')
        self.assertFalse(self.engine.audit())

    def test_update_can_replace_without_keeping_old_version(self):
        source=self.write('a',b'first')
        self.cfg['shares']['job'].update(mode='update',keep_versions=False)
        self.backup()
        source.write_bytes(b'second version')
        self.backup()
        self.assertEqual((self.dst/'a').read_bytes(),b'second version')
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM versions').fetchone()[0],0)

    def test_backup_retains_replaced_target_content(self):
        self.write('a',b'new source')
        self.dst.mkdir()
        (self.dst/'a').write_bytes(b'original destination')
        self.backup()
        archived=self.engine.db.execute('SELECT path FROM versions').fetchone()[0]
        self.assertEqual(Path(archived).read_bytes(),b'original destination')

    def test_selective_scan_does_not_erase_other_task_inventory(self):
        self.write('a')
        second=self.base/'second-source'
        second.mkdir()
        (second/'b').write_bytes(b'second')
        self.cfg['shares']['second']={'source':str(second),'target':str(self.base/'target2'),
            'layout':'direct','reserve_gib':0}
        self.engine.scan()
        self.assertEqual(self.engine.summary()['second']['n'],1)
        self.engine.close()
        selected={'shares':{'job':self.cfg['shares']['job']}}
        self.engine=Engine(self.base/'app/data',selected)
        self.engine.scan()
        self.engine.backup()
        self.engine.close()
        self.engine=Engine(self.base/'app/data',self.cfg)
        self.assertEqual(self.engine.summary()['second']['n'],1)
        self.assertTrue(self.engine.summary()['second']['scan_complete'])
        self.assertFalse((self.base/'target2').exists())

    def test_inactive_task_error_does_not_block_selected_audit(self):
        self.write('a')
        self.backup()
        self.engine.issue('copy','other-task','x','unrelated error')
        self.assertTrue(self.engine.audit())

    def test_recorded_local_timestamp_accepts_coarse_destination_timestamp(self):
        self.write('a')
        self.backup()
        path=self.dst/'a'
        st=path.stat()
        coarse=(st.st_mtime_ns//2_000_000_000)*2_000_000_000
        os.utime(path,ns=(coarse,coarse))
        self.engine.db.execute('UPDATE files SET local_mtime=?',(coarse,))
        self.engine.db.commit()
        row=self.engine.db.execute('SELECT * FROM files').fetchone()
        self.assertTrue(self.engine.check_local_file(row))
        self.assertTrue(self.engine.audit())

    def test_analysis_is_read_only(self):
        self.write('a')
        self.engine.scan()
        self.engine.capacity()
        self.assertFalse(self.dst.exists())
        self.assertEqual((self.src/'a').read_bytes(),b'important')

    def test_per_task_reserve_blocks_before_target_creation(self):
        self.write('a')
        self.cfg['shares']['job']['reserve_gib']=10**9
        self.engine.scan()
        with self.assertRaises(NoSpace):
            self.engine.backup()
        self.assertFalse(self.dst.exists())

    def test_internal_control_directory_is_excluded(self):
        self.write('.folderflow/other/task.json')
        self.write('real.txt')
        self.backup()
        self.assertEqual(self.engine.summary()['job']['n'],1)

    def test_mirror_can_recover_after_failed_audit(self):
        self.write('a')
        self.cfg['shares']['job']['mode']='mirror'
        self.backup()
        (self.dst/'extra').write_bytes(b'restore later')
        self.assertFalse(self.engine.audit())
        self.backup()
        self.assertFalse((self.dst/'extra').exists())
        self.assertTrue(self.engine.audit())

    def test_rate_limit_is_applied_to_copy(self):
        import time
        self.write('a',b'x'*65536)
        self.cfg['shares']['job']['max_mibps']=0.25
        self.engine.scan()
        started=time.monotonic()
        self.engine.backup()
        self.assertGreaterEqual(time.monotonic()-started,0.2)


if __name__=='__main__':
    unittest.main(verbosity=2)
