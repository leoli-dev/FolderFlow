"""Build a portable release from an explicit allowlist; never collect runtime data."""
import hashlib
from pathlib import Path
import shutil
import uuid
import zipfile
from folderflow import __version__

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'build/binary/FolderFlow.exe': 'FolderFlow.exe',
    'README.md': 'README.md',
    'README.en.md': 'README.en.md',
    'CONTRIBUTING.md': 'CONTRIBUTING.md',
    'SECURITY.md': 'SECURITY.md',
    'LICENSE': 'LICENSE',
    'THIRD_PARTY_NOTICES.md': 'THIRD_PARTY_NOTICES.md',
    'docs/user-guide.txt': '使用说明.txt',
    'docs/images/sync-progress.png': 'docs/images/sync-progress.png',
    'docs/BUILD.md': 'docs/BUILD.md',
    'docs/RELEASE.md': 'docs/RELEASE.md',
    f'docs/release-notes-v{__version__}.md': f'docs/release-notes-v{__version__}.md',
    'third_party/PYTHON-LICENSE.txt': 'third_party/PYTHON-LICENSE.txt',
    'third_party/TCL-TK-LICENSE.txt': 'third_party/TCL-TK-LICENSE.txt',
    'third_party/PYINSTALLER-COPYING.txt': 'third_party/PYINSTALLER-COPYING.txt',
    'third_party/APACHE-2.0.txt': 'third_party/APACHE-2.0.txt',
    'third_party/OPENSSL-NOTICE.txt': 'third_party/OPENSSL-NOTICE.txt',
    'third_party/SQLITE-NOTICE.txt': 'third_party/SQLITE-NOTICE.txt',
    'third_party/LIBFFI-LICENSE.txt': 'third_party/LIBFFI-LICENSE.txt',
    'third_party/ZLIB-LICENSE.txt': 'third_party/ZLIB-LICENSE.txt',
}


def package(root=ROOT):
    root = Path(root).resolve()
    stage = root/'build'/('release-stage-'+uuid.uuid4().hex)/'FolderFlow'
    stage.mkdir(parents=True)
    for source, relative in FILES.items():
        source_path = root/source
        if any(p.is_symlink() or p.is_junction() for p in [source_path, *source_path.parents]) or not source_path.is_file():
            raise ValueError('Missing or linked release input: '+source)
        dest = stage/relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest)
    portable = root/'dist'/'FolderFlow'
    portable.mkdir(parents=True, exist_ok=True)
    # Replace only allowlisted application files. Existing local data/reports stay local.
    for relative in FILES.values():
        dest = portable/relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stage/relative, dest)
    filename = f'FolderFlow-{__version__}-windows-x64-portable.zip'
    archive = root/'dist'/filename
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for relative in FILES.values():
            z.write(stage/relative,'FolderFlow/'+relative)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root/'dist'/'SHA256SUMS.txt').write_text(f'{digest}  {filename}\n',encoding='utf-8')
    print('Portable executable:', portable/'FolderFlow.exe')
    print('Public release ZIP:', archive)
    print('SHA256:', digest)
    return archive


if __name__=='__main__':
    package()
