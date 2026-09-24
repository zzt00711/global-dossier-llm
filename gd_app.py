#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Dossier 五局实审分析助手 - Web UI（面向中国专利审查员）
================================================================
输入 CN 公开号 → 直连 Global Dossier 公开 JSON 接口（无需 API KEY/浏览器）
→ 抓取国外局（US/EP/JP/KR）历次实审通知书 → OCR（本地 rapidocr / MinerU 云端）
→ 大模型生成《国外局实审过程分析报告》→ 页面展示，支持 Markdown / PDF 下载。

启动:
  python gd_app.py                     # 默认 http://127.0.0.1:7860，自动打开浏览器
  python gd_app.py --port 9000 --no-browser
"""
import argparse
import collections
import html as htmllib
import json
import os
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file

import gd_helper
import gd_log

REPORTS_DIR = Path(__file__).parent / "reports"

app = Flask(__name__)


# ───────────────────────── 简易 Basic Auth ─────────────────────────
# 公网保护: 传 --password <密码> 或设环境变量 GD_PASSWORD 后启用;
# 未配置则完全关闭 (本地使用行为不变)。用户名固定 gdb。
AUTH_USER = "gdb"
G_PASSWORD = ""      # main() 由 --password / GD_PASSWORD 填充


@app.before_request
def _basic_auth_gate():
    """Basic 认证门: 未带正确凭据的请求返回 401, 浏览器弹原生密码框."""
    if not G_PASSWORD:
        return None
    if request.path == "/favicon.ico" or request.path.startswith("/static"):
        return None
    a = request.authorization
    if a and a.username == AUTH_USER and a.password == G_PASSWORD:
        return None
    resp = jsonify({"error": "需要访问密码 (用户名 gdb)"})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = 'Basic realm="Global Dossier"'
    return resp


# ───────────────────────── Markdown → HTML（轻量渲染） ─────────────────────────

def _esc(s: str) -> str:
    return htmllib.escape(s)


def _inline(s: str) -> str:
    """行内格式：**加粗** 与 `代码`，其余内容转义。"""
    out = []
    for tok in re.split(r"(\*\*.+?\*\*|`[^`]+`)", s):
        if tok.startswith("**") and tok.endswith("**"):
            out.append("<strong>" + _esc(tok[2:-2]) + "</strong>")
        elif tok.startswith("`") and tok.endswith("`"):
            out.append("<code>" + _esc(tok[1:-1]) + "</code>")
        else:
            out.append(_esc(tok))
    return "".join(out)


def md_to_html(md: str) -> str:
    """支持报告常用语法：标题、表格、列表、加粗、代码、分隔线、段落。"""
    lines = md.split("\n")
    out, i, n = [], 0, len(lines)
    in_code, code_buf = False, []

    while i < n:
        line = lines[i]
        if line.strip().startswith("```"):
            if not in_code:
                in_code, code_buf = True, []
            else:
                out.append("<pre><code>" + _esc("\n".join(code_buf)) + "</code></pre>")
                in_code = False
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*---+\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html = ["<table><thead><tr>"] + [f"<th>{_inline(h)}</th>" for h in header]
            html.append("</tr></thead><tbody>")
            for r in rows:
                html.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            html.append("</tbody></table>")
            out.append("".join(html))
            continue
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append("<li>" + _inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append("<li>" + _inline(re.sub(r"^\s*\d+\.\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        if not line.strip():
            i += 1
            continue
        buf = []
        while (i < n and lines[i].strip()
               and not re.match(r"^(#{1,4})\s|^\s*[-*]\s|^\s*\d+\.\s|^\s*\|", lines[i])):
            buf.append(_inline(lines[i]))
            i += 1
        out.append("<p>" + " ".join(buf) + "</p>")
    return "\n".join(out)


def wrap_html(body_html: str) -> str:
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<style>
body{{font-family:"Microsoft YaHei","PingFang SC","SimSun",sans-serif;font-size:11pt;line-height:1.65;color:#222;}}
h1{{font-size:18pt;border-bottom:2px solid #2e8b57;padding-bottom:6px;}}
h2{{font-size:14pt;color:#0a5;border-left:4px solid #0a5;padding-left:8px;margin-top:20px;}}
h3{{font-size:12.5pt;color:#333;margin-top:16px;}}
h4{{font-size:11pt;color:#444;}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:10pt;}}
th,td{{border:1px solid #bbb;padding:4px 8px;text-align:left;vertical-align:top;}}
th{{background:#eef7ef;}}
pre{{background:#f6f6f6;padding:8px;border-radius:4px;font-size:9.5pt;white-space:pre-wrap;}}
code{{background:#f4f4f4;padding:1px 4px;border-radius:3px;font-family:Consolas,monospace;}}
li{{margin:2px 0;}}
hr{{border:none;border-top:1px solid #ccc;}}
</style></head><body>{body_html}</body></html>"""


