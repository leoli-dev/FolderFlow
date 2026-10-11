# FolderFlow 2.3.1

A startup fix for portable Windows installations. Extract the ZIP into a writable
folder and double-click `FolderFlow.exe`; no installation or administrator privileges
are required for a writable folder.

- Fixes the `Permission denied` / unhandled launcher exception when an instance is
  already running from the same application directory, including another version.
- Acquires the single-instance byte-range lock before touching its contents, so
  repeated launches show the existing-instance message instead of crashing.
- Keeps compatibility with lock files created by 2.2 and 2.3.0. Does not delete or
  bypass a lock held by a running backup.
- Releases the lock if GUI initialization fails. Genuine data-directory access
  failures show a readable error identifying the directory.
- Includes Windows process-level regression tests reproducing the original failure.

Close the existing FolderFlow window before switching to this build. Keep `data/`
when upgrading to preserve tasks and completed progress. Existing manifests, task
IDs and resumable partial files are unchanged. All concurrency and verification
features from 2.3.0 are retained.

Validated on Windows 11 x64 with Python 3.13. All 88 tests passed, including fresh
startup, legacy lock contention, restart after close and initialization failure.

[English README](https://github.com/leoli-dev/FolderFlow/blob/v2.3.1/README.md) ·
[简体中文](https://github.com/leoli-dev/FolderFlow/blob/v2.3.1/README_CN.md)
