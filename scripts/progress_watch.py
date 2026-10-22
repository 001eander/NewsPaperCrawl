#!/usr/bin/env python3
"""
进度看板:每分钟采样数据库,追加 CSV,重绘趋势图
================================================
while 循环每隔 --interval 秒(默认 60)读取 issues / boards / articles 的
done 与总数,把样本追加到 CSV,并按最近 --window 分钟的增量计算处理速度与
预计剩余时间(ETA),随后覆盖重绘 PNG 趋势图。

用法:
  uv run scripts/progress_watch.py                # 每 60 秒采样,持续运行
  uv run scripts/progress_watch.py --once         # 只采样一次并绘图(自检)
  uv run scripts/progress_watch.py --interval 30 --window 60
  uv run scripts/progress_watch.py --csv logs/progress.csv --png logs/progress.png

只读数据库,可与爬虫并发执行。CSV 时间戳统一为 UTC+8,按时间追加;重启脚本
会读取已有历史继续计算速度。PNG 每次整体重绘,可放在预览窗口里持续观察。
"""

import argparse
import csv
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import matplotlib

matplotlib.use("Agg")  # 无显示环境也能出图
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

# 项目未安装为包,直接运行时 sys.path[0] 是 scripts/,需手动加入项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.adapters.config import Settings

DB_PATH = Settings.from_env().db_path

STAGES = ("issues", "boards", "articles")
TIME_FMT = "%Y-%m-%d %H:%M:%S"
# CSV 统一存 UTC+8 时间(不含时区后缀),内存里用带时区的时间以便比较
TZ_UTC8 = timezone(timedelta(hours=8))
FIELDS = ["timestamp"] + [
    f"{stage}_{name}"
    for stage in STAGES
    for name in ("done", "total", "pending", "rate", "eta_min")
]


class Snap(NamedTuple):
    done: int
    total: int
    pending: int


# 图只画最近的点,避免长跑后 PNG 里挤满像素
MAX_PLOT_POINTS = 1500


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def sample(con: sqlite3.Connection) -> dict[str, Snap]:
    """返回各阶段计数;表尚未创建时按 0 处理。failed 不计入 pending。"""
    result: dict[str, Snap] = {}
    for stage in STAGES:
        try:
            rows = con.execute(
                f"SELECT status, COUNT(*) FROM {stage} GROUP BY status"
            ).fetchall()
        except sqlite3.OperationalError:
            result[stage] = Snap(0, 0, 0)
            continue
        counts = dict(rows)
        result[stage] = Snap(
            done=counts.get("done", 0),
            total=sum(counts.values()),
            # processing 是运行中的临时认领,仍算待处理
            pending=counts.get("pending", 0) + counts.get("processing", 0),
        )
    return result


def load_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if row.get("timestamp")]


