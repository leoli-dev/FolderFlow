import ctypes
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from folderflow import app
from folderflow.i18n import Translator,resolve_language,system_language


class LocaleTests(unittest.TestCase):
    def test_auto_uses_windows_ui_language(self):
        with patch('folderflow.i18n.system_language',return_value='en'):
            self.assertEqual(resolve_language('auto'),'en')
        with patch('folderflow.i18n.system_language',return_value='zh_CN'):
            self.assertEqual(resolve_language('auto'),'zh_CN')

    def test_manual_preference_overrides_system(self):
        with patch('folderflow.i18n.system_language',return_value='zh_CN'):
            self.assertEqual(resolve_language('en'),'en')
        with patch('folderflow.i18n.system_language',return_value='en'):
            self.assertEqual(resolve_language('zh_CN'),'zh_CN')

    def test_dynamic_log_keeps_user_names_and_formatted_counts(self):
        tr=Translator('en')
        self.assertEqual(tr.message('扫描 我的文件'), 'Scanning 我的文件')
        self.assertEqual(tr.message('扫描结束：1,234 个文件；错误 0'),'Scan complete: 1,234 files; 0 errors')
        self.assertEqual(tr.message('源文件在扫描后发生变化；重新扫描'),'Source changed after scanning; analyze again')
        self.assertEqual(tr.message('unknown diagnostic'),'unknown diagnostic')


class EnglishUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.source=self.base/'source'
        self.source.mkdir()
        (self.source/'中文文件.txt').write_bytes(b'backup safely')
        app.BASE=self.base/'application'
        app.STATE=app.BASE/'data'
        app.BASE.mkdir()
        app.save_json(app.STATE/'config.json',{'version':2,'language':'en','shares':{}})
        self.root=tk.Tk()
        self.root.withdraw()
        self.view=app.App(self.root)

    def tearDown(self):
        if self.view.busy:
            self.view.pause()
            self.wait_idle()
        self.root.destroy()
        self.temp.cleanup()

    def wait_idle(self):
        until=time.monotonic()+15
        while self.view.busy and time.monotonic()<until:
            self.root.update()
            time.sleep(.02)
        self.root.update()
        self.assertFalse(self.view.busy)

    def wait_inventory(self):
        self.view.request_inventory()
        until=time.monotonic()+10
        while time.monotonic()<until:
            self.root.update()
            time.sleep(.02)
            if self.view.query_applied==self.view.query_generation:
                return
        self.fail('inventory did not render')

    def task(self):
        key=self.view.save_task({'source':str(self.source),'target':str(self.base/'backup'),
            'label':'个人资料','mode':'update','layout':'direct','reserve_gib':0,'enabled':True})
        self.view.rebuild_rows(key)
        return key

    def test_english_controls_and_filter_codes_run_and_verify(self):
        key=self.task()
        self.assertEqual(self.view.language,'en')
        self.assertEqual(str(self.view.start_button['text']),'Run / Resume')
        self.assertEqual(str(self.view.table.heading('source')['text']),'Source')
        self.assertEqual(self.view.table.set(key,'mode'),'One-way sync')
        self.view.scope.set('All enabled tasks')
        self.assertEqual(self.view.run_ids(),[key])
        self.view.run('scan')
        self.wait_idle()
        self.wait_inventory()
        row=self.view.file_table.get_children()[0]
        self.assertEqual(self.view.file_table.set(row,'action'),'New')
        self.assertEqual(self.view.file_table.set(row,'path'),'中文文件.txt')
        self.view.run('backup')
        self.wait_idle()
        self.assertIn('Completed 1 / 1',self.view.overall_text.get())
        self.assertEqual(self.view.config['shares'][key]['mode'],'update')
        self.view.run('audit')
        self.wait_idle()
        self.assertTrue(self.view.latest[key]['audit_ok'])

    def test_live_switch_preserves_task_ids_paths_and_preference(self):
        key=self.task()
        before=json.loads(json.dumps(self.view.config['shares']))
        self.view.language_box.current(2)
        self.view.change_language()
        self.assertEqual(self.view.language,'zh_CN')
        self.assertEqual(str(self.view.start_button['text']),'执行 / 继续')
        self.assertEqual(self.view.config['shares'],before)
        self.assertEqual(self.view.selected(),[key])
        self.view.language_box.current(1)
        self.view.change_language()
        saved=json.loads((app.STATE/'config.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['language'],'en')
        self.assertEqual(saved['shares'],before)

    def test_switch_does_not_change_running_task_settings(self):
        self.task()
        before=json.loads(json.dumps(self.view.config))
        self.view.busy=True
        self.view.language_box.current(2)
        self.view.change_language()
        self.view.busy=False
        self.assertEqual(self.view.config,before)


if __name__=='__main__':
    unittest.main(verbosity=2)