def md_to_pdf(md: str, out_path: Path) -> None:
    """用 Playwright 的 Chromium 把 Markdown 渲染成 PDF（支持中文）。"""
    from playwright.sync_api import sync_playwright
    html = wrap_html(md_to_html(md))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out_path), format="A4", print_background=True,
                 margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"})
        browser.close()


# ───────────────────────── 后台任务管理 ─────────────────────────

# 模型预设：UI 下拉框选择后显式传入 cfg（优先级最高，不受环境变量残留影响）
MODEL_PRESETS = {
    "dots3-note-prev": {
        "api_base": "https://note3-prev-api.askdiandian.com/v1",
        "api_key": "",  # 密钥不随仓库分发：由 .env / 环境变量 LLM_API_KEY 提供
    },
    "intern-s2-preview-35b": {
        "api_base": "https://chat.intern-ai.org.cn/api/v1",
        "api_key": "",  # 该免费端点无需 Key（如需要可从环境变量 LLM_API_KEY 提供）
    },
}


class TaskManager:
    """排队任务管理: FIFO 队列 + 并发槽位调度 + 协作式停止.

    状态机: queued(排队) → running → done / error / stopped
    停止: queued 直接出队(即时); running 置 cancel Event, 流水线检查点退出.
    """

    def __init__(self, max_concurrent: int = 2):
        self.tasks = {}
        self.lock = threading.Lock()
        self.order = []            # 排队 tid FIFO
        self.running = set()       # 运行中 tid
        self.max_concurrent = max(1, int(max_concurrent))
        self.durations = collections.deque(maxlen=20)  # 最近完成纯运行耗时(秒)
        self._CANCELABLE = ("queued", "running")

    def create(self, query: str, ip: str = "", cfg: dict | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        with self.lock:
            self.tasks[tid] = {
                "status": "queued", "query": query, "lines": [],
                "report": None, "html": None, "error": None,
                "md_path": None, "json_path": None, "pdf_path": None,
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ip": ip, "t0": time.monotonic(),
                "t_run0": None,       # 实际开始运行时刻（纯运行耗时起算点）
                "cancel": threading.Event(), "cfg": cfg or {},
            }
            self.order.append(tid)
            self._cleanup_old_locked()
            pos = len(self.order)
        # push/_dispatch 都要拿锁, 必须在锁外调用（先记录排队提示再调度）
        self.push(tid, f"任务已提交，排队第 {pos} 位" if pos > 1 else "任务已提交")
        self._dispatch()
        return tid

    def _dispatch(self) -> None:
        """把排队任务填进空闲槽位（持锁内调用安全, 锁外也可）。"""
        with self.lock:
            while len(self.running) < self.max_concurrent and self.order:
                tid = self.order.pop(0)
                t = self.tasks.get(tid)
                if t is None:
                    continue  # 已被清理（清理只删终态, 理论不可达, 防御）
                self.running.add(tid)
                t["status"] = "running"
                t["t_run0"] = time.monotonic()
                threading.Thread(target=_run_task, args=(tid, t["query"], t["cfg"]),
                                 daemon=True).start()

    def finish(self, tid: str) -> None:
        """任务线程退出（任何路径）: 释放槽位 → 记录耗时 → 触发下次调度。

        try/finally 保证一定执行, 槽位不泄漏, 队列永不卡死。
        """
        with self.lock:
            self.running.discard(tid)
            t = self.tasks.get(tid)
            if t and t.get("status") == "done" and t.get("t_run0"):
                self.durations.append(time.monotonic() - t["t_run0"])
        self._dispatch()

    def stop(self, tid: str) -> tuple[bool, str]:
        """停止任务。返回 (是否受理, 说明)。排队=出队即时; 运行=置标志等检查点。"""
        with self.lock:
            t = self.tasks.get(tid)
            if t is None:
                return False, "任务不存在"
            st = t["status"]
            if st not in self._CANCELABLE:
                return False, "任务已结束，无需停止"
            if st == "queued":
                try:
                    self.order.remove(tid)
                except ValueError:
                    pass
                t["status"] = "stopped"
                mode = "排队取消"
            else:
                t["cancel"].set()
                t["status"] = "stopping"
                mode = "运行中停止"
            ip, q = t.get("ip", ""), t.get("query", "")
            t0 = t.get("t0", time.monotonic())
        # 锁外 push（push 自身要拿锁）
        self.push(tid, "任务已停止（排队中取消）" if mode == "排队取消"
                 else "收到停止请求，等待当前步骤完成 ...")
        gd_log.audit(ip, q, "停止任务", True, time.monotonic() - t0, mode)
        return True, mode

    def _eta_min_locked(self, queue_pos: int) -> float:
        """预计等待（分钟）: (前面任务数 + 运行中按半程估计) ÷ 并发 × 平均时长。

        平均时长取最近 20 次完成任务的纯运行耗时滚动均值; 无历史按 8 分钟。
        """
        if queue_pos <= 0:
            return 0.0
        avg = (sum(self.durations) / len(self.durations)) if self.durations else 480.0
        pending = (queue_pos - 1) + 0.5 * len(self.running)
        return pending / self.max_concurrent * avg / 60.0

    def _cleanup_old_locked(self) -> None:
        """防御: tasks 内存态只增不减, 超 500 清最老终态（运行/排队不受影响）。"""
        if len(self.tasks) <= 500:
            return
        need = len(self.tasks) - 400
        for tid in [k for k, v in self.tasks.items()
                    if v["status"] in ("done", "error", "stopped")][:need]:
            del self.tasks[tid]

    def push(self, tid: str, msg: str) -> None:
        with self.lock:
            t = self.tasks.get(tid)
            if t is not None:
                # 流式进度行（[LLM 生成中]）覆盖更新最后一行，避免 lines 无限膨胀
                if (msg.startswith("[LLM 生成中]")
                        and t["lines"] and t["lines"][-1].startswith("[LLM 生成中]")):
                    t["lines"][-1] = msg
                else:
                    t["lines"].append(msg)

    def get(self, tid: str):
        with self.lock:
            return self.tasks.get(tid)


TM = TaskManager(max_concurrent=int(os.environ.get("GD_MAX_CONCURRENT", "2")))


def normalize_cn(raw: str) -> tuple[str, str]:
    """返回 (api_query, display)；剥离 CN 前缀与 kind 后缀。统一大写以兼容小写 cn/cn…a。"""
    q = raw.strip().upper()
    if not re.match(r"^(CN)?\d{6,12}", q):
        raise ValueError("请输入有效 CN 公开号，如 CN118076910")
    display = q if q.startswith("CN") else "CN" + q
    q = q[2:] if q[:2] == "CN" else q
    q = re.sub(r"[A-Z]\d*$", "", q)
    return q, display


def _run_task(tid: str, query: str, cfg: dict) -> None:
    t = TM.get(tid)
    cancel = t["cancel"] if t else None
    TM.push(tid, "任务已启动，查询同族 ...")
    try:
        api_q, display = normalize_cn(query)
        data = gd_helper.run_pipeline(api_q, cfg, progress=lambda m: TM.push(tid, m),
                                       cancel=cancel)
        data["query"] = display
        TM.push(tid, "\n[4/4] 大模型生成报告 ...")
        report = gd_helper.llm_generate_report(data, cfg, progress=lambda m: TM.push(tid, m),
                                               cancel=cancel)
        md_path = gd_helper.save_report(data, report, REPORTS_DIR)
        with TM.lock:
            t = TM.tasks[tid]
            t["status"] = "done"
            t["report"] = report
            t["html"] = md_to_html(report) if report else ""
            t["md_path"] = str(md_path)
            t["json_path"] = str(md_path.with_name(f"{display}_raw.json"))
            ip = t.get("ip", "")
            t_run0 = t.get("t_run0") or t.get("t0", time.monotonic())
        docs_n = sum(len(m.get("documents", [])) for m in data.get("members", []))
        # 耗时口径: 从实际开始运行(t_run0)起算的纯运行时长, 不含排队等待
        gd_log.audit(ip, display, "生成报告", True, time.monotonic() - t_run0,
                     f"文档={docs_n}")
        TM.push(tid, f"报告已保存: {md_path}")
    except gd_helper.TaskCancelled:
        # 用户停止: 状态已由 stop() 置为 stopping, 此处落到终态 stopped。
        # 不写失败审计（stop() 已记"停止任务"）, 只补停止说明行。
        with TM.lock:
            t = TM.tasks.get(tid)
            if t:
                t["status"] = "stopped"
        TM.push(tid, "任务已由用户停止")
    except BaseException as e:  # SystemExit 也会从 run_pipeline 中抛出
        with TM.lock:
            t = TM.tasks.get(tid)
            if t:
                t["status"] = "error"
                t["error"] = str(e)
                ip = t.get("ip", "")
                t_run0 = t.get("t_run0") or t.get("t0", time.monotonic())
            else:
                ip, t_run0 = "", time.monotonic()
        gd_log.audit(ip, query, "生成报告", False, time.monotonic() - t_run0,
                     str(e)[:120])
        TM.push(tid, f"任务失败: {e}")
    finally:
        # 任何退出路径必经: 释放并发槽位并触发后续任务调度（队列不卡死的保证）
        TM.finish(tid)


# ───────────────────────── API ─────────────────────────

@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/generate", methods=["POST"])
def api_generate():
    body = request.get_json(force=True, silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "请输入 CN 公开号"}), 400
    try:
        normalize_cn(query)
    except Exception as e:
        gd_log.audit(request.remote_addr, query, "提交查询", False, 0.0, str(e)[:80])
        return jsonify({"error": str(e)}), 400
    offices = {o for o in (body.get("offices") or ["US", "EP", "JP", "KR"]) if o}
    model = (body.get("model") or "").strip() or "dots3-note-prev"
    preset = MODEL_PRESETS.get(model, {})
    # LLM 配置优先级：UI 输入 > 预设（仅当用户输入为空时回退预设）
    api_base = (body.get("api_base") or "").strip() or preset.get("api_base", "")
    api_key = (body.get("api_key") or "").strip() or preset.get("api_key", "")
    cfg = {
        "offices": offices,
        "ocr": body.get("ocr") or "auto",
        "mineru_key": gd_helper.resolve_mineru_key(""),
        "max_docs": int(body.get("max_docs") or 8),
        "doc_text_len": 25666,
        "cache_dir": ".cache",
        "out_dir": str(REPORTS_DIR),
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "max_tokens": 0, "thinking": bool(body.get("thinking")),
    }
    tid = TM.create(query, ip=request.remote_addr or "", cfg=cfg)
    gd_log.audit(request.remote_addr, query, "提交查询", True, 0.0,
                 f"局={','.join(sorted(offices))} 模型={model}")
    return jsonify({"task_id": tid})


