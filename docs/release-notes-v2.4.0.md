# FolderFlow 2.4.0

Faster startup for the portable Windows application.

- Changes the default portable ZIP to a folder bundle: `FolderFlow.exe` plus
  `_internal/`. No installer or separate Python installation is required, and the
  runtime no longer needs to be extracted into a temporary folder on every launch.
- Displays the interface before loading historical task statistics. Large manifest
  reads now run in the background instead of blocking the first UI frame.
- Reads saved summaries through a read-only SQLite connection, without initializing
  the backup engine or running schema writes on the UI thread during startup.
- Cancels stale history queries and prevents them overwriting a running job's totals.
- Preserves the existing configuration, manifest, task IDs, verified files and partials.

Extract the **whole FolderFlow folder** and double-click `FolderFlow.exe`. Keep the
`_internal/` directory beside the EXE; copying only the EXE will not work. Close the
old application before replacing files, and preserve `data/` when upgrading.

Startup time still depends on disk, OS cache and security scanning. These changes
remove repeated runtime extraction and UI-thread manifest aggregation; they do not
promise identical startup time on every machine.

Validation includes delayed-history first-frame responsiveness, read-only summary
accuracy, stale-query cancellation, runtime packaging and the existing backup tests.
