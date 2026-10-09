"""Read-only source, durable resumable file-content backup. Python stdlib only."""
import csv
import ctypes
import fnmatch
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from .progress import OverallProgress

GIB = 1024 ** 3
BLOCK = 4 * 1024 ** 2


class Stopped(Exception):
    pass


class Changed(Exception):
    pass


class NoSpace(Exception):
    pass


def longpath(path):
    s = os.path.abspath(os.fspath(path))
    if os.name != 'nt' or s.startswith('\\\\?\\'):
        return s
    return '\\\\?\\UNC\\' + s[2:] if s.startswith('\\\\') else '\\\\?\\' + s


def fingerprint(st):
    return [st.st_size, st.st_mtime_ns]


def is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def safe_path(root, rel=''):
    root = os.path.abspath(root)
    dest = os.path.abspath(os.path.join(root, rel))
    if os.path.commonpath([root, dest]) != root:
        raise ValueError('路径越界: ' + rel)
    # Reject junctions/symlinks in ALL existing ancestors, including selected parent.
    p = Path(dest)
    for item in [p, *p.parents]:
        try:
            if is_link(os.lstat(longpath(item))):
                raise ValueError('目标包含链接或 junction: ' + str(item))
        except FileNotFoundError:
            pass
    return longpath(dest)


def volume(path):
    if os.name == 'nt':
        buf = ctypes.create_unicode_buffer(1024)
        if not ctypes.windll.kernel32.GetVolumePathNameW(str(Path(path).absolute()), buf, len(buf)):
            # Selected subdirectory might not yet exist.
            p = Path(path)
            while not p.exists() and p != p.parent:
                p = p.parent
            if not ctypes.windll.kernel32.GetVolumePathNameW(str(p.absolute()), buf, len(buf)):
                raise OSError('无法识别目标磁盘: ' + str(path))
        return buf.value.upper()
    return str(Path(path).anchor)


def reserve(path):
    return max(128 * 1024 ** 2, int(shutil.disk_usage(volume(path)).total * .01))


def paths_overlap(first, second):
    """Compare resolved paths to prevent copying backups back into a source."""
    a = os.path.normcase(os.path.realpath(first))
    b = os.path.normcase(os.path.realpath(second))
    try:
        return os.path.commonpath([a, b]) in (a, b)
    except ValueError:
        return False