@app.route("/api/status/<tid>")
def api_status(tid: str):
    t = TM.get(tid)
    if t is None:
        return jsonify({"error": "任务不存在"}), 404
    with TM.lock:
        status = t["status"]
        queue_pos = 0
        if status == "queued":
            try:
                queue_pos = TM.order.index(tid) + 1
            except ValueError:
                pass
        eta_min = TM._eta_min_locked(queue_pos)
        lines = list(t["lines"])
        error, query, created = t["error"], t["query"], t["created"]
        running_count = len(TM.running)
        queued_count = len(TM.order)
    return jsonify({"status": status, "lines": lines, "error": error,
                    "query": query, "created": created,
                    "queue_pos": queue_pos, "eta_min": round(eta_min, 1),
                    "running_count": running_count, "queued_count": queued_count})


@app.route("/api/stop/<tid>", methods=["POST"])
def api_stop(tid: str):
    ok, msg = TM.stop(tid)
    if not ok:
        code = 404 if msg == "任务不存在" else 409
        return jsonify({"error": msg}), code
    return jsonify({"ok": True, "mode": msg})


@app.route("/api/queue")
def api_queue():
    with TM.lock:
        return jsonify({"running": len(TM.running), "queued": len(TM.order),
                        "max": TM.max_concurrent})


