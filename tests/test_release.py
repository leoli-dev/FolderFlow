import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/package_release.py'
spec = importlib.util.spec_from_file_location('release_packager', SCRIPT)
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for relative in packager.FILES:
            path = self.root/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'public release fixture')
        self.runtime=self.root/'build/binary/FolderFlow/_internal'
        self.runtime.mkdir(parents=True)
        (self.runtime/'python313.dll').write_bytes(b'public runtime fixture')

    def tearDown(self):
        self.temp.cleanup()

    def test_package_allowlist_excludes_private_local_state(self):
        secret = self.root/'dist/FolderFlow/data/config.json'
        secret.parent.mkdir(parents=True)
        secret.write_text('PRIVATE TEST SENTINEL', encoding='utf-8')
        report = self.root/'reports/private.csv'
        report.parent.mkdir()
        report.write_text('PRIVATE TEST SENTINEL', encoding='utf-8')
        archive = packager.package(self.root)
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(set(z.namelist()), {'FolderFlow/'+p for p in packager.release_files(self.root).values()})
            self.assertFalse(any('PRIVATE TEST SENTINEL' in z.read(name).decode(errors='ignore') for name in z.namelist()))
        self.assertEqual(secret.read_text(encoding='utf-8'), 'PRIVATE TEST SENTINEL')
        self.assertTrue((self.root/'dist/SHA256SUMS.txt').exists())

    def test_missing_public_input_stops_packaging(self):
        (self.root/'LICENSE').unlink()
        with self.assertRaises(ValueError):
            packager.package(self.root)

    def test_private_file_inside_runtime_stops_packaging(self):
        (self.runtime/'private.sqlite').write_text('PRIVATE TEST SENTINEL')
        with self.assertRaises(ValueError):
            packager.package(self.root)

    def test_empty_runtime_stops_packaging(self):
        (self.runtime/'python313.dll').unlink()
        with self.assertRaises(ValueError):
            packager.package(self.root)

    def test_unrelated_build_files_are_not_packaged(self):
        private=self.root/'build/binary/FolderFlow/data/config.json'
        private.parent.mkdir()
        private.write_text('PRIVATE TEST SENTINEL')
        archive=packager.package(self.root)
        with zipfile.ZipFile(archive) as z:
            self.assertNotIn('FolderFlow/data/config.json',z.namelist())
            self.assertEqual(z.read('FolderFlow/_internal/python313.dll'),b'public runtime fixture')


if __name__ == '__main__':
    unittest.main(verbosity=2)