class Engine:
    def __init__(self, state_dir, config, emit=None, reserve_fn=reserve):
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.emit = emit or (lambda *args, **kw: None)
        self.reserve_fn = reserve_fn
        self.stop = threading.Event()
        self.retry_seconds = 10
        self.retry_limit = 30
        self.overall = None
        self.current_file = None
        self.last_overall = 0
        self.last_phase = ''
        self.db = sqlite3.connect(self.state / 'manifest.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS files (
              id INTEGER PRIMARY KEY, share TEXT, rel TEXT, size INTEGER,
              mtime INTEGER, seen TEXT, status TEXT DEFAULT 'pending',
              target TEXT, sha256 TEXT, verified_at TEXT, error TEXT,
              UNIQUE(share,rel));
            CREATE INDEX IF NOT EXISTS file_status ON files(status,seen);
            CREATE TABLE IF NOT EXISTS dirs (share TEXT, rel TEXT, seen TEXT,
              UNIQUE(share,rel));
            CREATE TABLE IF NOT EXISTS issues (phase TEXT, share TEXT, rel TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS versions (share TEXT, rel TEXT, path TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS mirror_plan (id INTEGER PRIMARY KEY,
              share TEXT,rel TEXT,seen TEXT,size INTEGER,status TEXT,error TEXT,UNIQUE(share,rel));
            CREATE INDEX IF NOT EXISTS files_inventory ON files(share,seen,id);
        ''')
        self.scan_id = self.meta('scan_id')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(files)')}
        if 'local_mtime' not in columns:
            self.db.execute('ALTER TABLE files ADD COLUMN local_mtime INTEGER')
            self.db.commit()
        if 'plan_action' not in columns:
            self.db.execute('ALTER TABLE files ADD COLUMN plan_action TEXT')
            self.db.commit()
        if 'audit_seen' not in columns:
            self.db.execute('ALTER TABLE files ADD COLUMN audit_seen TEXT')
            self.db.commit()
        for key in config['shares']:
            if not self.meta(f'task:{key}:scan_id'):
                last = self.db.execute("SELECT seen FROM dirs WHERE share=? AND rel=''", (key,)).fetchone()
                self.meta(f'task:{key}:scan_id', last[0] if last else self.scan_id)
                self.meta(f'task:{key}:scan_complete', self.meta('scan_complete') or '0')

    def close(self):
        self.db.close()

    def select_file(self, row):
        self.current_file = {'id':row['id'],'size':row['size'],'share':row['share'],'rel':row['rel']}
        if self.overall:
            self.overall.select(row['id'],row['size'])

    def file_event(self, value):
        if self.current_file:
            value = dict(value, id=self.current_file['id'])
        if self.overall:
            self.overall.update(value['phase'],value['done'],value['size'])
        self.emit('file',value)
        self.overall_event()

    def overall_event(self, force=False):
        if not self.overall:
            return
        now = time.monotonic()
        if force or self.overall.phase != self.last_phase or now-self.last_overall >= .15:
            self.last_overall = now
            self.last_phase = self.overall.phase
            self.emit('overall',self.overall.snapshot())

    def start_progress(self, mode):
        files,size,done,done_bytes = 0,0,0,0
        for key in self.config['shares']:
            row = self.db.execute("""SELECT count(*) n,coalesce(sum(size),0) b,
                coalesce(sum(status='verified' AND target=?),0) v,
                coalesce(sum(CASE WHEN status='verified' AND target=? THEN size ELSE 0 END),0) vb
                FROM files WHERE share=? AND seen=?""",(self.target(key),self.target(key),key,self.task_id(key))).fetchone()
            files += row['n']
            size += row['b']
            if mode == 'backup':
                done += row['v']
                done_bytes += row['vb']
                files += self.db.execute('SELECT count(*) FROM mirror_plan WHERE share=? AND seen=?',
                    (key,self.task_id(key))).fetchone()[0]
            else:
                self.meta(f'task:{key}:audit_run',self.task_id(key))
        self.overall = OverallProgress(files,size,done,done_bytes,mode,self.scan_errors())
        self.overall_event(True)

    def complete_progress(self, row):
        if self.overall:
            self.overall.complete(row['size'])
        self.emit('complete_file',{'id':row['id'],'share':row['share'],'rel':row['rel'],'bytes':row['size']})
        self.current_file = None
        self.overall_event(True)

    def failed_progress(self, row, error):
        if self.overall:
            self.overall.fail()
            self.overall.issues = self.active_issues()
        self.emit('file_failed',{'id':row['id'],'share':row['share'],'rel':row['rel'],'error':str(error)})
        self.current_file = None
        self.overall_event(True)

    def finish_progress(self, state):
        if self.overall:
            if state == 'done' and self.overall.completed_files != self.overall.files and not self.active_issues():
                self.issue('copy','', '', '执行清单仍有未完成项目，请重新分析')
            if state == 'done' and self.active_issues():
                state = 'failed'
            self.overall.finish(state,self.active_issues())
            self.current_file = None
            self.overall_event(True)

    def meta(self, key, value=None):
        if value is not None:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
            self.db.commit()
            return value
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else ''

    def check_stop(self):
        if self.stop.is_set():
            raise Stopped('已暂停；再次开始会恢复')

    def log(self, text):
        self.emit('log', text)
        with open(self.state / 'activity.log', 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')

    def issue(self, phase, share, rel, error):
        self.db.execute('INSERT INTO issues VALUES (?,?,?,?)', (phase, share, rel, str(error)))
        self.db.commit()
        self.log(f'{phase}: {share}/{rel}: {error}')

    def root(self, share):
        return self.config['shares'][share]['source']

    def target(self, share):
        return self.config['shares'][share].get('target', '')

    def task_id(self, share):
        return self.meta(f'task:{share}:scan_id') or self.scan_id

    def task_reserve(self, share):
        value = self.config['shares'][share].get('reserve_gib')
        return int(float(value) * GIB) if value is not None else self.reserve_fn(self.target(share))

    def managed(self, share):
        return self.config['shares'][share].get('layout', 'managed') == 'managed'

    def data_path(self, share, rel=''):
        prefix = 'files' if self.managed(share) else ''
        return safe_path(self.target(share), os.path.join(prefix, rel))

    def control_path(self, share, rel=''):
        prefix = '_control' if self.managed(share) else os.path.join('.folderflow', share)
        return safe_path(self.target(share), os.path.join(prefix, rel))

    def matches(self, share, rel, directory=False):
        rel = rel.replace('\\', '/').strip('/')
        if rel == '.folderflow' or rel.startswith('.folderflow/'):
            return False
        entry = self.config['shares'][share]
        def match(pattern):
            pattern = pattern.replace('\\', '/').strip('/')
            if directory and pattern.endswith('/**') and rel == pattern[:-3]:
                return True
            return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel.rsplit('/', 1)[-1], pattern)
        if any(match(p) for p in entry.get('exclude', [])):
            return False
        return directory or not entry.get('include') or any(match(p) for p in entry['include'])

    def active_issues(self, phase=None, share=None):
        keys = [share] if share else list(self.config['shares'])
        if not keys:
            return 0
        sql = 'SELECT count(*) FROM issues WHERE (share IN (' + ','.join('?' for _ in keys) + ") OR share='')"
        args = keys[:]
        if phase:
            sql += ' AND phase=?'
            args.append(phase)
        return self.db.execute(sql, args).fetchone()[0]

    def validate_configuration(self):
        if not self.config['shares']:
            raise ValueError('请先添加至少一个来源文件夹或磁盘')
        for name, entry in self.config['shares'].items():
            source = entry['source']
            if not os.path.isabs(source):
                raise ValueError('来源必须是完整路径：' + source)
            if entry.get('target') and not os.path.isabs(entry['target']):
                raise ValueError('目标必须是完整路径：' + entry['target'])
            for option in ('reserve_gib','max_mibps','retry_limit','retry_seconds'):
                value = float(entry.get(option, 0))
                if not math.isfinite(value) or value < 0:
                    raise ValueError('空间、限速、重试次数和间隔必须为非负有限数字')
            if paths_overlap(source, self.state) and os.path.commonpath([
                    os.path.normcase(os.path.realpath(source)),
                    os.path.normcase(os.path.realpath(self.state))]) == os.path.normcase(os.path.realpath(source)):
                raise ValueError('来源包含本软件的状态目录；请选择具体文件夹，或将软件放到其他盘：' + source)
            for other, dest in self.config['shares'].items():
                if dest.get('target') and paths_overlap(source, dest['target']):
                    raise ValueError('来源和目标不能互相包含：' + name + ' / ' + other)
        targets = [(name, data['target']) for name, data in self.config['shares'].items() if data.get('target')]
        for i, (name, target) in enumerate(targets):
            for other, other_target in targets[i + 1:]:
                if paths_overlap(target, other_target):
                    raise ValueError('两个任务的专用目标目录不能互相包含：' + name + ' / ' + other)

    def scan(self):
        self.check_stop()
        self.validate_configuration()
        self.scan_id = uuid.uuid4().hex
        self.meta('scan_complete', '0')
        self.meta('audit_ok', '0')
        self.meta('scan_id', self.scan_id)
        for share in self.config['shares']:
            self.db.execute("DELETE FROM issues WHERE phase='scan' AND share=?", (share,))
            self.meta(f'task:{share}:scan_id', self.scan_id)
            self.meta(f'task:{share}:scan_complete', '0')
            self.meta(f'task:{share}:audit_ok', '0')
        self.db.commit()
        count, total = 0, 0
        for share in self.config['shares']:
            self.log('扫描 ' + share)
            share_count, share_bytes, excluded = 0, 0, 0
            stack = [('', self.root(share))]
            while stack:
                self.check_stop()
                rel, directory = stack.pop()
                try:
                    dst = os.lstat(longpath(directory))
                    if is_link(dst):
                        raise ValueError('源目录为链接/reparse point，需单独处理')
                    self.db.execute('INSERT OR REPLACE INTO dirs VALUES (?,?,?)',
                                    (share, rel, self.scan_id))
                    with os.scandir(longpath(directory)) as entries:
                        for ent in entries:
                            self.check_stop()
                            childrel = os.path.join(rel, ent.name)
                            try:
                                st = ent.stat(follow_symlinks=False)
                                if not self.matches(share, childrel, stat.S_ISDIR(st.st_mode)):
                                    excluded += 1
                                    continue
                                if is_link(st):
                                    raise ValueError('链接/reparse point 未复制，需单独处理')
                                if stat.S_ISDIR(st.st_mode):
                                    stack.append((childrel, os.path.join(directory, ent.name)))
                                elif stat.S_ISREG(st.st_mode):
                                    self.db.execute('''INSERT INTO files
                                      (share,rel,size,mtime,seen) VALUES (?,?,?,?,?)
                                      ON CONFLICT(share,rel) DO UPDATE SET
                                      status=CASE WHEN files.size!=excluded.size OR
                                        files.mtime!=excluded.mtime THEN 'pending' ELSE files.status END,
                                      size=excluded.size,mtime=excluded.mtime,seen=excluded.seen''',
                                      (share, childrel, st.st_size, st.st_mtime_ns, self.scan_id))
                                    count += 1
                                    total += st.st_size
                                    share_count += 1
                                    share_bytes += st.st_size
                                    if count % 200 == 0:
                                        self.db.commit()
                                        self.emit('scan', {'files': count, 'bytes': total, 'share': share,
                                            'share_files': share_count, 'share_bytes': share_bytes})
                                else:
                                    raise ValueError('非普通文件，需单独处理')
                            except OSError as e:
                                self.issue('scan', share, childrel, e)
                            except ValueError as e:
                                self.issue('scan', share, childrel, e)
                except (OSError, ValueError) as e:
                    self.issue('scan', share, rel, e)
                self.db.commit()
            self.meta(f'task:{share}:scan_complete', '1')
            self.meta(f'task:{share}:scan_time', time.strftime('%Y-%m-%d %H:%M:%S'))
            self.meta(f'task:{share}:excluded', excluded)
            self.emit('summary', self.summary())
        self.meta('scan_complete', '1')
        self.meta('scan_time', time.strftime('%Y-%m-%d %H:%M:%S'))
        self.emit('summary', self.summary())
        self.log(f'扫描结束：{count:,} 个文件；错误 {self.scan_errors()}')
        return self.summary()

    def scan_errors(self):
        return self.active_issues('scan')

    def summary(self):
        result = {}
        for share in self.config['shares']:
            row = self.db.execute('''SELECT count(*) n,coalesce(sum(size),0) b,
               coalesce(sum(CASE WHEN status='verified' AND target=? THEN 1 ELSE 0 END),0) v,
               coalesce(sum(CASE WHEN status='verified' AND target=? THEN size ELSE 0 END),0) vb
               FROM files WHERE share=? AND seen=?''',
               (self.target(share), self.target(share), share, self.task_id(share))).fetchone()
            result[share] = dict(row)
            result[share]['errors'] = self.db.execute(
                'SELECT count(*) FROM issues WHERE share=? AND phase="scan"', (share,)).fetchone()[0]
            result[share]['target'] = self.target(share)
            result[share]['source'] = self.root(share)
            result[share]['label'] = self.config['shares'][share].get('label', share)
            result[share]['scan_complete'] = self.meta(f'task:{share}:scan_complete') == '1'
            result[share]['audit_ok'] = self.meta(f'task:{share}:audit_ok') == '1'
            result[share]['scan_time'] = self.meta(f'task:{share}:scan_time')
            result[share]['excluded'] = int(self.meta(f'task:{share}:excluded') or 0)
        return result

    def check_local_file(self, row):
        target = self.target(row['share'])
        if not target or target != row['target']:
            return False
        try:
            st = os.stat(self.data_path(row['share'], row['rel']))
            expected_mtime = row['local_mtime'] if row['local_mtime'] is not None else row['mtime']
            return st.st_size == row['size'] and st.st_mtime_ns == expected_mtime
        except (OSError, ValueError):
            return False

    def capacity(self):
        """Conservative peak additional bytes; old versions remain on disk."""
        groups = {}
        unassigned = []
        task_plans = {}
        for share in self.config['shares']:
            self.db.execute('DELETE FROM mirror_plan WHERE share=?',(share,))
            target = self.target(share)
            if not target:
                unassigned.append(share)
                continue
            vol = volume(target)
            group = groups.setdefault(vol, {'needed': 0, 'shares': [],
                 'free': shutil.disk_usage(vol).free, 'reserve': self.task_reserve(share)})
            group['reserve'] = max(group['reserve'], self.task_reserve(share))
            group['shares'].append(share)
            plan = {'new': 0, 'update': 0, 'unchanged': 0, 'bytes': 0, 'extra': 0}
            task_plans[share] = plan
            planned = 0
            for row in self.db.execute('SELECT * FROM files WHERE share=? AND seen=?', (share, self.task_id(share))):
                self.check_stop()
                if row['status'] != 'verified' or not self.check_local_file(row):
                    # Do not credit partial files; conservative scan avoids understating demand.
                    group['needed'] += row['size'] + 4096
                    plan['bytes'] += row['size']
                    action = 'update' if os.path.exists(self.data_path(share,row['rel'])) else 'new'
                    plan[action] += 1
                    if row['status'] == 'verified':
                        self.db.execute("UPDATE files SET status='pending' WHERE id=?",(row['id'],))
                else:
                    action = 'skip'
                    plan['unchanged'] += 1
                if row['plan_action'] != action:
                    self.db.execute('UPDATE files SET plan_action=? WHERE id=?',(action,row['id']))
                planned += 1
                if planned % 500 == 0:
                    self.db.commit()
                    self.emit('planning',{'share':share,'files':planned})
            ndirs = self.db.execute('SELECT count(*) FROM dirs WHERE share=? AND seen=?',
                                   (share, self.task_id(share))).fetchone()[0]
            group['needed'] += ndirs * 4096
            if self.config['shares'][share].get('mode') == 'mirror':
                for rel in self.extra_files(share):
                    size = os.stat(self.data_path(share,rel)).st_size
                    self.db.execute('INSERT INTO mirror_plan(share,rel,seen,size,status) VALUES (?,?,?,?,?)',
                        (share,rel,self.task_id(share),size,'pending'))
                    plan['extra'] += 1
            self.meta(f'task:{share}:plan_scan',self.task_id(share))
            self.meta(f'task:{share}:plan_target',target)
        self.db.commit()
        for group in groups.values():
            group['ok'] = group['needed'] + group['reserve'] <= group['free']
        return {'volumes': groups, 'unassigned': unassigned, 'tasks': task_plans,
                'ok': not unassigned and all(g['ok'] for g in groups.values())}

    def prepare_target(self, share):
        target = self.target(share)
        if not target:
            raise ValueError('未选择目标：' + share)
        path = safe_path(target)
        if not self.managed(share):
            os.makedirs(path, exist_ok=True)
            os.makedirs(self.control_path(share), exist_ok=True)
        marker = safe_path(target, '.nasrescue.json') if self.managed(share) else self.control_path(share, 'task.json')
        if os.path.exists(path):
            if os.path.exists(marker):
                with open(marker, encoding='utf-8') as f:
                    info = json.load(f)
                if info.get('source') != self.root(share) or info.get('format') != 1:
                    raise ValueError('目标属于另一任务，选择其他文件夹：' + target)
            elif self.managed(share) and os.listdir(path):
                raise ValueError('专用备份子目录已存在且不是本软件创建：' + target)
        os.makedirs(path, exist_ok=True)
        if not os.path.exists(marker):
            with open(marker, 'x', encoding='utf-8') as f:
                json.dump({'format': 1, 'source': self.root(share)}, f)
                f.flush()
                os.fsync(f.fileno())
        os.makedirs(self.data_path(share), exist_ok=True)
        for sub in ('parts', 'versions', 'deleted'):
            os.makedirs(self.control_path(share, sub), exist_ok=True)
        return target

    def ensure_space(self, target, required):
        free = shutil.disk_usage(volume(target)).free
        this_volume = volume(target)
        keep = max((self.task_reserve(key) for key in self.config['shares']
            if self.target(key) and volume(self.target(key)) == this_volume), default=self.reserve_fn(target))
        if free - required < keep:
            raise NoSpace('磁盘空间不足或已到保留空间下限：' + target)

    def digest(self, path, limit=None, progress=None):
        h = hashlib.sha256()
        read = 0
        with open(longpath(path), 'rb') as f:
            while limit is None or read < limit:
                self.check_stop()
                chunk = f.read(BLOCK if limit is None else min(BLOCK, limit - read))
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
                if progress:
                    progress(read)
        if limit is not None and read != limit:
            raise Changed('文件长度在读取中变化')
        return h.hexdigest()

    def copy_file(self, row):
        self.select_file(row)
        share, rel = row['share'], row['rel']
        target = self.target(share)
        source = longpath(os.path.join(self.root(share), rel))
        st = os.lstat(source)
        if is_link(st):
            raise Changed('源文件变成链接')
        if fingerprint(st) != [row['size'], row['mtime']]:
            raise Changed('源文件在扫描后发生变化；重新扫描')
        dst = self.data_path(share, rel)
        part = self.control_path(share, f'parts/{row["id"]}.part')
        header = self.control_path(share, f'parts/{row["id"]}.json')
        expected = {'source': source, 'fingerprint': fingerprint(st)}
        resume = False
        try:
            with open(header, encoding='utf-8') as f:
                resume = json.load(f) == expected and os.path.getsize(part) <= st.st_size
        except (OSError, ValueError):
            pass
        offset = os.path.getsize(part) if resume else 0
        if offset:
            self.file_event({'share': share, 'rel': rel, 'phase': '核对续传段', 'done': offset, 'size': st.st_size})
            if self.digest(source, offset) != self.digest(part, offset):
                offset = 0
        self.ensure_space(target, st.st_size - offset + 4096)
        with open(header, 'w', encoding='utf-8') as f:
            json.dump(expected, f)
            f.flush()
            os.fsync(f.fileno())
        os.makedirs(self.data_path(share, os.path.dirname(rel)), exist_ok=True)
        started, initial = time.monotonic(), offset
        with open(source, 'rb') as src, open(part, 'r+b' if os.path.exists(part) else 'w+b') as out:
            out.truncate(offset)
            out.seek(offset)
            src.seek(offset)
            flushed = offset
            while offset < st.st_size:
                self.check_stop()
                chunk = src.read(min(BLOCK, st.st_size - offset))
                if not chunk:
                    raise Changed('源文件提前结束')
                self.ensure_space(target, len(chunk) + 4096)
                out.write(chunk)
                offset += len(chunk)
                if offset - flushed >= 64 * 1024 ** 2:
                    out.flush()
                    os.fsync(out.fileno())
                    flushed = offset
                self.file_event({'share': share, 'rel': rel, 'phase': '复制', 'done': offset,
                     'size': st.st_size, 'speed': (offset - initial) / max(.01, time.monotonic() - started)})
                speed_limit = self.config['shares'][share].get('max_mibps', 0) * 1024 ** 2
                if speed_limit:
                    delay = (offset-initial)/speed_limit - (time.monotonic()-started)
                    if delay > 0 and self.stop.wait(delay):
                        self.check_stop()
            out.flush()
            os.fsync(out.fileno())
            if fingerprint(os.fstat(src.fileno())) != fingerprint(st):
                raise Changed('源文件复制期间发生变化')
        self.file_event({'share': share, 'rel': rel, 'phase': 'SHA-256 校验来源', 'done': 0, 'size': st.st_size})
        def source_progress(n):
            self.file_event({'share': share, 'rel': rel, 'phase': 'SHA-256 校验来源', 'done': n, 'size': st.st_size})
        sha = self.digest(source, progress=source_progress)
        self.file_event({'share': share, 'rel': rel, 'phase': 'SHA-256 校验本地', 'done': 0, 'size': st.st_size})
        localsha = self.digest(part, progress=lambda n: self.file_event({
            'share': share, 'rel': rel, 'phase': 'SHA-256 校验本地', 'done': n, 'size': st.st_size}))
        if fingerprint(os.stat(source)) != fingerprint(st):
            raise Changed('校验期间源文件发生变化')
        if sha != localsha:
            # Invalidate resumable header; next attempt starts with a fresh partial file.
            with open(header, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            raise Changed('哈希不一致，未标记完成；重新扫描/重试')
        os.utime(part, ns=(st.st_atime_ns, st.st_mtime_ns))
        if os.path.exists(dst) and self.config['shares'][share].get('keep_versions', True):
            archive = self.control_path(share, f'versions/{row["id"]}/{uuid.uuid4().hex}')
            os.makedirs(self.control_path(share, f'versions/{row["id"]}'), exist_ok=True)
            os.replace(dst, archive)
            self.db.execute('INSERT INTO versions VALUES (?,?,?,?)',
                (share, rel, archive, time.strftime('%Y-%m-%d %H:%M:%S')))
            self.db.commit()
            self.log('保留旧版本：' + archive)
        os.replace(part, dst)
        self.db.execute('''UPDATE files SET status='verified',target=?,sha256=?,verified_at=?,local_mtime=?,error=NULL
                        WHERE id=?''', (target, sha, time.strftime('%Y-%m-%d %H:%M:%S'), os.stat(dst).st_mtime_ns, row['id']))
        self.db.commit()
        self.complete_progress(row)

    def backup(self):
        try:
            return self._backup()
        except Stopped:
            self.finish_progress('paused')
            raise
        except Exception:
            self.finish_progress('failed')
            raise

    def _backup(self):
        self.check_stop()
        self.validate_configuration()
        if any(self.meta(f'task:{key}:scan_complete') != '1' for key in self.config['shares']):
            raise ValueError('请先完成扫描')
        self.meta('audit_ok', '0')
        cap = self.capacity()
        self.emit('capacity', cap)
        if not cap['ok']:
            raise NoSpace('目标未全部选择或磁盘剩余空间不足')
        for key in self.config['shares']:
            self.db.execute("DELETE FROM issues WHERE phase IN ('copy','audit') AND (share=? OR share='')", (key,))
            self.meta(f'task:{key}:audit_ok','0')
        self.db.commit()
        self.start_progress('backup')
        last_summary = time.monotonic()
        for share in self.config['shares']:
            self.prepare_target(share)
            # Preserve empty directories too.
            for d in self.db.execute('SELECT rel FROM dirs WHERE share=? AND seen=?', (share, self.task_id(share))):
                self.check_stop()
                try:
                    os.makedirs(self.data_path(share, d[0]), exist_ok=True)
                except (OSError, ValueError) as e:
                    self.issue('copy', share, d[0], e)
            rows = self.db.execute('SELECT * FROM files WHERE share=? AND seen=? ORDER BY id', (share, self.task_id(share)))
            for row in rows:
                self.check_stop()
                if row['status'] == 'verified' and self.check_local_file(row):
                    continue
                retry_limit = self.config['shares'][share].get('retry_limit', self.retry_limit)
                retry_seconds = self.config['shares'][share].get('retry_seconds', self.retry_seconds)
                for attempt in range(retry_limit + 1):
                    try:
                        self.copy_file(row)
                        break
                    except OSError as e:
                        # Network and sharing errors can recover; permissions/path failures cannot.
                        win = getattr(e, 'winerror', None)
                        retryable = win in (32, 33, 53, 54, 59, 64, 67, 121, 1231, 1232, 1236, 10053, 10054, 10060)
                        if retryable and attempt < retry_limit:
                            self.log(f'连接/文件占用，{retry_seconds} 秒后重试：{share}/{row["rel"]} ({attempt+1})')
                            if self.stop.wait(retry_seconds):
                                self.check_stop()
                            continue
                        self.db.execute("UPDATE files SET status='error',error=? WHERE id=?", (str(e), row['id']))
                        self.db.commit()
                        self.issue('copy', share, row['rel'], e)
                        self.failed_progress(row,e)
                        if retryable:
                            raise OSError('连接连续重试后仍不可用；本轮停止，稍后点击继续。') from e
                        break
                    except (Changed, ValueError) as e:
                        self.db.execute("UPDATE files SET status='error',error=? WHERE id=?", (str(e), row['id']))
                        self.db.commit()
                        self.issue('copy', share, row['rel'], e)
                        self.failed_progress(row,e)
                        break
                if time.monotonic() - last_summary >= 2:
                    self.emit('summary', self.summary())
                    last_summary = time.monotonic()
        for share in self.config['shares']:
            if self.config['shares'][share].get('mode') == 'mirror':
                self.overall.phase = '镜像整理与回收'
                self.overall_event(True)
                self.recycle_extras(share)
        self.finish_progress('done')
        self.emit('summary', self.summary())
        self.export_report()
        self.log('本轮执行结束。可运行「完整校验」比对来源与目标。')

    def extra_files(self, share):
        root = self.data_path(share)
        if not os.path.isdir(root):
            return
        stack = [('', root)]
        while stack:
            rel, folder = stack.pop()
            self.check_stop()
            with os.scandir(folder) as entries:
                for ent in entries:
                    child = os.path.join(rel, ent.name)
                    st = ent.stat(follow_symlinks=False)
                    if not rel and ent.name == '.folderflow':
                        continue
                    if not self.matches(share, child, stat.S_ISDIR(st.st_mode)):
                        continue
                    if is_link(st):
                        raise ValueError('目标存在链接/reparse point，请单独处理：' + child)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append((child, ent.path))
                    elif stat.S_ISREG(st.st_mode):
                        exists = self.db.execute('SELECT 1 FROM files WHERE share=? AND rel=? AND seen=?',
                            (share, child, self.task_id(share))).fetchone()
                        if not exists:
                            yield child

    def recycle_extras(self, share):
        pending = self.db.execute("SELECT count(*) FROM files WHERE share=? AND seen=? AND status!='verified'",
            (share, self.task_id(share))).fetchone()[0]
        if self.active_issues(share=share) or pending:
            self.log('镜像回收未执行：存在失败或未完成项目：' + share)
            return
        batch = uuid.uuid4().hex
        for rel in self.extra_files(share):
            self.check_stop()
            # The source must still be reachable and this particular path still absent.
            if not os.path.isdir(longpath(self.root(share))):
                raise OSError('来源离线，停止镜像回收')
            try:
                os.lstat(longpath(os.path.join(self.root(share), rel)))
                continue
            except FileNotFoundError as e:
                if getattr(e, 'winerror', 2) not in (2, 3):
                    raise
            source = self.data_path(share, rel)
            recycled = self.control_path(share, os.path.join('deleted', batch, rel))
            os.makedirs(self.control_path(share, os.path.join('deleted', batch, os.path.dirname(rel))), exist_ok=True)
            os.replace(source, recycled)
            self.db.execute('INSERT INTO versions VALUES (?,?,?,?)',
                (share, rel, recycled, time.strftime('%Y-%m-%d %H:%M:%S')))
            self.db.commit()
            item = self.db.execute('SELECT id FROM mirror_plan WHERE share=? AND rel=? AND seen=?',
                (share,rel,self.task_id(share))).fetchone()
            if item:
                self.db.execute("UPDATE mirror_plan SET status='done' WHERE id=?",(item[0],))
                self.db.commit()
                if self.overall:
                    self.overall.complete(0)
                self.emit('recycled_file',{'id':item[0],'share':share,'rel':rel})
                self.overall_event(True)
            self.log('镜像回收（保留可恢复副本）：' + rel)
        # Remove target-only EMPTY directories after recycling files; retain a directory record.
        stack, folders = [('', self.data_path(share))], []
        while stack:
            rel, folder = stack.pop()
            with os.scandir(folder) as entries:
                for ent in entries:
                    child = os.path.join(rel, ent.name)
                    st = ent.stat(follow_symlinks=False)
                    if not self.matches(share, child, True) or is_link(st):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        folders.append((child, ent.path))
                        stack.append((child, ent.path))
        for rel, folder in reversed(folders):
            self.check_stop()
            if os.listdir(folder):
                continue
            known = self.db.execute('SELECT 1 FROM dirs WHERE share=? AND rel=? AND seen=?',
                (share, rel, self.task_id(share))).fetchone()
            if known:
                continue
            if not os.path.isdir(longpath(self.root(share))):
                raise OSError('来源离线，停止镜像回收')
            try:
                os.lstat(longpath(os.path.join(self.root(share), rel)))
                continue
            except FileNotFoundError as e:
                if getattr(e, 'winerror', 2) not in (2,3):
                    raise
            archive = self.control_path(share, os.path.join('deleted', batch, '_empty-directories', rel))
            os.makedirs(archive, exist_ok=True)
            os.rmdir(folder)
            self.db.execute('INSERT INTO versions VALUES (?,?,?,?)',
                (share, rel + os.sep, archive, time.strftime('%Y-%m-%d %H:%M:%S')))
            self.db.commit()

    def audit(self):
        try:
            return self._audit()
        except Stopped:
            self.finish_progress('paused')
            raise
        except Exception:
            self.finish_progress('failed')
            raise

    def _audit(self):
        """Fresh inventory + read/hash EVERY source and destination + another inventory."""
        self.meta('audit_ok', '0')
        for key in self.config['shares']:
            self.db.execute("DELETE FROM issues WHERE phase='audit' AND (share=? OR share='')", (key,))
        self.db.commit()
        self.scan()
        baseline = self.scan_id
        if self.scan_errors():
            self.export_report()
            raise ValueError('扫描存在不可读取项目，无法通过最终校验')
        self.start_progress('audit')
        for row in self.db.execute('SELECT * FROM files WHERE seen=? ORDER BY id', (baseline,)):
            self.check_stop()
            self.select_file(row)
            share, rel = row['share'], row['rel']
            try:
                if row['status'] != 'verified' or not self.check_local_file(row):
                    raise Changed('尚未完成复制或本地文件已改变')
                src = longpath(os.path.join(self.root(share), rel))
                dst = self.data_path(share, rel)
                before = os.stat(src)
                dst_before = os.stat(dst)
                self.file_event({'share': share, 'rel': rel, 'phase': '完整校验来源', 'done': 0, 'size': row['size']})
                def tick(n):
                    self.file_event({'share': share, 'rel': rel, 'phase': '完整校验来源', 'done': n, 'size': row['size']})
                a = self.digest(src, progress=tick)
                b = self.digest(dst, progress=lambda n: self.file_event({
                    'share': share, 'rel': rel, 'phase': '最终校验本地', 'done': n, 'size': row['size']}))
                if fingerprint(before) != [row['size'], row['mtime']] or fingerprint(os.stat(src)) != fingerprint(before):
                    raise Changed('最终校验期间源文件发生变化')
                if fingerprint(os.stat(dst)) != fingerprint(dst_before) or a != b:
                    raise Changed('源文件与本地副本不一致或本地文件校验中变化')
                self.db.execute('UPDATE files SET sha256=?,verified_at=?,audit_seen=? WHERE id=?',
                    (a, time.strftime('%Y-%m-%d %H:%M:%S'), baseline, row['id']))
                self.db.commit()
                self.complete_progress(row)
            except (OSError, ValueError, Changed) as e:
                self.db.execute("UPDATE files SET status='error',error=? WHERE id=?", (str(e), row['id']))
                self.db.commit()
                self.issue('audit', share, rel, e)
                self.failed_progress(row,e)
        # Check directory existence, including empty dirs, and record baseline in a temporary table.
        for d in self.db.execute('SELECT * FROM dirs WHERE seen=?', (baseline,)):
            try:
                if not os.path.isdir(self.data_path(d['share'], d['rel'])):
                    raise Changed('本地目录缺失')
            except (OSError, ValueError, Changed) as e:
                self.issue('audit', d['share'], d['rel'], e)
        self.db.executescript('''DROP TABLE IF EXISTS audit_baseline;
            CREATE TABLE audit_baseline AS SELECT share,rel,size,mtime FROM files WHERE 0;
            DROP TABLE IF EXISTS audit_dirs;
            CREATE TABLE audit_dirs AS SELECT share,rel FROM dirs WHERE 0;''')
        self.db.execute('INSERT INTO audit_baseline SELECT share,rel,size,mtime FROM files WHERE seen=?', (baseline,))
        self.db.execute('INSERT INTO audit_dirs SELECT share,rel FROM dirs WHERE seen=?', (baseline,))
        self.db.commit()
        self.overall.phase = '确认校验后的清单'
        self.overall_event(True)
        self.scan()
        delta = self.db.execute('''SELECT count(*) FROM (
          SELECT share,rel,size,mtime FROM files WHERE seen=? EXCEPT
          SELECT share,rel,size,mtime FROM audit_baseline)''', (self.scan_id,)).fetchone()[0]
        delta += self.db.execute('''SELECT count(*) FROM (
          SELECT share,rel,size,mtime FROM audit_baseline EXCEPT
          SELECT share,rel,size,mtime FROM files WHERE seen=?)''', (self.scan_id,)).fetchone()[0]
        delta += self.db.execute('''SELECT count(*) FROM (
          SELECT share,rel FROM dirs WHERE seen=? EXCEPT SELECT share,rel FROM audit_dirs)''', (self.scan_id,)).fetchone()[0]
        delta += self.db.execute('''SELECT count(*) FROM (
          SELECT share,rel FROM audit_dirs EXCEPT SELECT share,rel FROM dirs WHERE seen=?)''', (self.scan_id,)).fetchone()[0]
        if delta:
            self.issue('audit', '', '', f'校验前后清单有 {delta} 项变化；重新同步后校验')
        for key in self.config['shares']:
            if self.config['shares'][key].get('mode') == 'mirror' and next(self.extra_files(key), None) is not None:
                self.issue('audit', key, '', '镜像目标仍有来源中不存在的文件')
        errors = self.active_issues()
        pending = sum(v['n'] - v['v'] for v in self.summary().values())
        ok = not errors and not pending
        self.meta('audit_ok', '1' if ok else '0')
        for key in self.config['shares']:
            self.meta(f'task:{key}:audit_ok', '1' if ok else '0')
            self.meta(f'task:{key}:audit_time', time.strftime('%Y-%m-%d %H:%M:%S'))
        self.meta('audit_time', time.strftime('%Y-%m-%d %H:%M:%S'))
        self.finish_progress('done' if ok else 'failed')
        self.export_report()
        self.log('完整校验' + ('通过' if ok else '未通过；查看报告'))
        return ok

    def export_report(self):
        report = self.state.parent / 'reports'
        report.mkdir(parents=True, exist_ok=True)
        pairs = [(key,self.task_id(key)) for key in self.config['shares']]
        active = '(' + ' OR '.join('(share=? AND seen=?)' for _ in pairs) + ')' if pairs else '0'
        absent = '(' + ' OR '.join('(share=? AND seen!=?)' for _ in pairs) + ')' if pairs else '0'
        args = tuple(x for pair in pairs for x in pair)
        keys = tuple(self.config['shares'])
        scope = 'share IN (' + ','.join('?' for _ in keys) + ')' if keys else '0'
        for name, sql, params in [
            ('file-manifest.csv', 'SELECT share,rel,size,mtime,status,target,sha256,verified_at,error FROM files WHERE '+active, args),
            ('issues.csv', 'SELECT * FROM issues WHERE '+scope+" OR share=''", keys),
            ('retained-versions.csv', 'SELECT * FROM versions WHERE '+scope, keys),
            ('source-missing-retained.csv', 'SELECT share,rel,target,status FROM files WHERE '+absent, args)]:
            cursor = self.db.execute(sql, params)
            with open(report / name, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in cursor.description])
                writer.writerows(cursor)
        payload = {'scope': '当前会话可读的普通文件主数据流和目录',
            'not_included': ['不可见/无权限的数据', 'ACL/所有者/备用数据流', '符号链接及 reparse point',
                            '系统镜像/快照/运行中应用数据库一致性/加密密钥'],
            'scan_complete': bool(keys) and all(self.meta(f'task:{key}:scan_complete') == '1' for key in keys), 'scan_time': self.meta('scan_time'),
            'final_audit_passed': bool(keys) and all(self.meta(f'task:{key}:audit_ok') == '1' for key in keys), 'audit_time': self.meta('audit_time'),
            'tasks': self.summary(), 'configuration': self.config['shares'], 'issues': self.active_issues(),
            'caveat': '文件逐项校验不提供原子快照；需要一致性时先停止来源写入。'}
        with open(report / 'summary.json', 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self.emit('report', str(report))
        return report