def append_row(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def compute_rates(
    window_samples: deque[tuple[datetime, dict[str, Snap]]],
) -> dict[str, tuple[float, float | None]]:
    """用窗口首尾差值算每分钟速度与 ETA(分钟)。

    pending=0 时 ETA 记 0(已完成);速度<=0 且有 pending 时 ETA 为 None(停滞)。
    """
    first_ts, first = window_samples[0]
    last_ts, last = window_samples[-1]
    elapsed_min = (last_ts - first_ts).total_seconds() / 60
    out: dict[str, tuple[float, float | None]] = {}
    for stage in STAGES:
        done_delta = last[stage].done - first[stage].done
        rate = max(done_delta, 0) / elapsed_min if elapsed_min > 0 else 0.0
        pending = last[stage].pending
        if pending == 0:
            out[stage] = (rate, 0.0)
        else:
            out[stage] = (rate, pending / rate if rate > 0 else None)
    return out


def format_eta(eta_min: float | None) -> str:
    if eta_min is None:
        return "stalled"
    if eta_min <= 0:
        return "done"
    total = round(eta_min)
    days, rem = divmod(total, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def plot(
    rows: list[dict[str, str]], rates: dict[str, tuple[float, float | None]], png: Path
) -> None:
    """按 CSV 历史重绘三阶段 done 曲线与速度曲线;标题标注当前速度与 ETA。"""
    if len(rows) > MAX_PLOT_POINTS:
        step = len(rows) / MAX_PLOT_POINTS
        rows = [rows[int(i * step)] for i in range(MAX_PLOT_POINTS - 1)] + [rows[-1]]

    times = [
        datetime.strptime(r["timestamp"], TIME_FMT).replace(tzinfo=TZ_UTC8)
        for r in rows
    ]
    fig, axes = plt.subplots(len(STAGES) + 1, 1, figsize=(10, 9), sharex=True)
    for ax, stage, color in zip(axes, STAGES, ("#1f77b4", "#2ca02c", "#d62728")):
        done = [int(r[f"{stage}_done"]) for r in rows]
        total = [int(r[f"{stage}_total"]) for r in rows]
        pct = [d / t * 100 if t else 0 for d, t in zip(done, total)]
        rate, eta = rates[stage]
        pending = int(rows[-1][f"{stage}_pending"])
        ax.plot(times, pct, color=color, linewidth=1.6)
        ax.set_ylabel("% done", fontsize=8)
        # 数据在 62% 附近小幅上升时,默认会改用 +6.2e1 偏移量,读起来别扭
        ax.ticklabel_format(axis="y", useOffset=False)
        ax.set_title(
            f"{stage}: {done[-1]}/{rows[-1][f'{stage}_total']} done, "
            f"{pending} pending  | {rate:.1f}/min | ETA {format_eta(eta)}",
            loc="left",
            fontsize=10,
        )
        ax.grid(alpha=0.3)
        ax.margins(x=0.01)

    for stage, color in zip(STAGES, ("#1f77b4", "#2ca02c", "#d62728")):
        axes[-1].plot(
            times,
            [float(r[f"{stage}_rate"]) for r in rows],
            color=color,
            linewidth=1.2,
            label=stage,
        )
    axes[-1].set_title(
        "throughput (items/min, window average)", loc="left", fontsize=10
    )
    axes[-1].grid(alpha=0.3)
    axes[-1].legend(loc="upper left", fontsize=8)
    axes[-1].margins(x=0.01)

    # 采样跨度短时按秒显示,避免刻度只剩重复的分钟
    span = (times[-1] - times[0]).total_seconds()
    axes[-1].xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M:%S" if span < 7200 else "%m-%d %H:%M")
    )
    fig.autofmt_xdate(rotation=30, ha="right")
    fig.suptitle(f"crawl progress - updated {times[-1]:%Y-%m-%d %H:%M:%S}", fontsize=12)
    fig.tight_layout()
    png.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换,避免预览窗口读到写了一半的图
    tmp = png.with_name(png.name + ".tmp")
    fig.savefig(tmp, dpi=110, format="png")
    plt.close(fig)
    try:
        tmp.replace(png)
    except OSError:
        # Windows 上预览窗口可能锁住 PNG;保留 .tmp,下一轮再覆盖
        print(f"warning: cannot replace {png}, kept {tmp}", flush=True)


def print_sample(
    ts: datetime,
    snap: dict[str, Snap],
    rates: dict[str, tuple[float, float | None]],
) -> None:
    parts = []
    for stage in STAGES:
        s = snap[stage]
        rate, eta = rates[stage]
        parts.append(
            f"{stage} {s.done}/{s.total} ({s.done / s.total * 100 if s.total else 0:.1f}%) "
            f"{rate:.1f}/min eta {format_eta(eta)}"
        )
    print(f"[{ts:%F %T}] " + "  ".join(parts), flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="每分钟采样爬取进度,写 CSV 并重绘趋势图")
    p.add_argument("--interval", type=int, default=60, help="采样间隔,秒(默认 60)")
    p.add_argument(
        "--window",
        type=int,
        default=30,
        help="速度计算窗口,分钟(默认 30;窗口内无增量视为停滞)",
    )
    p.add_argument("--csv", type=Path, default=Path("logs/progress.csv"))
    p.add_argument("--png", type=Path, default=Path("logs/progress.png"))
    p.add_argument("--once", action="store_true", help="只采样一次后退出(用于自检)")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(
            f"DB not found: {DB_PATH}\n"
            "run the crawler first, or point NEWSPAPER_DB_PATH at an existing database"
        )

    history = load_history(args.csv)
    window = deque(maxlen=max(2, args.window * 60 // max(args.interval, 1) + 2))
    for row in history:
        try:
            ts = datetime.strptime(row["timestamp"], TIME_FMT).replace(tzinfo=TZ_UTC8)
            snap = {
                s: Snap(
                    int(row[f"{s}_done"]),
                    int(row[f"{s}_total"]),
                    int(row[f"{s}_pending"]),
                )
                for s in STAGES
            }
        except (KeyError, ValueError):
            continue
        window.append((ts, snap))

    con = connect()
    try:
        while True:
            started = time.monotonic()
            ts = datetime.now(TZ_UTC8)
            snap = sample(con)
            window.append((ts, snap))
            cutoff = ts - timedelta(minutes=args.window)
            while len(window) > 1 and window[0][0] < cutoff:
                window.popleft()

            rates = compute_rates(window)
            row = {"timestamp": ts.strftime(TIME_FMT)}
            for stage in STAGES:
                s = snap[stage]
                rate, eta = rates[stage]
                row[f"{stage}_done"] = str(s.done)
                row[f"{stage}_total"] = str(s.total)
                row[f"{stage}_pending"] = str(s.pending)
                row[f"{stage}_rate"] = f"{rate:.2f}"
                row[f"{stage}_eta_min"] = "" if eta is None else f"{eta:.0f}"
            append_row(args.csv, row)
            history.append(row)

            print_sample(ts, snap, rates)
            plot(history, rates, args.png)

            if args.once:
                print(f"csv: {args.csv}\npng: {args.png}")
                break

            time.sleep(max(1.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        con.close()


if __name__ == "__main__":
    main()