@app.route("/api/result/<tid>")
def api_result(tid: str):
    t = TM.get(tid)
    if t is None:
        return jsonify({"error": "任务不存在"}), 404
    if t["status"] != "done":
        return jsonify({"error": "任务尚未完成"}), 400
    return jsonify({"markdown": t["report"], "html": t["html"]})


def _date_tag(s: str) -> str:
    """legalDateStr（MM/DD/YYYY 或 YYYY-MM-DD）→ YYYYMMDD 文件名前缀。"""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    if m:
        return f"{m.group(3)}{m.group(1)}{m.group(2)}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    if m:
        return m.group(0).replace("-", "")
    return "无日期"


def _safe_zip_name(name: str) -> str:
    """清洗文书名中 zip 条目非法字符，限长防路径超限。"""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "文档").strip(" .") or "文档"
    return safe[:80]


def _build_exam_zip(t: dict, md_path: Path) -> Path:
    """把该案件下载到的所有审查过程 PDF 打包（按局分目录，日期+文书名命名）。

    源文件在 .cache/（15 天后会被清理），因此打包结果缓存到 reports/：
    首次点击打包，之后同号任务直接复用。"""
    zip_path = md_path.with_name(f"{t['query']}_审查文件包.zip")
    if zip_path.exists():
        return zip_path
    json_path = Path(t["json_path"]) if t.get("json_path") else md_path.with_suffix(".json")
    if not json_path.exists():
        raise FileNotFoundError("原始数据 JSON 不存在，无法定位审查文件")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    cache_dir = Path(__file__).parent / ".cache"
    entries: list[tuple[Path, str]] = []
    for m in data.get("members", []):
        office, app_num = m.get("office", ""), m.get("app_num", "")
        for d in m.get("documents", []):
            pdf_name = d.get("pdf")
            if not pdf_name:
                continue
            src = cache_dir / pdf_name
            if not src.exists():
                continue  # 下载失败或缓存已过期的跳过
            arc = f"{office}_{app_num}/{_date_tag(d.get('date'))}_{_safe_zip_name(d.get('name'))}.pdf"
            entries.append((src, arc))
    if not entries:
        raise FileNotFoundError("无可打包的审查文件（缓存已被清理或全部下载失败）")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        # PDF 本身已压缩，ZIP_STORED 直接存储更快
        for src, arc in entries:
            zf.write(src, arc)
    return zip_path


