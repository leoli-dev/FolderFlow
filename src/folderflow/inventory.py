"""Read-only, bounded pages of the manifest. No source/target filesystem access."""
import math
from pathlib import Path
import sqlite3


def empty_result(page_size=200):
    return {'rows':[], 'count':0,'page':1,'pages':1,'page_size':page_size,
        'metrics':{'total_files':0,'total_bytes':0,'completed_files':0,'completed_bytes':0,
                   'failed_files':0,'issues':0,'ready':False}}


def load_summary(database, config, cancelled=None):
    """Saved task totals without Engine initialization, schema writes or filesystem scans."""
    path=Path(database)
    if not path.is_file() or not config['shares']:
        return {}
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    conn.row_factory=sqlite3.Row
    if cancelled:
        conn.set_progress_handler(lambda:int(cancelled()),10000)
    try:
        conn.execute('BEGIN')
        meta=dict(conn.execute('SELECT key,value FROM meta'))
        result={}
        for key,entry in config['shares'].items():
            seen=meta.get(f'task:{key}:scan_id',meta.get('scan_id',''))
            target=entry.get('target','')
            row=conn.execute('''SELECT count(*) n,coalesce(sum(size),0) b,
                coalesce(sum(CASE WHEN status='verified' AND target=? THEN 1 ELSE 0 END),0) v,
                coalesce(sum(CASE WHEN status='verified' AND target=? THEN size ELSE 0 END),0) vb
                FROM files WHERE share=? AND seen=?''',(target,target,key,seen)).fetchone()
            result[key]=dict(row,errors=conn.execute(
                "SELECT count(*) FROM issues WHERE share=? AND phase='scan'",(key,)).fetchone()[0],
                target=target,source=entry['source'],label=entry.get('label',key),
                scan_complete=meta.get(f'task:{key}:scan_complete',meta.get('scan_complete'))=='1',
                audit_ok=meta.get(f'task:{key}:audit_ok')=='1',
                scan_time=meta.get(f'task:{key}:scan_time',''),
                excluded=int(meta.get(f'task:{key}:excluded') or 0))
        return result
    finally:
        conn.close()


def load_page(database, config, keys, state='pending', search='', page=1, page_size=200, cancelled=None, mode='backup'):
    path = Path(database)
    keys = [key for key in keys if key in config['shares']]
    if not path.is_file() or not keys:
        return empty_result(page_size)
    page_size = max(1,min(500,int(page_size)))
    conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    conn.row_factory = sqlite3.Row
    if cancelled:
        conn.set_progress_handler(lambda: int(cancelled()),10000)
    try:
        conn.execute('BEGIN')
        meta = dict(conn.execute('SELECT key,value FROM meta'))
        columns = {row[1] for row in conn.execute('PRAGMA table_info(files)')}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        source_parts, source_args, mirror_parts, mirror_args = [], [], [], []
        ready = True
        for key in keys:
            entry = config['shares'][key]
            seen = meta.get(f'task:{key}:scan_id',meta.get('scan_id',''))
            target = entry.get('target','')
            has_plan = (meta.get(f'task:{key}:plan_scan') == seen and meta.get(f'task:{key}:plan_target') == target)
            ready = ready and meta.get(f'task:{key}:scan_complete',meta.get('scan_complete')) == '1' and bool(target)
            action = 'plan_action' if 'plan_action' in columns and has_plan else "'unknown'"
            done_extra = ''
            extra_args = []
            if mode == 'audit':
                done_extra = ' AND audit_seen=?' if 'audit_seen' in columns else ' AND 0'
                if 'audit_seen' in columns:
                    extra_args.append(meta.get(f'task:{key}:audit_run',''))
            source_parts.append(f'''SELECT 'file:'||id uid,0 kind,id sort_id,share task,rel,size,
                coalesce({action},'unknown') action,
                CASE WHEN status='verified' AND target=? {done_extra} THEN 'done'
                     WHEN status='error' THEN 'error'
                     WHEN status IN ('copying','verifying') THEN status ELSE 'pending' END state,
                coalesce(error,'') error
                FROM files WHERE share=? AND seen=?''')
            source_args.extend([target,*extra_args,key,seen])
            if mode != 'audit' and 'mirror_plan' in tables and entry.get('mode') == 'mirror' and has_plan:
                mirror_parts.append("SELECT 'mirror:'||id uid,1 kind,id sort_id,share task,rel,size,'recycle' action,status state,coalesce(error,'') error FROM mirror_plan WHERE share=? AND seen=?")
                mirror_args.extend([key,seen])
        parts = source_parts + mirror_parts
        args = source_args + mirror_args
        base = ' UNION ALL '.join(parts)
        stats = conn.execute(f'''SELECT count(*) total_files,
            coalesce(sum(CASE WHEN kind=0 THEN size ELSE 0 END),0) total_bytes,
            coalesce(sum(state='done'),0) completed_files,
            coalesce(sum(CASE WHEN state='done' AND kind=0 THEN size ELSE 0 END),0) completed_bytes,
            coalesce(sum(state='error'),0) failed_files FROM ({base})''',args).fetchone()
        metrics = dict(stats)
        placeholders = ','.join('?' for _ in keys)
        metrics['issues'] = conn.execute(f"SELECT count(*) FROM issues WHERE share IN ({placeholders}) OR share=''",keys).fetchone()[0]
        metrics['ready'] = ready
        # Show unreadable directories/files too, without duplicating tracked file failures.
        parts.append(f'''SELECT 'issue:'||rowid uid,2 kind,rowid sort_id,share task,rel,NULL size,
            'unreadable' action,'error' state,detail error FROM issues i
            WHERE (share IN ({placeholders}) OR share='') AND NOT EXISTS (
              SELECT 1 FROM files f WHERE f.share=i.share AND f.rel=i.rel
              AND f.seen=coalesce((SELECT value FROM meta WHERE key='task:'||i.share||':scan_id'),
                                 (SELECT value FROM meta WHERE key='scan_id')))''')
        args.extend(keys)
        base = ' UNION ALL '.join(parts)
        filters, filter_args = [], []
        if state == 'pending':
            filters.append("state IN ('pending','copying','verifying')")
        elif state == 'done':
            filters.append("state='done'")
        elif state == 'error':
            filters.append("state='error'")
        elif state == 'recycle':
            filters.append("action='recycle'")
        if search:
            escaped = search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            filters.append("rel LIKE ? ESCAPE '\\'")
            filter_args.append('%'+escaped+'%')
        where = ' WHERE '+' AND '.join(filters) if filters else ''
        sql = '('+base+')'+where
        count = conn.execute('SELECT count(*) FROM '+sql,args+filter_args).fetchone()[0]
        pages = max(1,math.ceil(count/page_size))
        page = min(pages,max(1,int(page)))
        rows = conn.execute('SELECT * FROM '+sql+' ORDER BY kind,sort_id LIMIT ? OFFSET ?',
            args+filter_args+[page_size,(page-1)*page_size]).fetchall()
        return {'rows':[dict(row) for row in rows],'count':count,'page':page,'pages':pages,
                'page_size':page_size,'metrics':metrics}
    finally:
        conn.close()
