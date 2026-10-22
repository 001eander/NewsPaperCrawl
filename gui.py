#!/usr/bin/env python3
"""
浙图报纸爬虫 - 桌面 GUI
=======================

tkinter 薄壳入口,与 cli.py 平级:装配仍由 crawler.bootstrap.run_stage 完成,
本文件只做三件事:

  - 运行页:登录按钮(子进程跑 scripts/login.py)+ 四阶段
    (import/issues/boards/articles)参数输入 + 运行/停止
  - 状态页:三表(issues/boards/articles)pending/done/failed 计数与进度
  - 日志页:爬虫日志实时回显(QueueHandler + after 轮询)
  - 配置页:登录凭据(credentials.json)与 Bark 推送(bark.json)的编辑保存

线程模型:tkinter mainloop 在 GUI 线程;每次运行在 worker 线程里
asyncio.run 等价流程(手动建 loop 以便"停止"按钮跨线程 cancel 主任务)。
事件一律经 queue.Queue 回 GUI 线程,worker 不直接碰 tk 控件。

用法:
  uv run gui.py
"""

import asyncio
import json
import logging
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from crawler.adapters.bark import BarkNotifier
from crawler.adapters.config import Settings
from crawler.adapters.instance_lock import AlreadyRunningError
from crawler.bootstrap import run_stage
from crawler.domain.exceptions import AuthError
from crawler.logging_config import setup_logging

logger = logging.getLogger(__name__)

STAGE_TABLES = ("issues", "boards", "articles")
STATUS_ORDER = ("pending", "done", "failed")
LOG_MAX_LINES = 5000
POLL_MS = 200
AUTO_REFRESH_MS = 5000
# 登录辅助脚本(同步 Playwright,弹真实浏览器),以子进程方式启动
LOGIN_SCRIPT = Path(__file__).resolve().parent / "scripts" / "login.py"


# ---------------------------------------------------------------- 状态查询


