# Performance and concurrency

FolderFlow 2.3 uses threads for directory enumeration, destination metadata checks,
and optional concurrent file transfers. Waiting for SMB and disk I/O does not need
separate Python processes. Transfer workers read/copy/hash isolated partial files;
the coordinator alone publishes files, updates progress, and commits the manifest.

## Settings

Edit task → **Performance & retries**:

| Setting | Range | Default | Suggested starting point |
| --- | --- | --- | --- |
| Scan workers | 1–16 | 4 | 4; try 8 for latency-heavy directory trees |
| Transfer workers | 1–8 | 1 | 2–4 for many small files; 1 for busy HDDs |

Scan workers also inspect destination metadata during capacity planning. A copy
rate limit uses one worker so the limit applies to the whole task. Selected tasks
run one after another; worker limits apply to the current task, not each task at once.
Full audit remains serial. Increasing workers can make a busy mechanical disk slower.

One flat directory can only have one enumeration stream. Multiple files inside it
can still be transferred concurrently. Concurrency cannot raise physical bandwidth.
For example, 1 Gbit/s is about 119 MiB/s before protocol overhead. Copying a file
and independently reading the source again for SHA-256 use source bandwidth twice.

## Integrity and recovery

Each file still checks the scan fingerprint, validates any resumed prefix, durably
writes the partial file, independently hashes source and local copy, checks source
metadata again, then publishes and commits. Fully copied but unpublished parts can
be resumed after pause or restart. Completed items are credited only after their
individual durable commit. Overall progress sums the work of active files; the
file-level bar shows the most recently updated file.

Manifest scanning uses bounded 256-record batches and durable checkpoints about
every half second. An incomplete scan cannot be executed. Transfer scheduling is
bounded by the configured worker count; large manifests are not loaded into memory.
Disk-volume and reserve lookups are reused during a run, while free space is checked
before each copy block. Free-space admission and actual writes are synchronized.
Successful GUI runs export the report once.

## Reproducible local fixture

The benchmark creates and removes its own temporary source, target and manifest.
It never loads installed task configuration or touches a NAS. With a prepared
development environment:

```powershell
& .\.venv\Scripts\python.exe .\scripts\benchmark.py --scan-workers 4 --copy-workers 4 --latency-ms 10
```

The fixture contains 960 files (4.5 KiB each) in 121 directories. `--latency-ms 10`
adds a **synthetic 10 ms wait per source directory**, only during scanning. Copy
timings are real local file operations including capacity planning, hashes,
publication, durable commits and report generation. These are not NAS benchmarks.

Example measurements on Windows 11 / Python 3.13 x64 during development:

| Implementation | Scan (synthetic wait) | Local copy including verification |
| --- | --- | --- |
| 2.2, serial | 1.53 s | 20.38 s |
| 2.3, 1 scan / 1 transfer worker | 1.37 s | 11.09 s |
| 2.3, 4 scan / 4 transfer workers | 0.41 s | 4.61 s |

Timings vary with OS caches, storage and competing activity. The example shows
overlap and reduced repeated work; it does not promise a particular speedup on
another machine or dataset. All 960 outputs were verified in each run.

For an actual task, compare elapsed scan time and total run time using the same
unchanged dataset and different worker counts. Prefer the lowest concurrency that
keeps the bottleneck busy. Check the aggregate phase as well as copy MiB/s: hashing,
metadata planning and report export also take time.

Implementation references: [Python ThreadPoolExecutor](https://docs.python.org/3.13/library/concurrent.futures.html)
and [hashlib](https://docs.python.org/3.13/library/hashlib.html).
