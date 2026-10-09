import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from folderflow.engine import Engine, Stopped
from folderflow.inventory import load_page
from folderflow.progress import OverallProgress


class InventoryProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.source=self.base/'source'
        self.target=self.base/'target'
        self.source.mkdir()
        self.state=self.base/'app/data'
        self.cfg={'shares':{'job':{'source':str(self.source),'target':str(self.target),
            'layout':'direct','mode':'backup','reserve_gib':0}}}
        self.events=[]
        self.engine=Engine(self.state,self.cfg,lambda kind,value:self.events.append((kind,value)))

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def write(self,name,data=b'a'):
        path=self.source/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        return path

    def page(self,**options):
        return load_page(self.state/'manifest.sqlite',self.cfg,['job'],**options)

    def planned(self):
        self.engine.scan()
        self.engine.capacity()

    def overall(self):
        return [value for kind,value in self.events if kind=='overall']

    def test_entire_inventory_is_accessible_with_bounded_pages(self):
        for i in range(431):
            self.write(f'file-{i:04}.txt')
        self.planned()
        seen=[]
        for number in (1,2,3):
            page=self.page(page=number)
            self.assertEqual(page['count'],431)
            self.assertEqual(page['pages'],3)
            self.assertLessEqual(len(page['rows']),200)
            seen.extend(row['uid'] for row in page['rows'])
        self.assertEqual(len(set(seen)),431)
        self.assertTrue(all(row['action']=='new' for row in self.page()['rows']))

    def test_pending_done_and_error_filters_match_actual_copy_state(self):
        self.write('good.txt',b'good')
        bad=self.write('changed.txt',b'before')
        self.planned()
        bad.write_bytes(b'changed after scan')
        self.engine.backup()
        self.assertEqual(self.page(state='pending')['count'],0)
        self.assertEqual(self.page(state='done')['count'],1)
        errors=self.page(state='error')
        self.assertEqual(errors['count'],1)
        self.assertEqual(errors['rows'][0]['rel'],'changed.txt')
        self.assertIn('扫描后',errors['rows'][0]['error'])
        self.assertLess(self.overall()[-1]['percent'],100)
        self.assertEqual(self.overall()[-1]['state'],'failed')

    def test_progress_includes_previous_success_and_has_fixed_totals(self):
        self.write('existing',b'x'*300)
        self.engine.scan()
        self.engine.backup()
        self.write('new',b'y'*700)
        self.events.clear()
        self.engine.scan()
        self.engine.backup()
        values=self.overall()
        self.assertEqual(values[0]['completed_files'],1)
        self.assertEqual(values[0]['completed_bytes'],300)
        self.assertEqual(values[0]['percent'],30)
        self.assertTrue(all(v['total_files']==2 and v['total_bytes']==1000 for v in values))
        self.assertEqual(values[-1]['percent'],100)
        self.assertEqual(values[-1]['completed_files'],2)
        self.assertTrue(all(v['percent']<100 for v in values[:-1]))

    def test_zero_length_files_advance_overall_by_count(self):
        for i in range(4):
            self.write(str(i),b'')
        self.engine.scan()
        self.engine.backup()
        values=self.overall()
        self.assertEqual(values[0]['percent'],0)
        self.assertEqual(values[-1]['percent'],100)
        self.assertEqual(values[-1]['completed_files'],4)

    def test_scan_issues_appear_in_failed_list_and_block_hundred_percent(self):
        self.write('readable')
        self.planned()
        self.engine.issue('scan','job','unreadable-folder','access denied')
        self.engine.backup()
        errors=self.page(state='error')
        self.assertEqual(errors['count'],1)
        self.assertEqual(errors['rows'][0]['action'],'unreadable')
        self.assertLess(self.overall()[-1]['percent'],100)

    def test_deleted_destination_is_planned_again_instead_of_counted_complete(self):
        self.write('a')
        self.engine.scan()
        self.engine.backup()
        (self.target/'a').unlink()
        self.planned()
        self.assertEqual(self.page()['count'],1)
        self.assertEqual(self.page()['metrics']['completed_files'],0)

    def test_literal_search_does_not_interpret_percent_as_wildcard(self):
        self.write('100%-done.txt')
        self.write('100-other.txt')
        self.planned()
        result=self.page(search='100%')
        self.assertEqual(result['count'],1)
        self.assertEqual(result['rows'][0]['rel'],'100%-done.txt')

    def test_mirror_recycling_is_in_the_plan_and_completion_count(self):
        self.write('current')
        self.target.mkdir()
        (self.target/'extra').write_bytes(b'old')
        self.cfg['shares']['job']['mode']='mirror'
        self.planned()
        self.assertEqual(self.page(state='recycle')['count'],1)
        self.engine.backup()
        self.assertEqual(self.page(state='recycle')['rows'][0]['state'],'done')
        self.assertEqual(self.overall()[-1]['total_files'],2)
        self.assertEqual(self.overall()[-1]['completed_files'],2)
        self.assertEqual(self.overall()[-1]['percent'],100)

    def test_selected_scope_ignores_other_task_files(self):
        self.write('a')
        other=self.base/'other'
        other.mkdir()
        (other/'other-file').write_bytes(b'other')
        self.cfg['shares']['other']={'source':str(other),'target':str(self.base/'target2'),
            'layout':'direct','reserve_gib':0}
        self.planned()
        self.assertEqual(self.page()['count'],1)
        self.assertEqual(self.page()['metrics']['total_files'],1)

    def test_read_only_inventory_never_creates_or_alters_schema(self):
        self.write('a')
        self.planned()
        before=list(self.engine.db.execute('SELECT name,sql FROM sqlite_master'))
        self.page()
        after=list(self.engine.db.execute('SELECT name,sql FROM sqlite_master'))
        self.assertEqual([tuple(row) for row in before],[tuple(row) for row in after])
        missing=self.base/'missing.sqlite'
        self.assertEqual(load_page(missing,self.cfg,['job'])['rows'],[])
        self.assertFalse(missing.exists())

    def test_legacy_schema_is_readable_and_upgrade_preserves_completed_files(self):
        self.write('a',b'preserved')
        self.engine.scan()
        self.engine.backup()
        old=self.engine.db.execute('SELECT id,sha256 FROM files').fetchone()
        self.engine.close()
        connection=sqlite3.connect(self.state/'manifest.sqlite')
        connection.executescript('ALTER TABLE files DROP COLUMN plan_action; ALTER TABLE files DROP COLUMN audit_seen; DROP TABLE mirror_plan;')
        connection.close()
        self.assertEqual(self.page(state='done')['count'],1)
        self.engine=Engine(self.state,self.cfg)
        new=self.engine.db.execute('SELECT id,sha256 FROM files').fetchone()
        self.assertEqual(tuple(old),tuple(new))
        self.assertEqual((self.target/'a').read_bytes(),b'preserved')

    def test_audit_progress_starts_from_zero_and_queue_tracks_this_audit(self):
        self.write('a',b'content')
        self.engine.scan()
        self.engine.backup()
        self.events.clear()
        self.assertTrue(self.engine.audit())
        values=self.overall()
        self.assertEqual(values[0]['completed_files'],0)
        self.assertEqual(values[-1]['percent'],100)
        self.assertEqual(self.page(mode='audit',state='done')['count'],1)

    def test_copy_and_hashing_never_mean_published_success_early(self):
        tracker=OverallProgress(1,300)
        tracker.select(1,300)
        tracker.update('复制',300,300)
        self.assertAlmostEqual(tracker.snapshot()['percent'],100/3)
        tracker.update('SHA-256 校验来源',300,300)
        self.assertAlmostEqual(tracker.snapshot()['percent'],200/3)
        tracker.update('SHA-256 校验本地',300,300)
        self.assertLess(tracker.snapshot()['percent'],100)
        self.assertEqual(tracker.snapshot()['completed_files'],0)
        tracker.complete(300)
        self.assertLess(tracker.snapshot()['percent'],100)
        tracker.finish('done')
        self.assertEqual(tracker.snapshot()['percent'],100)

    def test_pause_clears_unverified_inflight_credit(self):
        self.write('large',b'x'*(4*1024**2+10))
        def stop(kind,value):
            self.events.append((kind,value))
            if kind=='file' and value['phase']=='复制':
                self.engine.stop.set()
        self.engine.emit=stop
        self.engine.scan()
        with self.assertRaises(Stopped):
            self.engine.backup()
        last=self.overall()[-1]
        self.assertEqual(last['state'],'paused')
        self.assertEqual(last['completed_files'],0)
        self.assertLess(last['percent'],100)


if __name__=='__main__':
    unittest.main(verbosity=2)
