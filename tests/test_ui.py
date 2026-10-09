import ctypes
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from folderflow import app


class TaskUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.src=self.base/'source'
        self.src.mkdir()
        (self.src/'a.txt').write_bytes(b'backup this file')
        self.target=self.base/'target'
        app.BASE=self.base/'app'
        app.STATE=app.BASE/'data'
        app.BASE.mkdir()
        self.root=tk.Tk()
        self.root.withdraw()
        self.view=app.App(self.root)

    def tearDown(self):
        if self.view.busy:
            self.view.pause()
            self.wait_idle()
        self.root.destroy()
        self.temp.cleanup()

    def payload(self,target=None):
        return {'source':str(self.src),'target':str(target or self.target),'label':'My backup',
            'mode':'backup','layout':'direct','reserve_gib':0,'enabled':True,
            'keep_versions':True,'include':[],'exclude':[]}

    def wait_idle(self):
        deadline=time.monotonic()+20
        while self.view.busy and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.02)
        self.root.update()
        self.assertFalse(self.view.busy)

    def test_fresh_install_has_no_personal_nas_defaults_and_no_autorun(self):
        self.assertEqual(self.view.config['shares'],{})
        self.assertFalse(self.view.busy)
        self.assertEqual(self.view.run_ids(),[])

    def test_any_local_target_is_allowed_without_drive_letter_restriction(self):
        key=self.view.save_task(self.payload())
        self.assertEqual(self.view.config['shares'][key]['target'],str(self.target))
        self.assertFalse(self.target.exists())

    def test_same_source_can_have_multiple_independent_backup_tasks(self):
        a=self.view.save_task(self.payload())
        b=self.view.save_task(self.payload(self.base/'second-target'))
        self.assertNotEqual(a,b)
        self.assertEqual(len(self.view.config['shares']),2)

    def test_selected_task_runs_alone_then_complete_audit(self):
        a=self.view.save_task(self.payload())
        b=self.view.save_task(self.payload(self.base/'second-target'))
        self.view.rebuild_rows(a)
        self.view.scope.set('选中任务')
        self.view.run('scan')
        self.wait_idle()
        self.assertEqual(self.view.latest[a]['n'],1)
        self.assertFalse(self.target.exists())
        self.view.run('backup')
        self.wait_idle()
        self.assertEqual((self.target/'a.txt').read_bytes(),b'backup this file')
        self.assertFalse((self.base/'second-target').exists())
        self.view.run('audit')
        self.wait_idle()
        self.assertTrue(self.view.latest[a]['audit_ok'])

    def test_disabled_tasks_are_excluded_from_both_execution_scopes(self):
        a=self.view.save_task(self.payload())
        self.view.rebuild_rows(a)
        self.view.toggle_tasks()
        self.assertEqual(self.view.run_ids(),[])
        self.view.scope.set('所有启用任务')
        self.assertEqual(self.view.run_ids(),[])

    def test_source_change_creates_new_manifest_identity(self):
        a=self.view.save_task(self.payload())
        other=self.base/'new-source'
        other.mkdir()
        value=self.payload()
        value['source']=str(other)
        b=self.view.save_task(value,editing=a)
        self.assertNotEqual(a,b)
        self.assertNotIn(a,self.view.config['shares'])

    def test_nonfinite_limits_are_rejected(self):
        entry=self.payload()
        entry['reserve_gib']=float('nan')
        with self.assertRaises(ValueError):
            self.view.save_task(entry)

    def test_legacy_configuration_migrates_without_changing_paths(self):
        old={'server':'private-nas','shares':{'legacy':{'source':str(self.src),
            'target':str(self.target)}}}
        app.save_json(app.STATE/'config.json',old)
        migrated=app.load_config()
        self.assertNotIn('server',migrated)
        self.assertEqual(migrated['shares']['legacy']['target'],str(self.target))
        self.assertEqual(migrated['shares']['legacy']['layout'],'managed')
        self.assertEqual(migrated['shares']['legacy']['mode'],'backup')


if __name__=='__main__':
    unittest.main(verbosity=2)
