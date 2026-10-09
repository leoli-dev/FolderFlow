"""Write trusted Windows version-resource source from the package version."""
from pathlib import Path
import re
import sys
from folderflow import __version__

if not re.fullmatch(r'\d+\.\d+\.\d+', __version__):
    raise ValueError('The release version must be MAJOR.MINOR.PATCH')
numbers = tuple(map(int, __version__.split('.'))) + (0,)
output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0',[
    StringStruct('CompanyName','FolderFlow contributors'),
    StringStruct('FileDescription','Portable folder backup and synchronization'),
    StringStruct('FileVersion','{__version__}'),
    StringStruct('InternalName','FolderFlow'),
    StringStruct('LegalCopyright','Copyright 2026 FolderFlow contributors'),
    StringStruct('OriginalFilename','FolderFlow.exe'),
    StringStruct('ProductName','FolderFlow'),
    StringStruct('ProductVersion','{__version__}')
  ])]),VarFileInfo([VarStruct('Translation',[1033,1200])])]
)\n''', encoding='utf-8')