class StatusQueries:
    """
    状态页只读查询(stdlib sqlite3)。

    与 scripts/status.py 相同的 SQL 思路,但脚本全是 print 函数不可复用,
    这里返回数据给 Treeview。DB 为 WAL 模式,爬虫写入时可并发读;
    查询都是毫秒级 GROUP BY,直接在 GUI 线程执行。
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None

    def overview(self) -> list[dict[str, Any]]:
        """每表一行:total/done/pending/failed;表未建时 exists=False"""
        if not self._db_path.exists():
            return []
        rows = []
        conn = sqlite3.connect(str(self._db_path))
        try:
            for table in STAGE_TABLES:
                if not self._table_exists(conn, table):
                    rows.append({"stage": table, "exists": False})
                    continue
                counts = dict.fromkeys(STATUS_ORDER, 0)
                cur = conn.execute(
                    f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status"
                )
                for status, n in cur.fetchall():
                    # processing 是运行中的临时认领,统计上并入 pending
                    counts["pending" if status == "processing" else status] += n
                rows.append(
                    {
                        "stage": table,
                        "exists": True,
                        "total": sum(counts.values()),
                        **counts,
                    }
                )
        finally:
            conn.close()
        return rows

    def per_paper(self) -> list[tuple[str, str, list[str]]]:
        """按报纸汇总:(paperid, name, [issues done/total, boards, articles])"""
        if not self._db_path.exists():
            return []
        conn = sqlite3.connect(str(self._db_path))
        try:
            if not self._table_exists(conn, "papers"):
                return []
            papers = conn.execute(
                "SELECT paperid, name FROM papers ORDER BY paperid"
            ).fetchall()
            per: dict[str, dict[str, dict[str, int]]] = {}
            for table in STAGE_TABLES:
                if not self._table_exists(conn, table):
                    continue
                cur = conn.execute(
                    f"SELECT paperid, status, COUNT(*) AS n FROM {table} "
                    "GROUP BY paperid, status"
                )
                for paperid, status, n in cur.fetchall():
                    bucket = per.setdefault(paperid, {}).setdefault(
                        table, dict.fromkeys(STATUS_ORDER, 0)
                    )
                    bucket["pending" if status == "processing" else status] += n
        finally:
            conn.close()
        rows = []
        for paperid, name in papers:
            cells = []
            for table in STAGE_TABLES:
                tb = per.get(paperid, {}).get(table)
                cells.append("--" if tb is None else f"{tb['done']}/{sum(tb.values())}")
            rows.append((paperid, name, cells))
        return rows

    def failed(self, limit: int = 5) -> dict[str, list[tuple[str, str, str]]]:
        """每表最近 limit 条失败:{table: [(paperid, date, error)]}"""
        result: dict[str, list[tuple[str, str, str]]] = {}
        if not self._db_path.exists():
            return result
        conn = sqlite3.connect(str(self._db_path))
        try:
            for table in STAGE_TABLES:
                if not self._table_exists(conn, table):
                    continue
                cur = conn.execute(
                    f"SELECT paperid, date, error FROM {table} "
                    "WHERE status='failed' ORDER BY date DESC, paperid LIMIT ?",
                    (limit,),
                )
                rows = cur.fetchall()
                if rows:
                    result[table] = [
                        (pid, date, (err or "").replace("\n", " "))
                        for pid, date, err in rows
                    ]
        finally:
            conn.close()
        return result


# ---------------------------------------------------------------- 后台运行


class QueueLogHandler(logging.Handler):
    """把日志记录格式化成字符串塞进事件队列,由 GUI 线程取出显示"""

    def __init__(self, events: queue.Queue):
        super().__init__()
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        self._events.put(("log", self.format(record)))


class CrawlRunner:
    """
    worker 线程封装:启动 run_stage / 跨线程停止 / 结果回报。

    手动建事件循环(而非 asyncio.run)是为了持有 loop 引用,
    "停止"时 call_soon_threadsafe(task.cancel);CancelledError 会穿过
    run_stage 的 finally(repo 正常关闭),不触发异常 Bark 通知。
    """

    def __init__(self, events: queue.Queue):
        self._events = events
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, stage: str, kwargs: dict[str, Any]) -> bool:
        if self.running:
            return False
        self._thread = threading.Thread(
            target=self._run, args=(stage, kwargs), daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)

    def _run(self, stage: str, kwargs: dict[str, Any]) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._task = loop.create_task(run_stage(stage, **kwargs))
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            outcome, message = "stopped", "已被用户停止"
        except AlreadyRunningError as e:
            outcome, message = "busy", str(e)
        except AuthError as e:
            outcome, message = "auth", str(e)
        except Exception as e:  # noqa: BLE001
            outcome, message = "error", str(e) or type(e).__name__
        else:
            outcome, message = "done", ""
        finally:
            loop.close()
            self._loop = None
            self._task = None
        self._events.put(("done", stage, outcome, message))


# ---------------------------------------------------------------- 界面


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("浙图报纸爬虫")
        self.geometry("980x700")
        self.minsize(760, 520)

        self._settings = Settings.from_env()
        self._queries = StatusQueries(self._settings.db_path)
        self._events: queue.Queue = queue.Queue()
        self._runner = CrawlRunner(self._events)
        self._login_proc: subprocess.Popen | None = None

        handler = QueueLogHandler(self._events)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S")
        )
        logging.getLogger().addHandler(handler)

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        run_tab = ttk.Frame(notebook)
        status_tab = ttk.Frame(notebook)
        log_tab = ttk.Frame(notebook)
        settings_tab = ttk.Frame(notebook)
        notebook.add(run_tab, text="运行")
        notebook.add(status_tab, text="状态")
        notebook.add(log_tab, text="日志")
        notebook.add(settings_tab, text="配置")

        self._run_buttons: list[ttk.Button] = []
        self._build_run_tab(run_tab)
        self._build_status_tab(status_tab)
        self._build_log_tab(log_tab)
        self._build_settings_tab(settings_tab)
        self._load_secrets()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll_events)
        self.after(0, self._refresh_status)

    # ---- 运行页 ----

    def _build_run_tab(self, parent: ttk.Frame) -> None:
        canvas_frame = ttk.Frame(parent, padding=8)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        # 登录:认证失效时重新获取 cookie(子进程跑 scripts/login.py,
        # 自动完成账号/验证码/提交;输出实时进日志页)
        fl = ttk.LabelFrame(canvas_frame, text="登录 - 重新获取 cookie", padding=6)
        fl.pack(fill=tk.X, pady=4)
        ttk.Label(
            fl,
            text="认证失效时点击右侧按钮,脚本会自动登录并刷新 cookie",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=2)
        self._login_button = ttk.Button(fl, text="登录", command=self._on_login)
        self._login_button.grid(row=0, column=2, padx=4, pady=2)

        # 阶段0: 导入报纸清单
        f0 = ttk.LabelFrame(canvas_frame, text="阶段0 import - 导入报纸清单", padding=6)
        f0.pack(fill=tk.X, pady=4)
        self._csv_var = tk.StringVar()
        self._labeled_entry(f0, 0, "CSV 路径(留空用默认)", self._csv_var, width=46)
        ttk.Button(f0, text="浏览…", command=self._browse_csv).grid(
            row=0, column=2, padx=4
        )
        self._stage_button(f0, 0, "import", self._collect_import)

        # 阶段1: 枚举期次
        f1 = ttk.LabelFrame(canvas_frame, text="阶段1 issues - 枚举期次", padding=6)
        f1.pack(fill=tk.X, pady=4)
        self._start_year_var = tk.StringVar(value="2014")
        self._end_year_var = tk.StringVar(value="2026")
        self._start_month_var = tk.StringVar()
        self._end_month_var = tk.StringVar()
        self._issues_conc_var = tk.StringVar()
        self._labeled_entry(f1, 0, "起始年", self._start_year_var)
        self._labeled_entry(f1, 1, "结束年", self._end_year_var)
        self._labeled_entry(f1, 2, "开始月份(可空)", self._start_month_var)
        self._labeled_entry(f1, 3, "结束月份(可空)", self._end_month_var)
        self._labeled_entry(f1, 4, "并发(可空)", self._issues_conc_var)
        self._stage_button(f1, 5, "issues", self._collect_issues)

        # 阶段2: 抓版面
        f2 = ttk.LabelFrame(canvas_frame, text="阶段2 boards - 抓版面", padding=6)
        f2.pack(fill=tk.X, pady=4)
        self._paperid_var = tk.StringVar()
        self._boards_limit_var = tk.StringVar()
        self._boards_conc_var = tk.StringVar()
        self._labeled_entry(f2, 0, "paperid(可空)", self._paperid_var, width=22)
        self._labeled_entry(f2, 1, "limit(可空)", self._boards_limit_var)
        self._labeled_entry(f2, 2, "并发(可空)", self._boards_conc_var)
        self._stage_button(f2, 3, "boards", self._collect_boards)

        # 阶段3: 抓报道
        f3 = ttk.LabelFrame(canvas_frame, text="阶段3 articles - 抓报道", padding=6)
        f3.pack(fill=tk.X, pady=4)
        self._articles_limit_var = tk.StringVar()
        self._articles_conc_var = tk.StringVar()
        self._labeled_entry(f3, 0, "limit(可空)", self._articles_limit_var)
        self._labeled_entry(f3, 1, "并发(可空)", self._articles_conc_var)
        self._stage_button(f3, 2, "articles", self._collect_articles)

        # 底部:停止按钮 + 当前任务状态
        bottom = ttk.Frame(canvas_frame)
        bottom.pack(fill=tk.X, pady=(10, 2))
        self._stop_button = ttk.Button(
            bottom, text="停止当前任务", command=self._on_stop, state=tk.DISABLED
        )
        self._stop_button.pack(side=tk.LEFT)
        self._run_state_var = tk.StringVar(value="空闲")
        ttk.Label(bottom, textvariable=self._run_state_var).pack(side=tk.LEFT, padx=12)

    @staticmethod
    def _labeled_entry(
        frame: ttk.LabelFrame, row: int, label: str, var: tk.StringVar, width: int = 12
    ) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, pady=2)
        ttk.Entry(frame, textvariable=var, width=width).grid(
            row=row, column=1, sticky=tk.W, padx=(6, 4), pady=2
        )

    def _stage_button(
        self,
        frame: ttk.LabelFrame,
        row: int,
        stage: str,
        collect,
    ) -> None:
        btn = ttk.Button(
            frame, text=f"运行 {stage}", command=lambda: self._on_run(stage, collect)
        )
        btn.grid(row=row, column=2, padx=4, pady=2)
        self._run_buttons.append(btn)

    def _browse_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="选择报纸清单 CSV",
            initialdir=self._settings.paperlist_csv.parent,
            filetypes=[("CSV 文件", "*.csv")],
        )
        if path:
            self._csv_var.set(path)

    # ---- 参数收集(返回 None 表示校验失败,已弹窗) ----

    @staticmethod
    def _opt_int(var: tk.StringVar, name: str) -> int | None:
        text = var.get().strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{name} 必须是整数") from None

    def _collect_import(self) -> dict[str, Any]:
        csv = self._csv_var.get().strip()
        return {"csv": csv} if csv else {}

    def _collect_issues(self) -> dict[str, Any]:
        start = self._opt_int(self._start_year_var, "起始年")
        end = self._opt_int(self._end_year_var, "结束年")
        if start is None or end is None:
            raise ValueError("起始年/结束年必填")
        kwargs: dict[str, Any] = {"start_year": start, "end_year": end}
        if (m := self._opt_int(self._start_month_var, "开始月份")) is not None:
            kwargs["start_month"] = m
        if (m := self._opt_int(self._end_month_var, "结束月份")) is not None:
            kwargs["end_month"] = m
        if (c := self._opt_int(self._issues_conc_var, "并发")) is not None:
            kwargs["concurrency"] = c
        return kwargs

    def _collect_boards(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if pid := self._paperid_var.get().strip():
            kwargs["paperid"] = pid
        if (n := self._opt_int(self._boards_limit_var, "limit")) is not None:
            kwargs["limit"] = n
        if (c := self._opt_int(self._boards_conc_var, "并发")) is not None:
            kwargs["concurrency"] = c
        return kwargs

    def _collect_articles(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if (n := self._opt_int(self._articles_limit_var, "limit")) is not None:
            kwargs["limit"] = n
        if (c := self._opt_int(self._articles_conc_var, "并发")) is not None:
            kwargs["concurrency"] = c
        return kwargs

    # ---- 运行/停止/收尾 ----

    def _on_run(self, stage: str, collect) -> None:
        if self._runner.running:
            messagebox.showinfo("提示", "已有任务在运行,请先停止或等待完成")
            return
        try:
            kwargs = collect()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return
        if not self._runner.start(stage, kwargs):
            return
        logger.info("GUI 启动阶段: %s %s", stage, kwargs)
        for btn in self._run_buttons:
            btn.config(state=tk.DISABLED)
        self._stop_button.config(state=tk.NORMAL)
        self._run_state_var.set(f"正在运行 {stage}…")

    def _on_stop(self) -> None:
        self._runner.stop()
        self._stop_button.config(state=tk.DISABLED)
        self._run_state_var.set("正在停止(等待当前请求收尾)…")

    def _on_stage_done(self, stage: str, outcome: str, message: str) -> None:
        for btn in self._run_buttons:
            btn.config(state=tk.NORMAL)
        self._stop_button.config(state=tk.DISABLED)
        self._run_state_var.set("空闲")
        if outcome == "auth":
            messagebox.showwarning(
                "需要重新登录",
                f"登录状态失效: {message}\n\n"
                "请点击运行页顶部的【登录】按钮重新登录,完成后再运行阶段。",
            )
        elif outcome == "busy":
            messagebox.showinfo("已有实例", message)
        elif outcome == "error":
            messagebox.showerror("运行出错", f"阶段 {stage} 异常中断:\n{message}")
        self._refresh_status()

    # ---- 登录(子进程跑 scripts/login.py) ----

    def _on_login(self) -> None:
        if self._login_proc is not None and self._login_proc.poll() is None:
            messagebox.showinfo("提示", "登录脚本已在运行,请稍候")
            return
        try:
            # 子进程 stdout 是管道时会回落到 GBK(终端里则用控制台 Unicode
            # API,所以手动跑没事);强制 UTF-8,否则中文输出触发
            # UnicodeEncodeError,且 GUI 端解码乱码
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            self._login_proc = subprocess.Popen(
                [sys.executable, str(LOGIN_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=LOGIN_SCRIPT.parent.parent,
                env=env,
            )
        except OSError as e:
            messagebox.showerror("启动失败", f"无法启动登录脚本:\n{e}")
            return
        logger.info("GUI 启动登录脚本: %s", LOGIN_SCRIPT)
        self._login_button.config(state=tk.DISABLED)
        threading.Thread(target=self._pump_login_output, daemon=True).start()

    def _pump_login_output(self) -> None:
        """读子进程输出行进日志队列,进程退出后回报退出码"""
        proc = self._login_proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self._events.put(("log", f"login | {line.rstrip()}"))
        self._events.put(("login_done", proc.wait()))

    def _on_login_done(self, returncode: int) -> None:
        self._login_button.config(state=tk.NORMAL)
        if returncode == 0:
            messagebox.showinfo("登录完成", "cookie 已保存,可以继续运行爬取阶段。")
        else:
            messagebox.showerror(
                "登录失败",
                f"登录脚本以退出码 {returncode} 结束,失败原因见日志页。",
            )

    # ---- 状态页 ----

    def _build_status_tab(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent, padding=(8, 8, 8, 0))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="刷新", command=self._refresh_status).pack(
            side=tk.LEFT
        )
        self._auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="自动刷新(5s)", variable=self._auto_var).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(toolbar, text="最近失败", command=self._show_failed).pack(
            side=tk.LEFT
        )
        self._db_var = tk.StringVar(value=f"DB: {self._settings.db_path}")
        ttk.Label(toolbar, textvariable=self._db_var, foreground="#666").pack(
            side=tk.RIGHT
        )

        ov = ttk.LabelFrame(parent, text="阶段总览", padding=4)
        ov.pack(fill=tk.X, padx=8, pady=6)
        cols = ("stage", "total", "done", "pending", "failed", "progress")
        self._ov_tree = ttk.Treeview(ov, columns=cols, show="headings", height=3)
        for col, text, w in zip(
            cols,
            ("阶段", "总数", "完成", "待处理", "失败", "进度"),
            (110, 110, 110, 110, 90, 110),
        ):
            self._ov_tree.heading(col, text=text)
            self._ov_tree.column(col, width=w, anchor=tk.CENTER)
        self._ov_tree.pack(fill=tk.X)

        pp = ttk.LabelFrame(parent, text="按报纸(done/total)", padding=4)
        pp.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        pcols = ("paperid", "name", "issues", "boards", "articles")
        self._pp_tree = ttk.Treeview(pp, columns=pcols, show="headings")
        for col, text, w, anchor in zip(
            pcols,
            ("paperid", "报纸名", "issues", "boards", "articles"),
            (170, 220, 120, 120, 120),
            (tk.W, tk.W, tk.CENTER, tk.CENTER, tk.CENTER),
        ):
            self._pp_tree.heading(col, text=text)
            self._pp_tree.column(col, width=w, anchor=anchor)
        vsb = ttk.Scrollbar(pp, orient=tk.VERTICAL, command=self._pp_tree.yview)
        self._pp_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._pp_tree.pack(fill=tk.BOTH, expand=True)

    def _refresh_status(self) -> None:
        try:
            overview = self._queries.overview()
            per_paper = self._queries.per_paper()
        except sqlite3.Error as e:
            logger.warning("状态查询失败: %s", e)
            return
        for item in self._ov_tree.get_children():
            self._ov_tree.delete(item)
        for row in overview:
            if not row.get("exists"):
                self._ov_tree.insert(
                    "", tk.END, values=(row["stage"], "未建表", "", "", "", "")
                )
                continue
            total, done = row["total"], row["done"]
            pct = f"{done / total * 100:.1f}%" if total else "-"
            self._ov_tree.insert(
                "",
                tk.END,
                values=(row["stage"], total, done, row["pending"], row["failed"], pct),
            )
        for item in self._pp_tree.get_children():
            self._pp_tree.delete(item)
        for paperid, name, cells in per_paper:
            self._pp_tree.insert("", tk.END, values=(paperid, name, *cells))

    def _auto_refresh(self) -> None:
        if self._auto_var.get():
            self._refresh_status()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _show_failed(self) -> None:
        failed = self._queries.failed(limit=5)
        win = tk.Toplevel(self)
        win.title("最近失败(每表最多 5 条)")
        win.geometry("760x360")
        text = scrolledtext.ScrolledText(win, wrap=tk.NONE, font=("Consolas", 9))
        text.pack(fill=tk.BOTH, expand=True)
        if not failed:
            text.insert(tk.END, "没有失败记录\n")
        for table, rows in failed.items():
            text.insert(tk.END, f"[{table}]\n")
            for paperid, date, err in rows:
                text.insert(tk.END, f"  {paperid} {date}  {err[:120]}\n")
            text.insert(tk.END, "\n")
        text.config(state=tk.DISABLED)

    # ---- 日志页 ----

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent, padding=(8, 8, 8, 0))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="清空", command=self._clear_log).pack(side=tk.LEFT)
        self._log_text = scrolledtext.ScrolledText(
            parent, wrap=tk.NONE, state=tk.DISABLED, font=("Consolas", 9)
        )
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _append_log(self, line: str) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, line + "\n")
        # 控制行数,避免长跑把 Text 撑爆
        if int(self._log_text.index("end-1c").split(".")[0]) > LOG_MAX_LINES:
            self._log_text.delete("1.0", f"{LOG_MAX_LINES // 5}.0")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    # ---- 配置页(登录凭据 / Bark 推送) ----

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)

        fc = ttk.LabelFrame(
            frame, text="登录凭据(.secrets/credentials.json)", padding=6
        )
        fc.pack(fill=tk.X, pady=4)
        self._cred_user_var = tk.StringVar()
        self._cred_pass_var = tk.StringVar()
        self._labeled_entry(fc, 0, "账号", self._cred_user_var, width=30)
        ttk.Label(fc, text="密码").grid(row=1, column=0, sticky=tk.W, pady=2)
        self._cred_pass_entry = ttk.Entry(
            fc, textvariable=self._cred_pass_var, width=32, show="•"
        )
        self._cred_pass_entry.grid(row=1, column=1, sticky=tk.W, padx=(6, 4), pady=2)
        self._show_pass_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            fc,
            text="显示密码",
            variable=self._show_pass_var,
            command=self._toggle_password,
        ).grid(row=1, column=2, sticky=tk.W, padx=4)
        ttk.Label(
            fc,
            text="登录脚本读取本文件自动填表;保存后下次登录生效",
            foreground="#666",
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=2)
        ttk.Button(fc, text="保存凭据", command=self._save_credentials).grid(
            row=2, column=2, padx=4, pady=2
        )

        fb = ttk.LabelFrame(frame, text="Bark 推送(.secrets/bark.json)", padding=6)
        fb.pack(fill=tk.X, pady=4)
        self._bark_key_var = tk.StringVar()
        self._bark_server_var = tk.StringVar(value="https://api.day.app")
        self._labeled_entry(fb, 0, "device key", self._bark_key_var, width=30)
        self._labeled_entry(fb, 1, "服务器", self._bark_server_var, width=30)
        ttk.Label(
            fb,
            text="认证失效/运行结束时推送到 iOS;device key 留空即关闭推送",
            foreground="#666",
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=2)
        ttk.Button(fb, text="发送测试推送", command=self._test_bark).grid(
            row=3, column=0, sticky=tk.W, pady=2
        )
        ttk.Button(fb, text="保存 Bark", command=lambda: self._save_bark()).grid(
            row=3, column=2, padx=4, pady=2
        )

    def _toggle_password(self) -> None:
        self._cred_pass_entry.config(show="" if self._show_pass_var.get() else "•")

    def _load_secrets(self) -> None:
        """启动时把已有 .secrets 配置回填到输入框(模板占位符视为未填)"""
        cred_path = self._settings.secrets_dir / "credentials.json"
        try:
            if cred_path.exists():
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                username = data.get("username", "")
                password = data.get("password", "")
                if "在此填入" not in username:
                    self._cred_user_var.set(username)
                if "在此填入" not in password:
                    self._cred_pass_var.set(password)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("credentials.json 读取失败: %s", e)
        bark_path = self._settings.secrets_dir / "bark.json"
        try:
            if bark_path.exists():
                data = json.loads(bark_path.read_text(encoding="utf-8"))
                key = data.get("device_key", "")
                if "在此填入" not in key:
                    self._bark_key_var.set(key)
                if server := data.get("bark_server", ""):
                    self._bark_server_var.set(server)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("bark.json 读取失败: %s", e)

    def _save_json(self, path: Path, payload: dict) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            messagebox.showerror("保存失败", f"写入 {path} 失败:\n{e}")
            return False
        logger.info("已保存配置: %s", path)
        return True

    def _save_credentials(self) -> None:
        username = self._cred_user_var.get().strip()
        password = self._cred_pass_var.get().strip()
        if not username or not password:
            messagebox.showerror("参数错误", "账号和密码都不能为空")
            return
        payload = {
            "username": username,
            "password": password,
            "_说明": "登录辅助脚本(login.py)读取本文件自动填表。"
            "认证过期时爬虫会通知重新登录,重新执行登录脚本即可。",
        }
        if self._save_json(self._settings.secrets_dir / "credentials.json", payload):
            messagebox.showinfo("已保存", "登录凭据已保存,下次登录时生效。")

    def _save_bark(self, quiet: bool = False) -> bool:
        payload = {
            "device_key": self._bark_key_var.get().strip(),
            "bark_server": self._bark_server_var.get().strip() or "https://api.day.app",
            "_说明": "Bark 是 iOS 推送。device key 填入后,"
            "爬虫会在认证过期等事件时推送通知。",
        }
        if not self._save_json(self._settings.secrets_dir / "bark.json", payload):
            return False
        if not quiet:
            messagebox.showinfo("已保存", "Bark 配置已保存,下次运行时生效。")
        return True

    def _test_bark(self) -> None:
        if not self._bark_key_var.get().strip():
            messagebox.showwarning("未配置", "device key 为空,无法推送。")
            return
        # 先静默落盘,让测试推送用的就是界面上这份配置
        if not self._save_bark(quiet=True):
            return

        def work() -> None:
            notifier = BarkNotifier(self._settings.secrets_dir / "bark.json")
            try:
                asyncio.run(notifier.notify("浙图爬虫测试", "Bark 配置生效"))
            except Exception as e:  # noqa: BLE001
                logger.warning("Bark 测试推送异常: %s", e)
            self._events.put(("bark_test_done", notifier.is_configured))

        threading.Thread(target=work, daemon=True).start()

    def _on_bark_test_done(self, configured: bool) -> None:
        if configured:
            messagebox.showinfo(
                "测试推送", "已发送测试推送,请查看手机。\n若未收到,失败原因见日志页。"
            )
        else:
            messagebox.showwarning("测试推送", "device key 无效,推送被跳过。")

    # ---- 事件泵 / 关闭 ----

    def _poll_events(self) -> None:
        try:
            while True:
                item = self._events.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1])
                elif item[0] == "login_done":
                    self._on_login_done(item[1])
                elif item[0] == "bark_test_done":
                    self._on_bark_test_done(item[1])
                else:
                    _, stage, outcome, message = item
                    self._on_stage_done(stage, outcome, message)
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll_events)

    def _on_close(self) -> None:
        if self._runner.running and not messagebox.askokcancel(
            "任务仍在运行", "爬虫任务仍在运行,退出将中断本次运行。确定退出?"
        ):
            return
        self._runner.stop()
        self.destroy()


def main() -> None:
    # 先配置日志(控制台+文件),再让 App 往 root 挂 QueueHandler —
    # setup_logging 见 root 已有 handler 会跳过文件通道,顺序不能反
    setup_logging()
    app = App()
    app.after(AUTO_REFRESH_MS, app._auto_refresh)
    app.mainloop()


if __name__ == "__main__":
    main()
