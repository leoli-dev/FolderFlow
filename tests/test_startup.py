import ctypes
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from folderflow import app
from folderflow.engine import Engine
from folderflow.inventory import load_summary


class StartupTests(unittest.TestCase):
    def setUp(self):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        app.BASE=self.base/'Application'
        app.STATE=app.BASE/'data'
        app.BASE.mkdir()
        app.save_json(app.STATE/'config.json',{'shares':{},'language':'en','version':2})
        self.root=tk.Tk()
        self.root.withdraw()
        self.view=None

    def tearDown(self):
        self.root.destroy()
        self.temp.cleanup()

    def test_slow_summary_does_not_block_first_frame_or_initialize_engine(self):
        started,release,finished=threading.Event(),threading.Event(),threading.Event()
        owner=threading.get_ident()
        workers=[]
        def slow(database,config,cancelled=None):
            workers.append(threading.get_ident())
            started.set()
            release.wait(5)
            finished.set()
            return {}
        try:
            with patch.object(app,'load_summary',slow), \
                 patch.object(app,'Engine',side_effect=AssertionError('Startup must not initialize Engine')):
                self.view=app.App(self.root)
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.is_set(),'Constructor waited for the slow history read')
                frame=threading.Event()
                self.root.after(0,frame.set)
                self.root.update()
                self.assertTrue(frame.is_set())
                self.assertFalse(finished.is_set())
                self.assertTrue(self.view.history_loading)
                self.assertFalse((app.STATE/'manifest.sqlite').exists())
                self.assertTrue(all(worker!=owner for worker in workers))
                release.set()
                self.view.summary_thread.join(2)
                deadline=time.monotonic()+3
                while self.view.history_loading and time.monotonic()<deadline:
                    self.root.update()
                    time.sleep(.02)
                self.assertFalse(self.view.history_loading)
        finally:
            release.set()

    def test_stale_summary_cannot_replace_current_task_statistics(self):
        self.view=app.App(self.root)
        self.view.summary_thread.join(2)
        self.view.summary_generation+=1
        self.view.events.put(('saved_summary',(self.view.summary_generation-1,{'stale':{'n':999}})))
        self.view.poll()
        self.assertNotIn('stale',self.view.latest)

    def test_read_only_summary_matches_engine_and_does_not_change_database(self):
        source=self.base/'Source'
        source.mkdir()
        (source/'a').write_bytes(b'important')
        cfg={'shares':{'demo':{'source':str(source),'target':str(self.base/'Backup')}}}
        engine=Engine(app.STATE,cfg,reserve_fn=lambda _:0)
        engine.scan()
        expected=engine.summary()
        engine.close()
        database=app.STATE/'manifest.sqlite'
        before=hashlib.sha256(database.read_bytes()).digest()
        with patch.object(app,'Engine',side_effect=AssertionError('Read-only summary')):
            self.assertEqual(load_summary(database,cfg),expected)
        self.assertEqual(hashlib.sha256(database.read_bytes()).digest(),before)

    def test_summary_sql_is_cancelled_when_request_becomes_stale(self):
        source=self.base/'Source'
        source.mkdir()
        cfg={'shares':{'demo':{'source':str(source),'target':''}}}
        engine=Engine(app.STATE,cfg)
        engine.meta('task:demo:scan_id','fixture')
        engine.db.executemany('INSERT INTO files(share,rel,size,mtime,seen) VALUES (?,?,?,?,?)',
            [('demo',str(i),i,1,'fixture') for i in range(5000)])
        engine.db.commit()
        engine.close()
        with self.assertRaises(sqlite3.OperationalError):
            load_summary(app.STATE/'manifest.sqlite',cfg,cancelled=lambda:True)


if __name__=='__main__':
    unittest.main()
