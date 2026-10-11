import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from folderflow import app
from folderflow.instance import AlreadyRunning,acquire_instance_lock


@unittest.skipUnless(os.name=='nt','Windows byte-range locks')
class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'application.lock'

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_lock_initializes_and_close_allows_restart(self):
        with acquire_instance_lock(self.path) as handle:
            self.assertEqual(os.fstat(handle.fileno()).st_size,1)
        self.assertEqual(self.path.read_bytes(),b'0')
        with acquire_instance_lock(self.path):
            pass

    def test_legacy_process_lock_is_detected_without_reading_locked_byte(self):
        # Use the old locking sequence in another process: this is the real
        # Windows failure seen when clicking an EXE while another version runs.
        code='''import msvcrt,sys
with open(sys.argv[1],'a+b') as handle:
    handle.write(b'0')
    handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    print('locked',flush=True)
    sys.stdin.readline()
'''
        process=subprocess.Popen([sys.executable,'-c',code,str(self.path)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with open(self.path,'rb') as old_reader:
                with self.assertRaises(PermissionError):
                    old_reader.read(1)
            for _ in range(3):
                with self.assertRaises(AlreadyRunning):
                    acquire_instance_lock(self.path)
        finally:
            process.communicate('\n',timeout=10)
        self.assertEqual(process.returncode,0)
        self.assertEqual(self.path.read_bytes(),b'0')
        with acquire_instance_lock(self.path):
            pass

    def test_repeated_main_launch_reports_existing_instance_without_creating_gui(self):
        with patch.object(app,'STATE',Path(self.temp.name)), \
             patch.object(app,'acquire_instance_lock',side_effect=AlreadyRunning), \
             patch.object(app,'load_config',return_value={'language':'en'}), \
             patch.object(app.ctypes.windll.user32,'MessageBoxW',return_value=1) as message, \
             patch.object(app.tk,'Tk') as gui:
            app.main()
        gui.assert_not_called()
        self.assertIn('already running',message.call_args.args[1])

    def test_real_access_failure_is_not_misreported_as_running(self):
        with patch.object(app,'STATE',Path(self.temp.name)), \
             patch.object(app,'acquire_instance_lock',side_effect=PermissionError('access denied')), \
             patch.object(app,'resolve_language',return_value='en'), \
             patch.object(app.ctypes.windll.user32,'MessageBoxW',return_value=1) as message, \
             patch.object(app.tk,'Tk') as gui:
            app.main()
        gui.assert_not_called()
        self.assertIn('data directory',message.call_args.args[1])
        self.assertIn('access denied',message.call_args.args[1])

    def test_gui_initialization_failure_releases_lock(self):
        with patch.object(app,'STATE',Path(self.temp.name)), \
             patch.object(app.tk,'Tk',side_effect=RuntimeError('GUI unavailable')):
            with self.assertRaisesRegex(RuntimeError,'GUI unavailable'):
                app.main()
        with acquire_instance_lock(self.path):
            pass
