"""FolderFlow: task-based Windows file backups and one-way synchronization."""
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .engine import Engine, Stopped, is_link, safe_path
from . import __version__
from .inventory import load_page

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
STATE = BASE / 'data'
MODES = {'backup': '版本备份', 'update': '单向同步', 'mirror': '镜像同步'}
MODE_HELP = {
    'backup': '来源 → 目标；保留目标独有文件，并保留每次替换前的旧版本。',
    'update': '来源 → 目标；只复制新增和改动文件，保留目标独有文件。',
    'mirror': '来源 → 目标；目标独有文件移入可恢复的回收区；来源读取失败时停止回收。',
}


def human(n):
    n = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or unit == 'TiB':
            return f'{n:,.2f} {unit}'
        n /= 1024


def defaults():
    return {'version': 2, 'shares': {}}


def guide_path():
    return BASE/'使用说明.txt' if getattr(sys, 'frozen', False) else BASE/'docs/user-guide.txt'


def load_config():
    path = STATE / 'config.json'
    if not path.exists():
        return defaults()
    with path.open(encoding='utf-8') as f:
        config = json.load(f)
    for key, entry in config.get('shares', {}).items():
        entry.setdefault('label', key)
        entry.setdefault('mode', 'backup')
        entry.setdefault('enabled', True)
        entry.setdefault('keep_versions', True)
        entry.setdefault('layout', 'managed' if entry.get('target') else 'direct')
        entry.setdefault('reserve_gib', 1)
        entry.setdefault('include', [])
        entry.setdefault('exclude', [])
    config.pop('server', None)
    config['version'] = 2
    return config


