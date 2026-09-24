#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计日志 + 15 天过期清理（gd_app.py 配套模块）。

日志:
  logs/gd_audit_YYYY-MM-DD.txt 按天一文件，明文 txt，一行一条:
  时间 | IP | 专利号 | 操作 | 成败 | 耗时 | 附注

清理（启动时执行一次 + 每日 03:30 后台线程）:
  - logs/gd_audit_*.txt : 文件名日期超 15 天 → 删除（整文件删）
  - .cache/*            : mtime 超 15 天 → 删除（审查 PDF / OCR 缓存）
  - reports/            : 不动（报告是产出，永久保留）

清理只针对本项目目录，防御性跳过子目录与异常文件。
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

_BASE = Path(__file__).parent
LOGS_DIR = _BASE / "logs"
CACHE_DIR = _BASE / ".cache"
RETENTION_DAYS = 15

_lock = threading.Lock()

# 文件名中提取日期: gd_audit_2026-09-11.txt
_LOG_NAME_RE = re.compile(r"^gd_audit_(\d{4}-\d{2}-\d{2})\.txt$")


def audit(ip: str, patent: str, action: str, ok: bool,
          secs: float = 0.0, note: str = "") -> None:
    """追加一条审计日志（线程安全，失败静默不影响业务）。"""
    line = (f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"IP {ip or '-'} | {patent or '-'} | {action} | "
            f"{'成功' if ok else '失败'} | {secs:.1f}s"
            + (f" | {note}" if note else "") + "\n")
    try:
        with _lock:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            f = LOGS_DIR / f"gd_audit_{datetime.now():%Y-%m-%d}.txt"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass  # 日志失败不影响主流程


def cleanup_once() -> tuple[int, int]:
    """执行一次清理，返回 (删除日志数, 删除缓存数)。"""
    n_log = n_cache = 0
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    try:
        if LOGS_DIR.exists():
            for f in LOGS_DIR.iterdir():
                m = _LOG_NAME_RE.match(f.name)
                if not m or not f.is_file():
                    continue
                try:
                    if datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
                        f.unlink()
                        n_log += 1
                except (ValueError, OSError):
                    continue
    except OSError:
        pass
    try:
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                if not f.is_file():
                    continue  # 只清文件，不动子目录
                try:
                    if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                        f.unlink()
                        n_cache += 1
                except OSError:
                    continue
    except OSError:
        pass
    return n_log, n_cache


def _scheduler_loop() -> None:
    """每日 03:30 执行一次清理（服务常驻时生效）。"""
    while True:
        now = datetime.now()
        nxt = (now + timedelta(days=1)).replace(hour=3, minute=30, second=0, microsecond=0)
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        n_log, n_cache = cleanup_once()
        print(f"[gd_log] 定时清理: 日志 {n_log} 个, 缓存 {n_cache} 个", flush=True)


def start_scheduler() -> None:
    """启动时立即清理一次，并拉起每日清理线程（daemon，随进程退出）。"""
    n_log, n_cache = cleanup_once()
    print(f"[gd_log] 启动清理: 日志 {n_log} 个, 缓存 {n_cache} 个 "
          f"(保留 {RETENTION_DAYS} 天)", flush=True)
    threading.Thread(target=_scheduler_loop, daemon=True, name="gd-log-cleaner").start()


if __name__ == "__main__":
    # 手动执行: python gd_log.py  → 立即清理一次并打印结果
    n_log, n_cache = cleanup_once()
    print(f"清理完成: 日志 {n_log} 个, 缓存 {n_cache} 个 (保留 {RETENTION_DAYS} 天)")
