"""Bounded I/O workers. Manifest writes and file publication stay on the owner thread."""
from concurrent.futures import ThreadPoolExecutor
import os
import queue
import stat
import threading


def scan_batches(engine, share, workers):
    from .engine import is_link, longpath
    output = queue.Queue(maxsize=workers*4)
    cancel = threading.Event()
    def send(value):
        while not cancel.is_set() and not engine.stop.is_set():
            try:
                output.put(value, timeout=.1)
                return True
            except queue.Full:
                pass
        return False
    def read_directory(rel, folder):
        batch = []
        try:
            st = os.lstat(longpath(folder))
            if is_link(st):
                raise ValueError('源目录为链接/reparse point，需单独处理')
            batch.append(('directory', rel))
            with os.scandir(longpath(folder)) as entries:
                for ent in entries:
                    if cancel.is_set() or engine.stop.is_set():
                        return
                    child = os.path.join(rel,ent.name)
                    try:
                        st = ent.stat(follow_symlinks=False)
                        if not engine.matches(share, child, stat.S_ISDIR(st.st_mode)):
                            batch.append(('excluded',))
                        elif is_link(st):
                            raise ValueError('链接/reparse point 未复制，需单独处理')
                        elif stat.S_ISDIR(st.st_mode):
                            batch.append(('child',child,os.path.join(folder,ent.name)))
                        elif stat.S_ISREG(st.st_mode):
                            batch.append(('file',child,st.st_size,st.st_mtime_ns))
                        else:
                            raise ValueError('非普通文件，需单独处理')
                    except (OSError,ValueError) as e:
                        batch.append(('error',child,str(e)))
                    if len(batch) >= 256:
                        if not send(('batch',batch)):
                            return
                        batch = []
        except (OSError,ValueError) as e:
            batch.append(('error',rel,str(e)))
        except Exception as e:
            send(('fatal',e))
        finally:
            if batch:
                send(('batch',batch))
            send(('done',None))
    stack = [('',engine.root(share))]
    active = 0
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='scan') as pool:
        try:
            while stack or active:
                engine.check_stop()
                while stack and active < workers:
                    pool.submit(read_directory,*stack.pop())
                    active += 1
                try:
                    kind,batch = output.get(timeout=.1)
                except queue.Empty:
                    continue
                if kind == 'fatal':
                    raise batch
                if kind == 'done':
                    active -= 1
                else:
                    records = []
                    for item in batch:
                        if item[0] == 'child':
                            stack.append(item[1:])
                        else:
                            records.append(item)
                    yield records
        finally:
            cancel.set()


def parallel_copy(engine, rows, workers):
    """Workers only create/check partials; the owner publishes and commits results."""
    from .engine import Changed, Engine, Stopped
    output = queue.Queue(maxsize=workers*8)
    cancel = threading.Event()
    futures = {}
    exhausted = False
    def send(kind,value):
        while not cancel.is_set():
            try:
                output.put((kind,value),timeout=.1)
                return
            except queue.Full:
                if engine.stop.is_set():
                    return
    def transfer(row):
        # Deliberately no SQLite connection on this lightweight I/O context.
        worker = object.__new__(Engine)
        worker.config,worker.stop = engine.config,engine.stop
        worker.overall,worker.current_file = None,None
        worker.emit = send
        worker.ensure_space = engine.ensure_space
        worker.write_lock = engine.write_lock
        worker.retry_limit,worker.retry_seconds = engine.retry_limit,engine.retry_seconds
        def check_stop():
            if cancel.is_set():
                raise Stopped('已暂停；再次开始会恢复')
            engine.check_stop()
        worker.check_stop = check_stop
        entry = engine.config['shares'][row['share']]
        limit = entry.get('retry_limit',engine.retry_limit)
        seconds = entry.get('retry_seconds',engine.retry_seconds)
        for attempt in range(limit+1):
            try:
                return worker.transfer_file(row)
            except OSError as e:
                if getattr(e,'winerror',None) not in Engine.RETRYABLE or attempt >= limit:
                    raise
                send('log',f'连接/文件占用，{seconds} 秒后重试：{row["share"]}/{row["rel"]} ({attempt+1})')
                if engine.stop.wait(seconds) or cancel.is_set():
                    worker.check_stop()
    def drain():
        while True:
            try:
                kind,value = output.get_nowait()
            except queue.Empty:
                break
            if kind == 'file':
                engine.file_event(value)
            elif kind == 'log':
                engine.log(value)
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='copy') as pool:
        try:
            while futures or not exhausted:
                engine.check_stop()
                while not exhausted and len(futures) < workers:
                    try:
                        row = next(rows)
                    except StopIteration:
                        exhausted = True
                        break
                    if row['status']=='verified' and engine.check_local_file(row):
                        continue
                    row = dict(row)
                    futures[pool.submit(transfer,row)] = row
                drain()
                ready = [future for future in futures if future.done()]
                if not ready:
                    # Wake promptly for progress and pause without spinning.
                    try:
                        kind,value = output.get(timeout=.05)
                        if kind == 'file':
                            engine.file_event(value)
                        elif kind == 'log':
                            engine.log(value)
                    except queue.Empty:
                        pass
                for future in ready:
                    row = futures.pop(future)
                    drain()
                    try:
                        result = future.result()
                        engine.publish_file(row,result)
                    except (OSError,Changed,ValueError) as e:
                        engine.record_copy_failure(row,e)
                        if isinstance(e,OSError) and getattr(e,'winerror',None) in Engine.RETRYABLE:
                            raise OSError('连接连续重试后仍不可用；本轮停止，稍后点击继续。') from e
                yield None
        finally:
            cancel.set()
            for future in futures:
                future.cancel()