@app.route("/api/download/<tid>")
def api_download(tid: str):
    t = TM.get(tid)
    if t is None or not t.get("md_path"):
        return jsonify({"error": "报告不存在"}), 404
    fmt = request.args.get("fmt", "md")
    md_path = Path(t["md_path"])
    ip, q = t.get("ip", ""), t.get("query", "")
    t0 = time.monotonic()
    if fmt == "md":
        resp = send_file(md_path, as_attachment=True, download_name=md_path.name,
                         mimetype="text/markdown; charset=utf-8")
        gd_log.audit(ip, q, "下载", True, time.monotonic() - t0,
                     f"格式=md 大小={md_path.stat().st_size // 1024}KB")
        return resp
    if fmt == "json":
        json_path = Path(t["json_path"]) if t.get("json_path") else md_path.with_suffix(".json")
        if not json_path.exists():
            return jsonify({"error": "原始数据文件不存在"}), 404
        resp = send_file(json_path, as_attachment=True, download_name=json_path.name,
                         mimetype="application/json; charset=utf-8")
        gd_log.audit(ip, q, "下载", True, time.monotonic() - t0,
                     f"格式=json 大小={json_path.stat().st_size // 1024}KB")
        return resp
    if fmt == "pdf":
        pdf_path = Path(t.get("pdf_path")) if t.get("pdf_path") else md_path.with_suffix(".pdf")
        # 每次点击下载 PDF 都强制重新生成（用户要求），确保导出永远基于最新报告
        try:
            md_to_pdf(t["report"] or md_path.read_text(encoding="utf-8"), pdf_path)
        except Exception as e:
            return jsonify({"error": f"PDF 生成失败: {e}"}), 500
        with TM.lock:
            TM.tasks[tid]["pdf_path"] = str(pdf_path)
        resp = send_file(pdf_path, as_attachment=True, download_name=pdf_path.name,
                         mimetype="application/pdf")
        gd_log.audit(ip, q, "下载", True, time.monotonic() - t0,
                     f"格式=pdf 大小={pdf_path.stat().st_size // 1024}KB")
        return resp
    if fmt == "zip":
        try:
            zip_path = _build_exam_zip(t, md_path)
        except FileNotFoundError as e:
            gd_log.audit(ip, q, "下载", False, time.monotonic() - t0, f"格式=zip {str(e)[:80]}")
            return jsonify({"error": str(e)}), 404
        except Exception as e:
            gd_log.audit(ip, q, "下载", False, time.monotonic() - t0, f"格式=zip {str(e)[:80]}")
            return jsonify({"error": f"打包失败: {e}"}), 500
        resp = send_file(zip_path, as_attachment=True, download_name=zip_path.name,
                         mimetype="application/zip")
        gd_log.audit(ip, q, "下载", True, time.monotonic() - t0,
                     f"格式=zip 大小={zip_path.stat().st_size // 1024}KB")
        return resp
    return jsonify({"error": "不支持的格式"}), 400


