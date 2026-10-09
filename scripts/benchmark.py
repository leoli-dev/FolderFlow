"""Isolated local fixture benchmark. Never opens user configuration or shares."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def benchmark(scan_workers, copy_workers, latency=0):
    from folderflow.engine import Engine
    import folderflow.engine as module
    with tempfile.TemporaryDirectory(prefix='folderflow-bench-') as temp:
        base = Path(temp)
        source = base/'source'
        source.mkdir()
        for i in range(120):
            folder = source/f'directory-{i}'
            folder.mkdir()
            for j in range(8):
                (folder/f'file-{j}.bin').write_bytes(b'benchmark'*512)
        config = {'shares': {'demo': {'source':str(source), 'target':str(base/'target'),
            'layout':'direct', 'reserve_gib':0, 'scan_workers':scan_workers,
            'copy_workers':copy_workers}}}
        engine = Engine(base/'app/data', config)
        original = os.scandir
        def delayed(path):
            time.sleep(latency)
            return original(path)
        if latency:
            os.scandir = delayed
        try:
            begin = time.perf_counter()
            engine.scan()
            scan_seconds = time.perf_counter()-begin
        finally:
            os.scandir = original
        begin = time.perf_counter()
        engine.backup()
        copy_seconds = time.perf_counter()-begin
        result = {'files':960, 'directories':121, 'scan_workers':scan_workers,
            'copy_workers':copy_workers, 'simulated_directory_latency_ms':latency*1000,
            'scan_seconds':round(scan_seconds,3), 'copy_seconds':round(copy_seconds,3),
            'verified':engine.summary()['demo']['v']}
        engine.close()
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-tree', default=str(Path(__file__).resolve().parents[1]/'src'))
    parser.add_argument('--scan-workers', type=int, default=1)
    parser.add_argument('--copy-workers', type=int, default=1)
    parser.add_argument('--latency-ms', type=float, default=0,
        help='Optional synthetic directory wait; not an actual network benchmark.')
    args = parser.parse_args()
    sys.path.insert(0,args.source_tree)
    print(json.dumps(benchmark(args.scan_workers,args.copy_workers,args.latency_ms/1000)))