def normalize_path(path):
    path = path.strip()
    if not path or not os.path.isabs(path):
        raise ValueError('请填写完整路径，例如 C:\\Documents、E:\\Backup 或 \\\\server\\share\\folder')
    return os.path.normpath(path)


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class App:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.events = queue.Queue()
        self.busy = False
        self.engine = None
        self.worker = None
        self.pending_close = False
        self.stop = threading.Event()
        self.latest = {}
        self.active_ids = []
        self.last_progress = 0
        self.last_overall = None
        self.overall_active = False
        self.operation = None
        self.current_file_value = None
        self.query_generation = 0
        self.query_applied = -1
        self.query_pending = None
        self.query_thread = None
        self.view_closing = False
        self.file_page = 1
        self.file_pages = 1
        self.last_view_refresh = 0
        self.scale = max(1, float(root.tk.call('tk', 'scaling')) / (96 / 72))
        width = min(round(1320 * self.scale), round(root.winfo_screenwidth() * .94))
        height = min(round(900 * self.scale), round(root.winfo_screenheight() * .92))
        self.wrap = width - 44
        self.compact = height/self.scale < 850
        self.tasks_expanded = not self.compact
        root.title(f'FolderFlow {__version__}  •  Backup & Sync')
        root.geometry(f'{width}x{height}+30+30')
        root.minsize(min(width, round(1050*self.scale)), min(height, round(700*self.scale)))
        root.configure(bg='#f3f5f8')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.style()
        self.build()
        self.refresh_saved()
        self.save_config()
        self.request_inventory()
        root.bind('<Destroy>',self.on_destroy,add='+')
        self.poll_id = root.after(120, self.poll)

    def style(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f3f5f8', foreground='#243045')
        style.configure('TButton', padding=(12, 8), background='#e7edf5')
        style.configure('Primary.TButton', background='#1768d5', foreground='white')
        style.map('Primary.TButton', background=[('active', '#0d53b3'), ('disabled', '#a3b6cc')])
        style.configure('Treeview', rowheight=round(38*self.scale), background='white', fieldbackground='white', borderwidth=0)
        style.configure('Treeview.Heading', padding=9, background='#e8edf4', font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#dceafe')], foreground=[('selected', '#142f52')])
        style.configure('TProgressbar', background='#1768d5', troughcolor='#dde4ee', borderwidth=0)

    def build(self):
        if self.compact:
            shell = ttk.Frame(self.root)
            shell.pack(fill='both',expand=True)
            canvas = tk.Canvas(shell,highlightthickness=0,bg='#f3f5f8')
            outer_scroll = ttk.Scrollbar(shell,orient='vertical',command=canvas.yview)
            outer_scroll.pack(side='right',fill='y')
            canvas.pack(side='left',fill='both',expand=True)
            canvas.configure(yscrollcommand=outer_scroll.set)
            main = ttk.Frame(canvas,padding=20)
            window = canvas.create_window(0,0,window=main,anchor='nw')
            canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
            main.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        else:
            main = ttk.Frame(self.root, padding=20)
            main.pack(fill='both', expand=True)
        header = ttk.Frame(main)
        header.pack(fill='x')
        ttk.Label(header, text='FolderFlow', font=('Segoe UI', 27, 'bold')).pack(side='left')
        ttk.Label(header, text='Backup & Sync  /  Windows', font=('Segoe UI', 12)).pack(side='right', pady=12)
        ttk.Label(main, text='保存常用任务，先分析变化，再备份或同步。', foreground='#53647a',
                  font=('Microsoft YaHei UI', 11)).pack(anchor='w', pady=(5, 16))
        bar = ttk.Frame(main)
        bar.pack(fill='x', pady=(0, 10))
        self.add_button = ttk.Button(bar, text='＋ 新建任务', style='Primary.TButton', command=lambda: self.task_dialog())
        self.add_button.pack(side='left')
        self.edit_button = ttk.Button(bar, text='编辑任务', command=self.edit_task)
        self.edit_button.pack(side='left', padx=8)
        self.duplicate_button = ttk.Button(bar, text='复制任务', command=self.duplicate_task)
        self.duplicate_button.pack(side='left')
        self.toggle_button = ttk.Button(bar, text='启用 / 停用', command=self.toggle_tasks)
        self.toggle_button.pack(side='left', padx=8)
        self.remove_button = ttk.Button(bar, text='移除任务', command=self.remove_tasks)
        self.remove_button.pack(side='left')
        self.tasks_toggle = ttk.Button(bar,text='收起任务列表' if self.tasks_expanded else '展开任务列表',command=self.toggle_task_table)
        self.tasks_toggle.pack(side='left',padx=8)
        ttk.Button(bar, text='使用说明', command=lambda: os.startfile(guide_path())).pack(side='right')
        frame = ttk.Frame(main)
        self.tasks_frame = frame
        if self.tasks_expanded:
            frame.pack(fill='x')
        columns = ('enabled', 'name', 'mode', 'source', 'target', 'files', 'size', 'verified', 'status')
        self.table = ttk.Treeview(frame, columns=columns, show='headings', height=max(1,min(4,len(self.config['shares']))), selectmode='extended')
        titles = ['启用', '任务', '模式', '来源', '目标', '文件数', '容量', '已验证', '状态']
        widths = [48, 135, 100, 260, 260, 90, 110, 140, 120]
        for col, title, width in zip(columns, titles, widths):
            self.table.heading(col, text=title)
            self.table.column(col, width=round(width*self.scale), minwidth=45,
                stretch=col in ('source', 'target'), anchor='w' if col in ('name','source','target') else 'center')
        self.table.pack(side='left', fill='x', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        scrollbar.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind('<Double-1>', lambda e: self.edit_task())
        self.table.bind('<Control-a>', lambda e: (self.table.selection_set(self.table.get_children()), 'break')[1])
        self.table.bind('<<TreeviewSelect>>', lambda e: self.show_details())
        self.details = tk.StringVar(value='点击新建任务，选择来源和目标。本机目录、移动硬盘、映射盘及 SMB 网络共享均可使用。')
        self.details_label = ttk.Label(main, textvariable=self.details, wraplength=self.wrap, foreground='#53647a')
        self.details_label.pack(anchor='w', pady=(10, 12))
        actions = ttk.Frame(main)
        actions.pack(fill='x')
        ttk.Label(actions, text='范围').pack(side='left', padx=(0, 7))
        self.scope = tk.StringVar(value='选中任务')
        self.scope_box = ttk.Combobox(actions, textvariable=self.scope, values=['选中任务', '所有启用任务'], state='readonly', width=13)
        self.scope_box.pack(side='left', padx=(0, 10))
        self.scope_box.bind('<<ComboboxSelected>>',lambda e:self.request_inventory(reset=True,scope_change=True))
        self.scan_button = ttk.Button(actions, text='分析 / 扫描', command=lambda: self.run('scan'))
        self.scan_button.pack(side='left')
        self.start_button = ttk.Button(actions, text='执行 / 继续', style='Primary.TButton', command=lambda: self.run('backup'))
        self.start_button.pack(side='left', padx=8)
        self.audit_button = ttk.Button(actions, text='完整校验', command=lambda: self.run('audit'))
        self.audit_button.pack(side='left')
        self.pause_button = ttk.Button(actions, text='暂停', command=self.pause, state='disabled')
        self.pause_button.pack(side='left', padx=8)
        ttk.Button(actions, text='打开目标', command=self.open_target).pack(side='right')
        ttk.Button(actions, text='报告与版本记录', command=self.open_reports).pack(side='right', padx=8)
        self.plan_text = tk.StringVar(value='分析会显示新增、更新、无需复制的文件和容量需求。启动后不会自动运行任务。')
        ttk.Label(main, textvariable=self.plan_text, wraplength=self.wrap).pack(anchor='w', pady=(12, 10))
        self.status = tk.StringVar(value='就绪 · 选择任务，点击分析。')
        ttk.Label(main, textvariable=self.status, font=('Microsoft YaHei UI', 12, 'bold'), wraplength=self.wrap).pack(anchor='w')
        self.overall_text = tk.StringVar(value='总体进度 · 选择任务并分析清单')
        ttk.Label(main,textvariable=self.overall_text,wraplength=self.wrap,
            font=('Microsoft YaHei UI',11,'bold')).pack(anchor='w',pady=(8,4))
        self.overall_bar = ttk.Progressbar(main,maximum=100)
        self.overall_bar.pack(fill='x',pady=(0,6))
        self.file_text = tk.StringVar(value='等待操作')
        ttk.Label(main, textvariable=self.file_text, wraplength=self.wrap).pack(anchor='w', pady=(5, 6))
        self.progress = ttk.Progressbar(main, maximum=100)
        self.progress.pack(fill='x')
        self.metric = tk.StringVar(value='文件级进度 · SHA-256 完整验证 · 安全发布副本')
        ttk.Label(main, textvariable=self.metric, foreground='#53647a').pack(anchor='w', pady=(5, 10))
        notebook = ttk.Notebook(main)
        notebook.pack(fill='both',expand=True,pady=(3,0))
        fileframe = ttk.Frame(notebook,padding=8)
        logframe = ttk.Frame(notebook)
        notebook.add(fileframe,text='文件清单')
        notebook.add(logframe,text='操作日志')
        filters = ttk.Frame(fileframe)
        filters.pack(fill='x',pady=(0,6))
        self.file_filter = tk.StringVar(value='待同步')
        self.file_filter_box = ttk.Combobox(filters,textvariable=self.file_filter,
            values=['待同步','全部文件','已完成','失败','镜像回收'],state='readonly',width=12)
        self.file_filter_box.pack(side='left')
        self.file_filter_box.bind('<<ComboboxSelected>>',lambda e:self.request_inventory(reset=True))
        self.file_search = tk.StringVar()
        entry = ttk.Entry(filters,textvariable=self.file_search,width=36)
        entry.pack(side='left',padx=8)
        entry.bind('<Return>',lambda e:self.request_inventory(reset=True))
        ttk.Button(filters,text='搜索路径',command=lambda:self.request_inventory(reset=True)).pack(side='left')
        ttk.Button(filters,text='刷新',command=self.request_inventory).pack(side='left',padx=8)
        self.inventory_text = tk.StringVar(value='清单尚未加载')
        ttk.Label(filters,textvariable=self.inventory_text).pack(side='right')
        grid = ttk.Frame(fileframe)
        grid.pack(fill='both',expand=True)
        cols = ('task','path','size','action','state','error')
        self.file_table = ttk.Treeview(grid,columns=cols,show='headings',height=3,selectmode='browse')
        for col,title,width in zip(cols,['任务','相对路径 / 文件','大小','操作','状态','问题说明'],[125,560,110,100,110,270]):
            self.file_table.heading(col,text=title)
            self.file_table.column(col,width=round(width*self.scale),minwidth=70,
                stretch=col in ('path','error'),anchor='w')
        self.file_table.grid(row=0,column=0,sticky='nsew')
        scroll = ttk.Scrollbar(grid,orient='vertical',command=self.file_table.yview)
        scroll.grid(row=0,column=1,sticky='ns')
        horizontal = ttk.Scrollbar(grid,orient='horizontal',command=self.file_table.xview)
        horizontal.grid(row=1,column=0,sticky='ew')
        self.file_table.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        grid.columnconfigure(0,weight=1)
        grid.rowconfigure(0,weight=1)
        self.file_table.tag_configure('error',foreground='#b42318')
        self.file_table.tag_configure('current',background='#e4efff')
        paging = ttk.Frame(fileframe)
        paging.pack(fill='x',pady=(5,0))
        ttk.Button(paging,text='上一页',command=lambda:self.change_page(-1)).pack(side='left')
        self.page_text = tk.StringVar(value='第 1 / 1 页 · 每页 200 项')
        ttk.Label(paging,textvariable=self.page_text).pack(side='left',padx=10)
        ttk.Button(paging,text='下一页',command=lambda:self.change_page(1)).pack(side='left')
        ttk.Label(paging,text='转到页').pack(side='left',padx=(16,5))
        self.page_input = tk.StringVar(value='1')
        ttk.Entry(paging,textvariable=self.page_input,width=7).pack(side='left')
        ttk.Button(paging,text='跳转',command=self.jump_page).pack(side='left',padx=8)
        self.logbox = tk.Text(logframe, height=7, bg='#172336', fg='#d9e6f6', relief='flat', wrap='word',
            font=('Microsoft YaHei UI', 9), padx=12, pady=8, state='disabled')
        self.logbox.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(logframe, command=self.logbox.yview)
        scroll.pack(side='right', fill='y')
        self.logbox.configure(yscrollcommand=scroll.set)
        ttk.Label(main, text='多任务  ·  断点恢复  ·  空间预检  ·  文件过滤  ·  版本与回收记录',
            foreground='#53647a').pack(anchor='w', pady=(9, 3))
        ttk.Label(main, text='文件内容备份；不包含系统镜像、权限/备用数据流或运行中数据库的一致性快照。',
            foreground='#53647a', font=('Microsoft YaHei UI', 9), wraplength=self.wrap).pack(anchor='w')
        self.rebuild_rows()

    def controls(self):
        return (self.add_button, self.edit_button, self.duplicate_button, self.toggle_button,
            self.remove_button, self.scan_button, self.start_button, self.audit_button)

    def save_config(self):
        save_json(STATE/'config.json', self.config)

    def selected(self):
        return list(self.table.selection())

    def one_selected(self):
        keys = self.selected()
        if len(keys) != 1:
            messagebox.showinfo('选择任务', '请先选中一个任务。')
            return None
        return keys[0]

    def rebuild_rows(self, selected=None):
        self.table.delete(*self.table.get_children())
        self.table.configure(height=max(1,min(4,len(self.config['shares']))))
        for key, entry in self.config['shares'].items():
            self.table.insert('', 'end', iid=key, values=('✓' if entry.get('enabled', True) else '—',
                entry.get('label', key), MODES.get(entry.get('mode', 'backup'), '版本备份'),
                entry['source'], entry.get('target') or '待选择', '—', '—', '—', '待分析'))
        if selected and self.table.exists(selected):
            self.table.selection_set(selected)
        elif self.config['shares']:
            ready = next((key for key,entry in self.config['shares'].items() if entry.get('target') and entry.get('enabled',True)),None)
            self.table.selection_set(ready or next(iter(self.config['shares'])))

    def toggle_task_table(self):
        self.tasks_expanded = not self.tasks_expanded
        if self.tasks_expanded:
            self.tasks_frame.pack(fill='x',before=self.details_label)
        else:
            self.tasks_frame.pack_forget()
        self.tasks_toggle.configure(text='收起任务列表' if self.tasks_expanded else '展开任务列表')

    def refresh_saved(self):
        engine = Engine(STATE, self.config)
        try:
            self.update_summary(engine.summary())
        finally:
            engine.close()

    def update_summary(self, values):
        self.latest.update(values)
        for key, value in values.items():
            if not self.table.exists(key):
                continue
            entry = self.config['shares'][key]
            status = f'{value["errors"]} 扫描错误' if value['errors'] else '校验通过' if value.get('audit_ok') else '已分析' if value.get('scan_complete') else '需分析'
            self.table.item(key, values=('✓' if entry.get('enabled', True) else '—', entry.get('label', key),
                MODES.get(entry.get('mode', 'backup')), entry['source'], entry.get('target') or '待选择',
                f'{value["n"]:,}', human(value['b']), f'{value["v"]:,} / {value["n"]:,}', status))

    def show_details(self):
        keys = self.selected()
        if len(keys) == 1:
            entry = self.config['shares'][keys[0]]
            text = entry.get('label',keys[0])+' · '+MODE_HELP.get(entry.get('mode','backup'), '')
            if entry.get('layout') == 'managed':
                text += '  此历史任务的副本位于目标的 files 子目录。'
            text += f'  保留空间：{entry.get("reserve_gib", 1)} GiB。'
            self.details.set(text)
        elif keys:
            self.details.set(f'已选中 {len(keys)} 个任务，可统一分析、执行或完整校验。')
        if hasattr(self,'file_table'):
            self.request_inventory(reset=True,scope_change=not self.busy)

    def inventory_ids(self):
        return self.active_ids if self.busy else self.run_ids()

    def request_inventory(self, reset=False, scope_change=False):
        if self.view_closing:
            return
        if reset:
            self.file_page = 1
        if scope_change and not self.busy:
            self.last_overall = None
            self.operation = None
        self.query_generation += 1
        config = json.loads(json.dumps(self.config))
        states = {'待同步':'pending','全部文件':'all','已完成':'done','失败':'error','镜像回收':'recycle'}
        self.query_pending = (self.query_generation,config,list(self.inventory_ids()),
            states[self.file_filter.get()],self.file_search.get(),self.file_page,
            'audit' if self.operation == 'audit' else 'backup')
        self.last_view_refresh = time.monotonic()
        self.start_query_worker()

    def start_query_worker(self):
        if not self.query_pending or self.view_closing or (self.query_thread and self.query_thread.is_alive()):
            return
        def query():
            while not self.view_closing:
                job = self.query_pending
                self.query_pending = None
                if not job:
                    break
                generation,config,keys,state,search,page,mode = job
                try:
                    result = load_page(STATE/'manifest.sqlite',config,keys,state,search,page,
                        cancelled=lambda:generation != self.query_generation or self.view_closing,mode=mode)
                    if generation == self.query_generation:
                        self.events.put(('inventory',(generation,result)))
                except Exception as e:
                    if generation == self.query_generation and not self.view_closing:
                        self.events.put(('inventory_error',str(e)))
        self.query_thread = threading.Thread(target=query,daemon=True)
        self.query_thread.start()

    def on_destroy(self,event):
        if event.widget == self.root:
            self.view_closing = True
            if getattr(self,'poll_id',None):
                try:
                    self.root.after_cancel(self.poll_id)
                except tk.TclError:
                    pass
                self.poll_id = None
            self.query_generation += 1
            self.query_pending = None
            if self.query_thread and self.query_thread.is_alive():
                self.query_thread.join(timeout=2)

    def change_page(self,delta):
        self.file_page = min(self.file_pages,max(1,self.file_page+delta))
        self.request_inventory()

    def jump_page(self):
        try:
            self.file_page = min(self.file_pages,max(1,int(self.page_input.get())))
        except ValueError:
            return
        self.request_inventory()

    def paint_inventory(self,result):
        selected = self.file_table.selection()
        self.file_table.delete(*self.file_table.get_children())
        actions = {'new':'新增','update':'更新','skip':'无需复制','recycle':'回收','unreadable':'无法读取','unknown':'待分析'}
        states = {'pending':'待同步','done':'已完成','error':'失败','copying':'待继续','verifying':'待继续'}
        for row in result['rows']:
            current = self.busy and self.current_file_value and row['uid'] == 'file:'+str(self.current_file_value.get('id'))
            state = ('校验中' if '校验' in self.current_file_value['phase'] else '复制中') if current else states.get(row['state'],'待同步')
            label = self.config['shares'].get(row['task'],{}).get('label',row['task'] or '清单问题')
            self.file_table.insert('', 'end',iid=row['uid'],values=(label,row['rel'] or '(清单范围)',
                human(row['size']) if row['size'] is not None else '—',actions.get(row['action'],'待分析'),state,row['error']),
                tags=('current',) if current else ('error',) if row['state']=='error' else ())
        if selected and self.file_table.exists(selected[0]):
            self.file_table.selection_set(selected[0])
        self.file_page,self.file_pages = result['page'],result['pages']
        self.page_input.set(str(self.file_page))
        self.page_text.set(f'第 {self.file_page:,} / {self.file_pages:,} 页 · 每页 200 项 · 匹配 {result["count"]:,} 项')
        metrics = result['metrics']
        empty = ' · 当前筛选无记录' if not result['count'] else ''
        self.inventory_text.set(f'已知文件 {metrics["total_files"]:,} 项 · 已完成 {metrics["completed_files"]:,} · 问题 {max(metrics["failed_files"],metrics["issues"]):,}{empty}')
        if not self.busy and self.last_overall is None:
            self.overall_bar.stop()
            self.overall_bar.configure(mode='determinate')
            if not metrics['ready']:
                self.overall_bar['value'] = 0
                self.overall_text.set('总体进度 · 请先设置目标并分析完整清单；当前文件数量可能不完整。')
            else:
                total = metrics['total_bytes']
                percent = (100*metrics['completed_bytes']/total if total else
                    100*metrics['completed_files']/max(1,metrics['total_files']))
                if metrics['completed_files'] != metrics['total_files'] or metrics['issues']:
                    percent = min(99.9,percent)
                self.show_overall(dict(metrics,percent=percent,state='preview',eta_seconds=None,phase='清单'))

    def show_overall(self,value):
        self.overall_bar.stop()
        self.overall_bar.configure(mode='determinate')
        self.overall_bar['value'] = value['percent']
        eta = value.get('eta_seconds')
        eta_text = ''
        if eta is not None:
            minutes = max(1,round(eta/60))
            eta_text = f' · 预计剩余 {minutes//60} 小时 {minutes%60} 分' if minutes >= 60 else f' · 预计剩余 {minutes} 分'
        failures = max(value.get('failed_files',0),value.get('issues',0))
        keys = self.active_ids if self.last_overall is not None else self.inventory_ids()
        names = [self.config['shares'][key].get('label',key) for key in keys if key in self.config['shares']]
        scope = '、'.join(names[:2])+(' 等' if len(names)>2 else '')
        self.overall_text.set(f'总体 {value["percent"]:.1f}%（{scope}） · 完成 {value["completed_files"]:,} / {value["total_files"]:,} 项'
            f' · 已验证 {human(value["completed_bytes"])} / {human(value["total_bytes"])} · 失败 {failures}'
            f' · {value["phase"]}{eta_text}')

    def edit_task(self):
        key = self.one_selected()
        if key:
            self.task_dialog(key)

    def duplicate_task(self):
        key = self.one_selected()
        if key:
            self.task_dialog(copy_from=key)

    def task_dialog(self, editing=None, copy_from=None):
        if self.busy:
            return
        previous = self.config['shares'].get(editing or copy_from, {})
        dialog = tk.Toplevel(self.root)
        dialog.title('编辑任务' if editing else '新建任务')
        dialog.transient(self.root)
        dialog.grab_set()
        tabs = ttk.Notebook(dialog)
        tabs.pack(fill='both', expand=True, padx=16, pady=16)
        general = ttk.Frame(tabs, padding=18)
        advanced = ttk.Frame(tabs, padding=18)
        tabs.add(general, text='任务')
        tabs.add(advanced, text='过滤与传输选项')
        name = tk.StringVar(value=previous.get('label','') + (' 副本' if copy_from else ''))
        source = tk.StringVar(value=previous.get('source',''))
        target = tk.StringVar(value='' if copy_from else previous.get('target',''))
        mode = tk.StringVar(value=MODES.get(previous.get('mode','backup'), '版本备份'))
        enabled = tk.BooleanVar(value=previous.get('enabled', True))
        keep = tk.BooleanVar(value=previous.get('keep_versions', True))
        reserve = tk.StringVar(value=str(previous.get('reserve_gib',1)))
        rate = tk.StringVar(value=str(previous.get('max_mibps',0)))
        retries = tk.StringVar(value=str(previous.get('retry_limit',30)))
        wait = tk.StringVar(value=str(previous.get('retry_seconds',10)))
        def field(parent, row, title, var):
            ttk.Label(parent, text=title).grid(row=row, column=0, sticky='w', pady=(8, 3))
            entry = ttk.Entry(parent, textvariable=var, width=62)
            entry.grid(row=row+1, column=0, sticky='ew', pady=(0, 8))
            return entry
        field(general, 0, '任务名称', name)
        def path_field(row, title, variable):
            field(general, row, title, variable)
            def browse():
                options = {'parent': dialog, 'title': title}
                if variable.get():
                    options['initialdir'] = variable.get()
                chosen = filedialog.askdirectory(**options)
                if chosen:
                    variable.set(chosen)
            ttk.Button(general, text='浏览…', command=browse).grid(row=row+1, column=1, padx=(8,0))
        path_field(2, '来源 · 本机目录 / 移动硬盘 / 网络共享', source)
        path_field(4, '目标 · 可浏览选择，也可填写尚未创建的文件夹', target)
        ttk.Label(general, text='支持 C:\\Documents、E:\\Backup、\\\\server\\share\\folder；新任务直接复制到目标目录。',
            wraplength=round(620*self.scale), foreground='#53647a').grid(row=6, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Label(general, text='模式').grid(row=7, column=0, sticky='w', pady=(12,3))
        combo = ttk.Combobox(general, textvariable=mode, values=list(MODES.values()), state='readonly', width=28)
        combo.grid(row=8, column=0, sticky='w')
        help_text = tk.StringVar()
        ttk.Label(general, textvariable=help_text, wraplength=round(620*self.scale), foreground='#53647a').grid(row=9, column=0, columnspan=2, sticky='w', pady=8)
        def mode_changed(event=None):
            code = next(key for key, value in MODES.items() if value == mode.get())
            help_text.set(MODE_HELP[code])
            if code == 'backup':
                keep.set(True)
        combo.bind('<<ComboboxSelected>>', mode_changed)
        mode_changed()
        ttk.Checkbutton(general, text='启用此任务', variable=enabled).grid(row=10, column=0, sticky='w')
        ttk.Checkbutton(general, text='保留每次替换前的旧版本（版本备份始终开启）', variable=keep).grid(row=11, column=0, columnspan=2, sticky='w', pady=8)
        include = tk.StringVar(value='; '.join(previous.get('include',[])))
        field(advanced, 0, '只包含这些文件 · 通配符用分号分隔；留空包含全部', include)
        exclude = tk.StringVar(value='; '.join(previous.get('exclude',[])))
        field(advanced, 2, '排除文件或目录 · 例如 *.tmp; cache/**; .git/**', exclude)
        field(advanced, 4, '至少保留的目标空闲空间 · GiB（可设为 0）', reserve)
        field(advanced, 6, '复制限速 · MiB/s（0 不限速）', rate)
        field(advanced, 8, '连接 / 文件占用重试次数', retries)
        field(advanced, 10, '两次重试之间的间隔 · 秒', wait)
        note = tk.StringVar(value='新任务只在点击执行后写入目标。来源保持只读。')
        ttk.Label(dialog, textvariable=note, wraplength=round(650*self.scale), foreground='#8a4b00').pack(anchor='w', padx=34, pady=(0,8))
        bottom = ttk.Frame(dialog, padding=(16,0,16,16))
        bottom.pack(fill='x')
        def commit():
            try:
                code = next(key for key, value in MODES.items() if value == mode.get())
                payload = {'label': name.get().strip(), 'source': source.get(), 'target': target.get(),
                    'mode': code, 'enabled': enabled.get(), 'keep_versions': keep.get() or code == 'backup',
                    'layout': previous.get('layout','direct') if editing else 'direct',
                    'reserve_gib': float(reserve.get()), 'max_mibps': float(rate.get()),
                    'retry_limit': int(retries.get()), 'retry_seconds': float(wait.get()),
                    'include': [x.strip() for x in include.get().split(';') if x.strip()],
                    'exclude': [x.strip() for x in exclude.get().split(';') if x.strip()]}
                key = self.save_task(payload, editing)
            except Exception as e:
                note.set(str(e))
                return
            dialog.destroy()
            self.rebuild_rows(key)
            self.refresh_saved()
            self.status.set('任务已保存 · 点击分析查看变化与空间需求。')
        ttk.Button(bottom, text='取消', command=dialog.destroy).pack(side='right')
        ttk.Button(bottom, text='保存任务', style='Primary.TButton', command=commit).pack(side='right', padx=8)
        dialog.bind('<Escape>', lambda e: dialog.destroy())

    def save_task(self, entry, editing=None):
        entry = json.loads(json.dumps(entry))
        entry['source'] = normalize_path(entry['source'])
        if entry.get('target'):
            entry['target'] = normalize_path(entry['target'])
            safe_path(entry['target'])
        if not entry.get('label'):
            entry['label'] = Path(entry['source']).name or entry['source']
        for option in ('reserve_gib','max_mibps','retry_limit','retry_seconds'):
            if not math.isfinite(entry.get(option,0)) or entry.get(option,0) < 0:
                raise ValueError('空间、限速、重试次数和间隔不能为负数')
        if entry.get('retry_limit',30) > 10000:
            raise ValueError('重试次数不得超过 10000')
        if entry.get('mode','backup') not in MODES:
            raise ValueError('未知任务模式')
        if entry.get('mode') == 'backup':
            entry['keep_versions'] = True
        old = self.config['shares'].get(editing, {})
        key = editing or uuid.uuid4().hex
        source_changed = editing and os.path.normcase(os.path.realpath(old['source'])) != os.path.normcase(os.path.realpath(entry['source']))
        if source_changed:
            key = uuid.uuid4().hex
            entry['layout'] = 'direct'
        elif editing:
            entry['source'] = old['source']  # Preserve lexical marker binding for path aliases.
        candidate = json.loads(json.dumps(self.config))
        if source_changed:
            del candidate['shares'][editing]
        candidate['shares'][key] = entry
        engine = Engine(STATE, candidate)
        try:
            engine.validate_configuration()
            if entry.get('target'):
                marker = engine.control_path(key, 'task.json') if not engine.managed(key) else safe_path(entry['target'], '.nasrescue.json')
                if os.path.isfile(marker):
                    with open(marker, encoding='utf-8') as f:
                        info = json.load(f)
                    if info.get('source') != entry['source']:
                        raise ValueError('目标由其他来源使用，请为新来源选一个不同的目标目录。')
            engine.meta(f'task:{key}:audit_ok', '0')
            if old.get('include') != entry.get('include') or old.get('exclude') != entry.get('exclude') or source_changed or not editing:
                engine.meta(f'task:{key}:scan_complete', '0')
        finally:
            engine.close()
        self.config = candidate
        self.save_config()
        return key

    def toggle_tasks(self):
        if self.busy:
            return
        keys = self.selected()
        for key in keys:
            self.config['shares'][key]['enabled'] = not self.config['shares'][key].get('enabled',True)
        self.save_config()
        self.rebuild_rows(keys[0] if keys else None)
        self.refresh_saved()

    def remove_tasks(self):
        if self.busy:
            return
        keys = self.selected()
        for key in keys:
            del self.config['shares'][key]
        self.save_config()
        self.rebuild_rows()
        self.status.set('任务已移除。来源、目标文件和历史记录均保留。')

    def run_ids(self):
        if self.scope.get() == '所有启用任务':
            return [key for key, entry in self.config['shares'].items() if entry.get('enabled',True)]
        return [key for key in self.selected() if self.config['shares'][key].get('enabled',True)]

    def event(self, kind, value):
        if kind == 'file':
            now = time.monotonic()
            if now-self.last_progress < .15:
                return
            self.last_progress = now
        self.events.put((kind,value))

    def run(self, operation):
        if self.busy:
            return
        keys = self.run_ids()
        if not keys:
            messagebox.showinfo('选择任务', '请新建或选中一个启用的任务。')
            return
        if operation in ('backup','audit'):
            missing = [self.config['shares'][key]['label'] for key in keys if not self.config['shares'][key].get('target')]
            if missing:
                messagebox.showinfo('选择目标', '请编辑这些任务并选择目标：\n'+'、'.join(missing))
                return
        self.active_ids = keys
        self.operation = operation
        self.last_overall = None
        self.overall_active = False
        self.current_file_value = None
        config = {'version':2, 'shares': {key:json.loads(json.dumps(self.config['shares'][key])) for key in keys}}
        self.stop = threading.Event()
        self.busy = True
        for button in self.controls():
            button.configure(state='disabled')
        self.scope_box.configure(state='disabled')
        self.pause_button.configure(state='normal')
        self.status.set({'scan':'分析变化与空间需求…','backup':'执行任务…','audit':'完整读取来源与目标，进行校验…'}[operation])
        self.progress['value'] = 0
        self.overall_bar.configure(mode='indeterminate')
        self.overall_bar.start(40)
        self.overall_text.set('总体进度 · 正在扫描并建立执行清单，暂无法计算百分比。')
        self.request_inventory(reset=True)
        def work():
            engine = None
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
                engine = Engine(STATE, config, self.event)
                engine.stop = self.stop
                self.engine = engine
                if operation == 'scan':
                    engine.scan()
                    self.event('capacity',engine.capacity())
                    self.event('done','分析完成 · 查看变化与容量，点击执行。' if not engine.scan_errors() else '分析完成，但存在无法读取的项目，请查看报告。')
                elif operation == 'backup':
                    engine.scan()
                    engine.backup()
                    failed = engine.active_issues()
                    self.event('done',f'本轮执行结束 · {failed} 个问题，请查看报告。' if failed else '执行完成 · 可运行完整校验，或打开目标查看副本。')
                else:
                    passed = engine.audit()
                    self.event('done','完整校验通过。' if passed else '完整校验未通过，请查看报告。')
            except Stopped as e:
                self.event('done',str(e))
            except Exception as e:
                self.event('error',str(e))
                STATE.mkdir(parents=True,exist_ok=True)
                with (STATE/'diagnostics.log').open('a',encoding='utf-8') as f:
                    f.write(traceback.format_exc()+'\n')
            finally:
                if engine:
                    try:
                        engine.export_report()
                        self.event('summary',engine.summary())
                    except Exception:
                        pass
                    engine.close()
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                self.events.put(('idle',None))
        self.worker = threading.Thread(target=work,daemon=False)
        self.worker.start()

    def pause(self):
        self.stop.set()
        self.status.set('正在暂停 · 等待当前网络/磁盘操作返回，续传数据会保留。')
        self.pause_button.configure(state='disabled')

    def show_plan(self, capacity):
        lines = []
        for key, plan in capacity.get('tasks',{}).items():
            label = self.config['shares'][key].get('label',key)
            text = f'{label}：新增 {plan["new"]:,}，更新 {plan["update"]:,}，无需复制 {plan["unchanged"]:,}'
            if self.config['shares'][key].get('mode') == 'mirror':
                text += f'，待回收 {plan["extra"]:,}'
            lines.append(text)
        for path, item in capacity['volumes'].items():
            lines.append(f'{path} 需求 {human(item["needed"])} / 空闲 {human(item["free"])} / 保留 {human(item["reserve"])}：'+('足够' if item['ok'] else '不足'))
        if capacity['unassigned']:
            lines.append('部分任务尚未选目标。')
        self.plan_text.set('  |  '.join(lines))

    def add_log(self,text):
        self.logbox.configure(state='normal')
        self.logbox.insert('end',time.strftime('%H:%M:%S  ')+str(text)+'\n')
        if int(self.logbox.index('end-1c').split('.')[0]) > 500:
            self.logbox.delete('1.0','100.0')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def poll(self):
        try:
            for _ in range(200):
                kind,value = self.events.get_nowait()
                if kind == 'log':
                    self.add_log(value)
                elif kind == 'summary':
                    self.update_summary(value)
                elif kind == 'capacity':
                    self.show_plan(value)
                    self.request_inventory(reset=True)
                elif kind == 'overall':
                    first = not self.overall_active
                    self.overall_active = True
                    self.last_overall = value
                    self.show_overall(value)
                    if first:
                        self.request_inventory(reset=True)
                elif kind == 'planning':
                    label = self.config['shares'][value['share']].get('label',value['share'])
                    self.file_text.set(f'建立执行清单 · {label} / {value["files"]:,} 项')
                elif kind == 'scan':
                    key = value['share']
                    label = self.config['shares'][key].get('label',key)
                    self.file_text.set(f'分析 {label} · {value["share_files"]:,} 个文件 / {human(value["share_bytes"])}')
                    if self.table.exists(key):
                        self.table.set(key,'files',f'{value["share_files"]:,}')
                        self.table.set(key,'size',human(value['share_bytes']))
                        self.table.set(key,'status','分析中')
                elif kind == 'file':
                    previous = self.current_file_value
                    self.current_file_value = value
                    if previous and previous.get('id') != value.get('id'):
                        old = 'file:'+str(previous.get('id'))
                        if self.file_table.exists(old):
                            self.file_table.item(old,tags=())
                    uid = 'file:'+str(value.get('id'))
                    if self.file_table.exists(uid):
                        self.file_table.set(uid,'state','校验中' if '校验' in value['phase'] else '复制中')
                        self.file_table.item(uid,tags=('current',))
                    label = self.config['shares'][value['share']].get('label',value['share'])
                    self.file_text.set(f'{value["phase"]} · {label} / {value["rel"]}')
                    percent = 100*value['done']/max(1,value['size'])
                    self.progress['value'] = percent
                    speed = f' · {human(value["speed"])}/s' if value.get('speed') else ''
                    self.metric.set(f'当前文件 {human(value["done"])} / {human(value["size"])} · {percent:.1f}%{speed}')
                elif kind in ('complete_file','file_failed','recycled_file'):
                    uid = ('mirror:' if kind=='recycled_file' else 'file:')+str(value['id'])
                    if self.file_table.exists(uid):
                        self.file_table.set(uid,'state','失败' if kind=='file_failed' else '已完成')
                        self.file_table.item(uid,tags=('error',) if kind=='file_failed' else ())
                        if kind=='file_failed':
                            self.file_table.set(uid,'error',value['error'])
                    self.current_file_value = None
                    if kind=='complete_file':
                        self.progress['value'] = 100
                        self.metric.set(f'当前文件已复制并校验 · {human(value["bytes"])}')
                elif kind in ('done','error'):
                    self.status.set(value if kind=='done' else '操作停止：'+value)
                    self.add_log(value)
                elif kind == 'idle':
                    self.busy = False
                    self.engine = None
                    for button in self.controls():
                        button.configure(state='normal')
                    self.scope_box.configure(state='readonly')
                    self.pause_button.configure(state='disabled')
                    self.overall_bar.stop()
                    self.overall_bar.configure(mode='determinate')
                    if self.last_overall is not None:
                        self.show_overall(self.last_overall)
                    self.current_file_value = None
                    self.request_inventory()
                    if self.pending_close:
                        self.root.destroy()
                        return
                elif kind == 'inventory':
                    generation,result = value
                    if generation == self.query_generation:
                        self.paint_inventory(result)
                        self.query_applied = generation
                elif kind == 'inventory_error':
                    self.inventory_text.set('清单读取失败：'+value)
        except queue.Empty:
            pass
        reading = self.query_pending or (self.query_thread and self.query_thread.is_alive())
        if self.busy and not reading and time.monotonic()-self.last_view_refresh >= 2:
            self.request_inventory()
        self.start_query_worker()
        self.poll_id = self.root.after(120,self.poll)

    def open_target(self):
        key = self.one_selected()
        if key:
            path = self.config['shares'][key].get('target')
            if path and os.path.isdir(path):
                os.startfile(path)
            else:
                messagebox.showinfo('目标目录','目标尚未选择或尚未创建。')

    def open_reports(self):
        path = BASE/'reports'
        path.mkdir(exist_ok=True)
        os.startfile(path)

    def close(self):
        if self.busy:
            self.pending_close = True
            self.pause()
        else:
            self.root.destroy()


def main():
    STATE.mkdir(parents=True,exist_ok=True)
    lock = open(STATE/'application.lock','a+b')
    import msvcrt
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        ctypes.windll.user32.MessageBoxW(None,'FolderFlow 已在运行，请使用已有窗口。','FolderFlow',0)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    lock.close()


if __name__ == '__main__':
    main()
