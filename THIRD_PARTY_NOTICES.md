# Third-party notices

FolderFlow's application source is licensed under MIT. Portable binaries bundle
the Python interpreter, Tcl/Tk, and PyInstaller runtime components.

| Component | Terms / notice |
| --- | --- |
| Python 3.13 | [Python license and included component notices](third_party/PYTHON-LICENSE.txt) |
| Tcl/Tk 8.6 | [Tcl/Tk license terms](third_party/TCL-TK-LICENSE.txt) |
| PyInstaller 6.22.3 | [PyInstaller COPYING and bootloader exception](third_party/PYINSTALLER-COPYING.txt) |
| PyInstaller runtime hooks | Apache License 2.0; see [full license](third_party/APACHE-2.0.txt) |
| OpenSSL 3.x libcrypto | Apache License 2.0; [notice](third_party/OPENSSL-NOTICE.txt) and [license](third_party/APACHE-2.0.txt) |
| SQLite | Public-domain library; [notice](third_party/SQLITE-NOTICE.txt) |
| libffi | MIT-style terms; [full license](third_party/LIBFFI-LICENSE.txt) |
| zlib | zlib terms; [full license](third_party/ZLIB-LICENSE.txt) |

PyInstaller's bootloader exception permits distribution of combined application
executables under the application's license. Runtime hooks retain their own
Apache 2.0 terms. See the [official PyInstaller license documentation](https://pyinstaller.org/en/stable/license.html).

Build dependencies are pinned in `requirements-build.txt`; they are not installed
on the end user's machine. The release package includes this file and all notices
in `third_party/`. If you change the bundled interpreter or dependencies, review
and refresh the corresponding notices before distributing that build.
