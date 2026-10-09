"""Run-wide progress with a fixed denominator and commit-based completion."""
import time


class OverallProgress:
    def __init__(self, files, size, completed_files=0, completed_bytes=0, mode='backup', issues=0):
        self.files = files
        self.size = size
        self.completed_files = completed_files
        self.completed_bytes = completed_bytes
        self.base_bytes = completed_bytes
        self.mode = mode
        self.issues = issues
        self.failed_files = 0
        self.current_id = None
        self.current_size = 0
        self.credit = 0.0
        self.inflight = {}
        self.active_phases = {}
        self.started = time.monotonic()
        self.state = 'running'
        self.phase = '复制与校验' if mode == 'backup' else '完整校验'

    def select(self, file_id, size):
        self.current_id = file_id
        self.current_size = size
        self.credit = 0.0
        self.inflight.setdefault(file_id,0.0)

    def update(self, phase, done, size, file_id=None):
        self.phase = phase
        fraction = min(1, max(0, done / max(1, size)))
        if self.mode == 'audit':
            stage = 1 if '本地' in phase else 0
            self.credit = size * min(.999999, (stage + fraction) / 2)
        elif '续传' in phase:
            self.credit = 0
        else:
            stage = 2 if '校验本地' in phase else 1 if '校验来源' in phase else 0
            self.credit = size * min(.999999, (stage + fraction) / 3)
        self.inflight[self.current_id if file_id is None else file_id] = self.credit
        self.active_phases[self.current_id if file_id is None else file_id] = phase

    def complete(self, size, file_id=None):
        self.active_phases.pop(self.current_id if file_id is None else file_id,None)
        self.inflight.pop(self.current_id if file_id is None else file_id,None)
        self.completed_files += 1
        self.completed_bytes += size
        self.current_id = None
        self.current_size = 0
        self.credit = 0.0

    def fail(self, file_id=None):
        self.active_phases.pop(self.current_id if file_id is None else file_id,None)
        self.inflight.pop(self.current_id if file_id is None else file_id,None)
        self.failed_files += 1
        self.current_id = None
        self.current_size = 0
        self.credit = 0.0

    def finish(self, state, issues=0):
        self.state = state
        self.issues = issues
        self.current_id = None
        self.credit = 0.0
        self.inflight.clear()
        self.active_phases.clear()
        self.phase = {'done':'完成', 'failed':'存在失败项目', 'paused':'已暂停'}.get(state, state)

    def snapshot(self):
        processed = min(self.size, self.completed_bytes + sum(self.inflight.values()))
        percent = 100 * processed / self.size if self.size else 100 * self.completed_files / max(1,self.files)
        complete = self.state == 'done' and not self.issues and self.completed_files == self.files
        percent = 100.0 if complete else min(99.9, percent)
        elapsed = max(.001,time.monotonic()-self.started)
        rate = max(0,processed-self.base_bytes)/elapsed
        eta = (self.size-processed)/rate if rate and elapsed >= 2 and self.state == 'running' else None
        return {'total_files':self.files,'total_bytes':self.size,
            'completed_files':self.completed_files,'completed_bytes':self.completed_bytes,
            'failed_files':self.failed_files,'issues':self.issues,
            'percent':max(0,percent),'eta_seconds':eta,'phase':self.phase,'state':self.state,
            'mode':self.mode,'current_id':self.current_id,'active_files':len(self.inflight),
            'active_items':dict(self.active_phases)}
