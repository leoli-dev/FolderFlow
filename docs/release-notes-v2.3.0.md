# FolderFlow 2.3.0

- Configurable scan/analysis concurrency (1–16 workers, default 4).
- Configurable file-transfer concurrency (1–8 workers, default 1) under Edit task → Performance & retries.
- Bounded queues and manifest batches for large inventories; less repeated volume/reserve lookup work.
- One owner commits manifest writes and publishes files; I/O workers never use SQLite.
- Aggregate progress tracks all active files. Successful files remain individually durable.
- Retains both source and local SHA-256 reads, source change detection, validated resume prefixes,
  version retention, mirror recycling gates, and space checks before each copy block.
- Copy rate limits use one worker to retain task-wide bandwidth semantics. Full audit remains serial.
- Avoids exporting the same large CSV report twice after a successful GUI operation.
- Existing task IDs, file IDs, completed records, and partial-file formats remain compatible.

Try 2–4 transfer workers for small files and 1 for busy HDDs. Actual performance depends on
directory structure, SMB latency, disk contention and link bandwidth. See PERFORMANCE.md.