# ───────────────────────── 页面 ─────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Dossier 五局实审分析助手</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#f0f4f8;color:#223;line-height:1.6;padding:20px}
.wrap{max-width:960px;margin:0 auto}
header{background:linear-gradient(135deg,#0b3a6b,#128c9c);color:#fff;border-radius:12px;padding:22px 28px;margin-bottom:18px}
header h1{font-size:22px;margin-bottom:6px}
header p{opacity:.92;font-size:13px}
.card{background:#fff;border-radius:12px;padding:22px 26px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
label{font-weight:600;display:block;margin-bottom:6px}
input[type=text]{width:100%;padding:11px 14px;font-size:15px;border:1px solid #c4d0cc;border-radius:8px;outline:none}
input[type=text]:focus{border-color:#128c9c;box-shadow:0 0 0 3px rgba(18,140,156,.12)}
.row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-top:12px}
.opt{display:flex;flex-direction:column;gap:4px}
.opt span{font-size:12px;color:#667}
select,input[type="text"],input[type="password"]{padding:9px 10px;border:1px solid #c4d0cc;border-radius:8px;font-size:14px;box-sizing:border-box}
button{background:#128c9c;color:#fff;border:none;padding:11px 26px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#0e7a88}
button:disabled{background:#9ec7cf;cursor:not-allowed}
details{margin-top:14px}
summary{cursor:pointer;color:#128c9c;font-weight:600;font-size:14px}
.help{font-size:13px;color:#445;margin-top:10px}
.help li{margin:5px 0 5px 18px}
.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:600;margin-left:8px}
.badge.queue{background:#cfe2ff;color:#084298}
.badge.run{background:#fff3cd;color:#856404}
.badge.stop{background:#ffe5d0;color:#a05a00}
.badge.stopgray{background:#e2e3e5;color:#414549}
.badge.done{background:#d4edda;color:#155724}
.badge.err{background:#f8d7da;color:#721c24}
.stopbtn{background:#c0392b;color:#fff;border:none;padding:7px 18px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;margin-left:10px;vertical-align:middle}
.stopbtn:hover{background:#96281b}
.stopbtn:disabled{background:#e5a89f;cursor:not-allowed}
#busy{font-size:13px;color:#128c9c;margin-top:6px;font-weight:600}
#log{background:#0f2027;color:#c7f0d8;font-family:Consolas,"Courier New",monospace;font-size:12px;border-radius:8px;padding:12px;height:220px;overflow-y:auto;white-space:pre-wrap;display:none;margin-top:12px}
.hidden{display:none!important}
#result{padding-top:8px}
#report{overflow-wrap:break-word}
#report h1{font-size:20px;border-bottom:2px solid #2e8b57;padding-bottom:6px;margin:14px 0}
#report h2{font-size:16px;color:#0a5;border-left:4px solid #0a5;padding-left:8px;margin:16px 0 8px}
#report h3{font-size:14px;margin:14px 0 6px}
#report h4{font-size:13px;margin:10px 0 4px}
#report table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
#report th,#report td{border:1px solid #c9d6d2;padding:5px 9px;text-align:left;vertical-align:top}
#report th{background:#e8f5ee}
#report pre{background:#f6f6f6;padding:9px;border-radius:6px;font-size:12px;white-space:pre-wrap}
#report code{background:#f1f1f1;padding:1px 4px;border-radius:3px}
#report ul,#report ol{margin:4px 0 4px 22px}
#report li{margin:2px 0}
#report hr{border:none;border-top:1px solid #dde}
.dlbar{display:flex;gap:10px;margin:14px 0 6px;flex-wrap:wrap}
.dlbar a{display:inline-block;padding:9px 18px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px}
.dl-pdf{background:#d43d3d;color:#fff}
.dl-pdf:hover{background:#b83333}
.dl-md{background:#3a6ea5;color:#fff}
.dl-md:hover{background:#315d8c}
.dl-json{background:#7a5aa8;color:#fff}
.dl-json:hover{background:#6a4c94}
.dl-zip{background:#2e8b57;color:#fff}
.dl-zip:hover{background:#256f47}
#err{background:#d43d3d;color:#fff;padding:14px 18px;border-radius:8px;display:none;margin-top:12px;font-weight:700;font-size:15px;border-left:6px solid #a02b2b;box-shadow:0 2px 10px rgba(212,61,61,.35);animation:errShake .45s ease}
@keyframes errShake{0%,100%{transform:translateX(0)}25%{transform:translateX(-5px)}75%{transform:translateX(5px)}}
#tips{font-size:12px;color:#888;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🌐 Global Dossier 五局实审分析助手</h1>
    <p>输入 CN 公开号，直连 Global Dossier 获取国外局（US/EP/JP/KR）历次实审通知书，由大模型生成面向中国审查员的《国外局实审过程分析报告》，支持下载 PDF / Markdown / 原始数据 / 审查文件包</p>
  </header>

  <div class="card">
    <label for="q">CN 公开号</label>
    <input type="text" id="q" placeholder="例如：CN118076910  或  CN117460982A">
    <div class="row">
      <div class="opt">
        <span>关注国外局（可多选）</span>
        <div style="display:flex;gap:10px;align-items:center">
          <label style="display:flex;align-items:center;gap:4px;font-weight:400;cursor:pointer"><input type="checkbox" id="ofUS" checked style="width:15px;height:15px">US</label>
          <label style="display:flex;align-items:center;gap:4px;font-weight:400;cursor:pointer"><input type="checkbox" id="ofEP" checked style="width:15px;height:15px">EP</label>
          <label style="display:flex;align-items:center;gap:4px;font-weight:400;cursor:pointer"><input type="checkbox" id="ofJP" style="width:15px;height:15px">JP</label>
          <label style="display:flex;align-items:center;gap:4px;font-weight:400;cursor:pointer"><input type="checkbox" id="ofKR" style="width:15px;height:15px">KR</label>
        </div>
      </div>
      <div class="opt">
        <span>OCR 后端</span>
        <select id="ocr">
          <option value="auto" selected>auto（有 MinerU Token 用云端，否则本地）</option>
          <option value="mineru">MinerU 云端（快，需 Token）</option>
          <option value="rapidocr">rapidocr 本地（免费，慢）</option>
        </select>
      </div>
      <div class="opt">
        <span>每局文书数</span>
        <select id="maxd">
          <option value="4">4 份（最快）</option>
          <option value="8" selected>8 份（推荐）</option>
          <option value="12">12 份（更全）</option>
        </select>
      </div>
      <div class="opt">
        <span>模型（可输入自定义，或选预设）</span>
        <input type="text" id="model" list="model_presets" value="dots3-note-prev">
        <datalist id="model_presets">
          <option value="dots3-note-prev">dots3-note-prev（默认，多模态）</option>
          <option value="intern-s2-preview-35b">intern-s2-preview-35b</option>
        </datalist>
      </div>
      <div class="opt">
        <span>API Base URL（可选，留空用预设）</span>
        <input type="text" id="api_base" placeholder="https://.../v1" style="width:220px">
      </div>
      <div class="opt">
        <span>API Key（可选，留空用预设）</span>
        <input type="password" id="api_key" placeholder="sk-..." style="width:220px">
      </div>
      <div class="opt">
        <span>模型思考模式</span>
        <select id="think">
          <option value="0" selected>关闭（快速）</option>
          <option value="1">开启（更严谨）</option>
        </select>
      </div>
      <button id="go" onclick="start()">开始查询</button>
    </div>
    <p id="tips">提示：全新案件约 5~10 分钟（下载通知书 + OCR + 大模型报告）；已缓存案件 1~2 分钟。期间请勿关闭页面。任务排队时可点"停止任务"取消。</p>
    <p id="busy">⚙ 加载中 ...</p>

    <details>
      <summary>📖 使用说明</summary>
      <div class="help">
        <ul>
          <li><b>输入格式</b>：CN 公开号（如 <code>CN118076910</code>、<code>CN117460982A</code>），字母不区分大小写，kind 后缀（A/B）可带可不带。</li>
          <li><b>运行流程</b>：① 直连 Global Dossier 查询同族 → ② 逐个国外局挑选实审文书（审查意见 / 892 引证清单 / 申请人答复 / 权利要求书等）并下载 PDF → ③ OCR 抽取文本（MinerU 云端优先）→ ④ 大模型生成报告。</li>
          <li><b>报告内容</b>：跨局对比与中国审查员参考（置顶）、历次通知书详解（引用法条 / 对比文件 X·Y·A / 审查意见要点）、申请人答复与权利要求修改、同族总览、时间线、最终审查结论（授权/驳回/视为撤回）与授权独立权利要求。</li>
          <li><b>面向用户</b>：中国专利审查员，聚焦国外局实审过程，不分析中国同族。</li>
          <li><b>MinerU OCR</b>：检测到同目录 <code>peizhi.txt</code> 或 <code>.env</code> 中的 <code>MINERU_API_KEY</code> 时自动使用云端高精度 OCR；无 Token 时回退本地 rapidocr。</li>
          <li><b>模型配置</b>：下拉框可在 <code>dots3-note-prev</code>（默认，多模态，内置 Token）与 <code>intern-s2-preview-35b</code> 之间切换；也可在同目录 <code>.env</code> 中修改 LLM_API_BASE / LLM_API_KEY / LLM_MODEL。</li>
        </ul>
      </div>
    </details>
  </div>

  <div class="card hidden" id="progressCard">
    <h3 style="font-size:15px">执行进度 <span class="badge run" id="badge">运行中…</span><button id="stopBtn" class="stopbtn" style="display:none" onclick="stopTask()">■ 停止任务</button></h3>
    <div id="log"></div>
  </div>

  <div class="card hidden" id="err"></div>

  <div class="card hidden" id="resultCard">
    <h3 style="font-size:15px">审查报告<span class="badge done">已完成</span></h3>
    <div class="dlbar">
      <a class="dl-pdf" id="dlPdf" href="#">⬇ 下载 PDF</a>
      <a class="dl-md" id="dlMd" href="#">⬇ 下载 Markdown</a>
      <a class="dl-json" id="dlJson" href="#">⬇ 原始数据 JSON</a>
      <a class="dl-zip" id="dlZip" href="#" title="打包下载本案件下载到的所有审查过程 PDF（按局分目录）">⬇ 审查文件包(ZIP)</a>
    </div>
    <div id="report"></div>
  </div>
</div>

<script>
// ES5 写法 (无箭头函数/async-await/fetch): 兼容 Win7 IE11 及国产浏览器兼容模式
function $(id){return document.getElementById(id);}
var pollTimer=null;
var curTask=null;    // 当前任务 id (停止按钮用)
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}

// 统一 XHR 封装: cb(err, data), 替代 fetch+await
function xhrJson(url,method,body,cb){
  var x=new XMLHttpRequest();
  x.open(method||'GET',url,true);
  if(body){x.setRequestHeader('Content-Type','application/json');}
  x.onreadystatechange=function(){
    if(x.readyState!==4){return;}
    var data=null,err=null;
    try{data=JSON.parse(x.responseText);}
    catch(e){err='响应解析失败 (HTTP '+x.status+')';}
    if(!err&&x.status>=400){err=(data&&data.error)||('HTTP '+x.status);}
    if(err){cb(err,null);}else{cb(null,data);}
  };
  x.send(body?JSON.stringify(body):null);
}

function start(){
  var q=$('q').value.trim();
  if(!q){alert('请输入 CN 公开号');return;}
  $('go').disabled=true;
  $('resultCard').className='card hidden';
  $('err').className='card hidden';
  $('progressCard').className='card';
  $('log').style.display='block';
  $('log').innerText='';$('badge').textContent='提交中…';
  $('badge').className='badge queue';
  $('stopBtn').style.display='none';$('stopBtn').disabled=false;
  var offices=[];
  if($('ofUS').checked)offices.push('US');
  if($('ofEP').checked)offices.push('EP');
  if($('ofJP').checked)offices.push('JP');
  if($('ofKR').checked)offices.push('KR');
  if(!offices.length){alert('请至少选择一个国外局');$('go').disabled=false;return;}
  var body={query:q,offices:offices,ocr:$('ocr').value,max_docs:parseInt($('maxd').value,10),
    model:$('model').value.trim(),api_base:$('api_base').value.trim(),api_key:$('api_key').value.trim(),
    thinking:$('think').value==='1'};
  xhrJson('/api/generate','POST',body,function(err,data){
    if(err){showErr(err);return;}
    if(data.error){showErr(data.error);return;}
    curTask=data.task_id;
    poll(curTask);
  });
}

function stopTask(){
  if(!curTask){return;}
  if(!confirm('确定停止当前任务？\\n已下载的文书缓存会保留（重查同号可直接复用），但不会生成报告。')){return;}
  $('stopBtn').disabled=true;
  xhrJson('/api/stop/'+curTask,'POST',null,function(err,d){
    if(err){alert(err);$('stopBtn').disabled=false;return;}
    if(d.error){alert(d.error);$('stopBtn').disabled=false;return;}
    if(d.mode==='排队取消'){/* 状态由下次轮询刷新 */}
    /* 运行中停止: 徽章变"停止中…", 等待流水线检查点退出 */
  });
}

function updateBusy(r,q){
  if(typeof r==='undefined'){return;}
  $('busy').textContent=(r===0&&q===0)?'⚙ 系统空闲':('⚙ 当前运行 '+r+' 个 · 排队 '+q+' 个');
}
function loadBusy(){
  xhrJson('/api/queue',null,null,function(err,d){
    if(err){return;}
    updateBusy(d.running,d.queued);
  });
}
window.onload=function(){loadBusy();};

function poll(id){
  xhrJson('/api/status/'+id,null,null,function(err,s){
    if(err){showErr(err);return;}  // 含 API 层错误（如任务不存在）
    $('log').innerText=s.lines.join('\\n');
    $('log').scrollTop=$('log').scrollHeight;
    updateBusy(s.running_count,s.queued_count);
    if(s.status==='queued'){
      $('badge').textContent='排队中·第 '+s.queue_pos+' 位（预计约 '+Math.max(1,Math.ceil(s.eta_min))+' 分钟）';
      $('badge').className='badge queue';
      $('stopBtn').style.display='';$('stopBtn').disabled=false;
      pollTimer=setTimeout(function(){poll(id);},1500);return;
    }
    if(s.status==='running'){
      $('badge').textContent='运行中…';$('badge').className='badge run';
      $('stopBtn').style.display='';$('stopBtn').disabled=false;
      pollTimer=setTimeout(function(){poll(id);},1500);return;
    }
    if(s.status==='stopping'){
      $('badge').textContent='停止中…';$('badge').className='badge stop';
      $('stopBtn').style.display='';$('stopBtn').disabled=true;
      pollTimer=setTimeout(function(){poll(id);},1000);return;
    }
    if(s.status==='stopped'){
      clearTimeout(pollTimer);
      $('badge').textContent='已停止';$('badge').className='badge stopgray';
      $('stopBtn').style.display='none';$('stopBtn').disabled=false;
      $('go').disabled=false;
      return;
    }
    if(s.status==='done'){clearTimeout(pollTimer);$('badge').textContent='已完成';
      $('badge').className='badge done';$('stopBtn').style.display='none';loadResult(id);return;}
    if(s.status==='error'){clearTimeout(pollTimer);$('badge').textContent='失败';
      $('badge').className='badge err';$('stopBtn').style.display='none';showErr(s.error||'生成失败');return;}
  });
}

function loadResult(id){
  xhrJson('/api/result/'+id,null,null,function(err,d){
    if(err){showErr(err);return;}
    if(d.error){showErr(d.error);return;}
    $('report').innerHTML=d.html;
    $('dlPdf').href='/api/download/'+id+'?fmt=pdf';
    $('dlMd').href='/api/download/'+id+'?fmt=md';
    $('dlJson').href='/api/download/'+id+'?fmt=json';
    $('dlZip').href='/api/download/'+id+'?fmt=zip';
    $('resultCard').className='card';
    $('go').disabled=false;
  });
}

function showErr(msg){$('err').innerHTML='❌ '+esc(msg);$('err').className='card';$('go').disabled=false;}
</script>
</body>
</html>"""


def main():
    global G_PASSWORD
    ap = argparse.ArgumentParser(description="Global Dossier 五局实审分析 Web UI")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址 (公网部署用 0.0.0.0)")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--password", default="",
                    help="访问密码 (用户名固定 gd; 也可用环境变量 GD_PASSWORD)")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()
    G_PASSWORD = args.password or os.environ.get("GD_PASSWORD", "")
    gd_log.start_scheduler()  # 启动即清理过期日志/缓存，并拉起每日 03:30 清理线程
    url = f"http://127.0.0.1:{args.port}"
    print(f"Global Dossier 五局实审分析 UI: {url}")
    print(f"访问密码: {'已启用' if G_PASSWORD else '未启用 (本地使用)'}")
    print(f"任务调度: 最大并发 {TM.max_concurrent} (GD_MAX_CONCURRENT 可调), 其余排队")
    print("按 Ctrl+C 停止服务")
    if not args.no_browser:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
