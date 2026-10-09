# FolderFlow 2.1.0

- Full manifest browser with pending/all/completed/failed/mirror filters, literal path search and 200-item pages.
- Separate run-wide and current-file progress bars, completed-item and verified-byte totals, problem counts and approximate remaining time.
- Overall progress includes previously verified files in the selected task scope. Copying and both hash passes contribute partial progress; only validated, published files count as complete.
- Failed or paused operations never show a successful 100%. Full verification has its own run-wide progress and queue.
- Read-only asynchronous manifest queries keep large inventories out of the GUI thread; existing manifests are migrated without replacing file IDs or resume data.
- Logs moved into their own tab, leaving the file queue visible by default.

For an existing backup: pause and close the old application, replace program files and retain `data/`.
The new version can resume using the existing manifest and partial files. Do not replace an executable while its backup is running.
