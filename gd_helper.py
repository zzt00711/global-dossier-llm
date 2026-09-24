#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Dossier 五局实审分析助手（面向中国专利审查员）
========================================================
输入 CN 公开号，直连 USPTO Global Dossier 的公开 JSON 接口（CloudFront CDN，
无需 API KEY、无需浏览器），获取同族中**国外局（US/EP/JP/KR）**的实审文书，
下载历次审查通知书 / 申请人答复 / 检索相关 PDF，经 OCR（本地 rapidocr 或
MinerU 云端 API）抽取文本，正则预提取【对比文件引用 / 权利要求 / 审查结论】，
再由大模型生成面向中国审查员的《国外局实审过程分析报告》。

报告聚焦（对中国审查员的实用价值）:
  1. 历次通知书中引用的对比文件（编号/类别 X·Y·A/专利号）
  2. 申请人答复与权利要求修改（历次）
  3. 最终审查结论：驳回 / 授权 / 视为撤回等
  4. 授权权利要求文本

用法:
  python gd_helper.py CN118076910
  python gd_helper.py CN118076910 --ocr mineru --mineru-key <token>
  python gd_helper.py CN118076910 --no-llm            # 只抓取+提取，不调大模型
  python gd_helper.py CN118076910 --offices US,EP,JP,KR

依赖: curl_cffi, PyMuPDF, requests, openai
可选: rapidocr-onnxruntime(本地OCR) / mineru-open-sdk(云端OCR, 需 MINERU_API_KEY)

大模型配置优先级: 命令行 > 环境变量 LLM_* > 项目 .env > 内置默认(dots3-note-prev)
"""
import argparse
import copy
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as cr

from extract import detect_conclusion, detect_doc_type, extract_citations, extract_claims
from ocr import extract_text

HOST = "https://d1kazzu6rbodne.cloudfront.net"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 默认关注国外局（中国审查员视角，不分析中国同族）
DEFAULT_OFFICES = ["US", "EP", "JP", "KR"]
# 大模型默认配置（与旧项目一致：dots3-note-prev，阿里百炼 Token Plan）
DEFAULT_LLM_BASE = "https://note3-prev-api.askdiandian.com/v1"
DEFAULT_LLM_MODEL = "dots3-note-prev"
DEFAULT_LLM_KEY = ""  # 密钥不随仓库分发：请在 .env 或环境变量 LLM_API_KEY 中配置
DEFAULT_MAX_TOKENS = 32768
# 大模型调用参数（默认值；可用 cfg / 环境变量 LLM_* / 项目 .env 覆盖）
LLM_TIMEOUT = 300        # 请求超时（秒），防服务端挂起时 SDK 默认静默阻塞约 30 分钟
LLM_MAX_RETRIES = 1      # 失败重试次数（openai SDK 自动重试）
LLM_STREAM = True        # 流式输出：True 实时回传生成进度，False 一次性返回
LLM_PROGRESS_STEP = 400  # 流式进度回传步长（字符）
ENV_FILE = Path(__file__).parent / ".env"


def load_env(path: Path = ENV_FILE) -> dict:
    """读取 .env 键值（简单解析，忽略注释/空行/引号）。"""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_mineru_key(cli_key: str = "") -> str:
    """MinerU Token 来源优先级：命令行 > 环境变量 > 项目 .env > peizhi.txt"""
    if cli_key:
        return cli_key
    env = load_env()
    for k in ("MINERU_API_KEY", "MINERU_KEY"):
        v = os.environ.get(k, "") or env.get(k, "")
        if v:
            return v
    for p in (Path(__file__).parent / "peizhi.txt", Path("peizhi.txt")):
        if p.exists():
            t = p.read_text(encoding="utf-8").strip().strip('"')
            if t:  # OpenXLab JWT Token
                return t
    return ""


def log(msg):
    print(msg, flush=True)


def _date_key(s: str) -> tuple:
    """日期排序键：MM/DD/YYYY 或 YYYY-MM-DD → (y, m, d)；空值排最后。
    不能用字符串直接排序（'12/04/2025' 字典序会排在 '04/24/2026' 前）。"""
    s = (s or "").strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return (int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (9999, 12, 31)


# ────────────────────────── 任务取消 ──────────────────────────
class TaskCancelled(Exception):
    """用户请求停止（Web UI 停止按钮置位 cancel Event 后由检查点抛出）。

    协作式取消: Python 线程无法安全强杀, 停止方只置 Event 标志,
    流水线在检查点主动查看并抛本异常退出。"""


def _check_cancel(cancel) -> None:
    """取消检查点: cancel Event 已置位则抛 TaskCancelled (None 容忍, CLI 不传)."""
    if cancel is not None and cancel.is_set():
        raise TaskCancelled()


# ────────────────────────── 全局限速器 ──────────────────────────
class RateLimiter:
    """进程级全局限速（跨 GDClient 实例 / 线程共享）：
    两次请求间最小间隔 MIN_GAP + 随机抖动 JITTER。

    用途：
    1. 同 IP 多用户共用服务时，统一控制对上游的请求速率，避免集中突发触发限流；
    2. 抖动打破固定请求节奏，降低行为模式检测命中概率。
    """
    _lock = threading.Lock()
    _last = 0.0
    MIN_GAP = 1.0   # 两次请求最小间隔（秒）
    JITTER = 2.0    # 额外随机抖动（秒），随机范围 [0, JITTER)

    @classmethod
    def wait(cls):
        gap = cls.MIN_GAP + random.uniform(0, cls.JITTER)
        with cls._lock:
            dt = time.monotonic() - cls._last
            if dt < gap:
                time.sleep(gap - dt)
            cls._last = time.monotonic()


# ────────────────────────── CloudFront API 客户端 ──────────────────────────
class GDClient:
    """直连 Global Dossier 公开 JSON 接口（无需 API KEY / 浏览器）。"""

    def __init__(self, retries: int = 3, timeout: int = 60):
        self.retries = retries
        self.timeout = timeout

    def _get(self, url: str) -> cr.Response:
        RateLimiter.wait()  # 全局限速（含抖动）
        last = None
        for i in range(self.retries):
            try:
                r = cr.get(url, impersonate="chrome", timeout=self.timeout,
                           headers={"User-Agent": UA})
                if r.status_code == 429:
                    # 限流：优先尊重 Retry-After 头；缺失时按 3s*2^i 指数退避
                    ra = r.headers.get("Retry-After", "").strip()
                    wait = float(ra) if ra.replace(".", "").isdigit() else min(3 * (2 ** i), 60)
                    log(f"  [429 限流] {url} → 等待 {wait:.1f}s 后重试 (第{i+1}/{self.retries}次)")
                    time.sleep(wait)
                    continue
                if r.status_code < 500:
                    return r
                last = r
            except Exception as e:
                last = e
            time.sleep(3 + random.uniform(0, 2))  # 500+/异常：带抖动的退避
        raise RuntimeError(f"HTTP获取失败: {url} ({last})")

    def family(self, cn_pub: str) -> list[dict]:
        """按 CN 公开号查询同族。"""
        r = self._get(f"{HOST}/patent-family/svc/family/publication/CN/{cn_pub}")
        if r.status_code != 200:
            raise RuntimeError(f"同族查询失败 HTTP {r.status_code}: {r.text[:200]}")
        return r.json().get("list", [])

    def doclist(self, country: str, app_num: str, kind: str) -> dict:
        r = self._get(f"{HOST}/doc-list/svc/doclist/{country}/{app_num}/{kind}")
        if r.status_code != 200:
            raise RuntimeError(f"文书列表失败 HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def download(self, country: str, app_num: str, doc_number: str,
                 doc_id: str, pages: int, out_path: Path) -> str:
        """下载文书 PDF。doccontent 路径不含 kind 段、页数为真实页数；
        US 用 appNum，其他局依次尝试 appNum / 去后缀 docNumber / 带 kind 的 docNumber。
        注意：CloudFront 对错误路径返回 HTTP 200 + "Attachment Not Found" 纯文本，
        不能只看状态码；必须校验响应以 %PDF 开头，否则继续尝试下一候选。
        JP 只有带 kind 后缀的 docNumber（如 2024504797.A）才返回真 PDF。"""
        candidates = [app_num]
        if country.upper() != "US":
            candidates += [(doc_number or "").rsplit(".", 1)[0], doc_number or ""]
        if doc_number:
            candidates += [doc_number]  # 完整 docNumber（含 kind 后缀，JP 必需）
        for num in dict.fromkeys(c for c in candidates if c):
            url = (f"{HOST}/doc-content/svc/doccontent/{country.upper()}/"
                   f"{num}/{doc_id}/{pages}/PDF")
            try:
                r = self._get(url)
            except Exception:
                continue
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                out_path.write_bytes(r.content)
                return url
            if r.status_code != 404:
                time.sleep(2)
        raise RuntimeError(f"全部下载失败: {out_path.name}")


# ────────────────────────── 实审文书筛选 ──────────────────────────
# 文书类别（按中国审查员关注点）：最终授权/驳回 > 审查意见 > 对比文件清单 > 申请人答复 > 权利要求书
FAMILY_ORDER = ["final_grant", "rejection", "citation", "applicant", "claims", "other"]
FAMILY_QUOTA = {"final_grant": 1, "rejection": 2, "citation": 2, "applicant": 2, "claims": 1, "other": 0}
CODE_RANK = {"NOA": 0, "CTFR": 1, "CTNF": 2, "A01": 0, "A131": 1, "A523": 2, "A53": 3,
             "AIB21J": 4, "892": 3, "SRNT": 4, "SRFW": 6, "CLM": 5, "ISR": 4, "EDREX": 3,
             "2004": 3, "2015C": 1, "REM": 5, "A...": 5, "AMSB": 5, "IPRP": 5, "RCEX": 7}
# 注意: CLM=5 优先于 FWCLM/WCLM 的 name rank 6, 否则 claims 配额会被
# "Index of Claims" / "Claims Worksheet" 索引类文书占用, 含完整权利要求的 Claims 全文落选
# (US 案实测: 配额 1 选中 FWCLM 索引, CLM 全文从未下载, 导致授权权利要求缺失)
# 注意：列表顺序即匹配优先级；申请人关键词须先于驳回关键词，
# 否则 "Amendment ... After Non-Final Rejection" 会被 "non-final" 抢匹配
NAME_RANK = [
    (0, r"notice\s+of\s+allowance|decision\s+to\s+grant|登録査定|등록결정|intention\s+to\s+grant"
        r"|text\s+intended\s+for\s+grant|issue\s+notification"),
    (5, r"amendment|request\s+for\s+reconsideration"
        r"|written\s+opinion|意見書|答弁|보정|補正|reply|response|remarks|argument"),
    (2, r"non[- ]final|reasons?\s+(for|of)\s+refusal|拒絶理由|거절이유|notification\s+of\s+reasons"
        r"|communication\s+(from|under|pursuant)"),  # EP Art.94(3) 实审意见通知书
    (1, r"final\s+rejection|deemed\s+to\s+be\s+withdrawn|拒絶査定|거절결정|notice\s+of\s+abandonment"),
    (3, r"892|list\s+of\s+references\s+cited\s+by\s+examiner"),
    (4, r"search\s+report|search\s+strategy|search\s+information|引用文献|인용문헌"),
    (6, r"claims"),
]


def _doc_rank(d: dict) -> int:
    code = (d.get("docCode") or "").upper().split("-")[0]
    name = (d.get("docDesc") or "").lower()
    best = 99
    if code in CODE_RANK:
        best = min(best, CODE_RANK[code])
    for rk, pat in NAME_RANK:
        if re.search(pat, name):
            best = min(best, rk)
            break
    return best


def _doc_family(d: dict) -> str:
    code = (d.get("docCode") or "").upper().split("-")[0]
    name = (d.get("docDesc") or "").lower()
    if re.search(r"notice\s+of\s+allowance|decision\s+to\s+grant|decision\s+(on|of|for)\s+registration"
                 r"|登録査定|등록결정|intention\s+to\s+grant|text\s+intended\s+for\s+grant"
                 r"|issue\s+notification|patented", name):
        return "final_grant"
    # 申请人答复/修改优先于审查意见（"Amendment ... After Non-Final Rejection" 本质是答复）
    if re.search(r"amendment|remarks|written\s+opinion|意見書|答弁|의견|보정|補正"
                 r"|request\s+for\s+continued\s+examination|request\s+for\s+reconsideration"
                 r"|reply|response|argument", name):
        return "applicant"
    if re.search(r"final\s+rejection|notice\s+of\s+abandonment|deemed\s+to\s+be\s+withdrawn"
                 r"|拒絶査定|거절결정|decision\s+(of|on)\s+rejection|revocat"
                 r"|non[- ]final|reasons?\s+(for|of)\s+refusal|拒絶理由|거절이유", name):
        return "rejection"
    if re.search(r"list\s+of\s+references|search\s+report|search\s+strategy"
                 r"|search\s+information|引用文献|인용문헌|검색", name):
        return "citation"
    if code == "CLM" or re.search(r"\bclaims\b", name):
        return "claims"
    return "other"


def pick_exam_docs(docs: list[dict], max_docs: int) -> list[dict]:
    """按中国审查员关注点挑选文书：授权/驳回通知书 > 历次审查意见 > 对比文件清单
    (892/ISR/SRNT) > 申请人答复与修改 > 权利要求书。

    同一文档的翻译版/原文只取其一（优先英文翻译版，如 JP 的 "(TRANSLATED)"）。
    """
    if not docs:
        return []
    fam_docs = {f: [] for f in FAMILY_ORDER}
    for d in docs:
        fam_docs[_doc_family(d)].append(d)

    def sort_docs(arr):
        # 稳定排序：先按日期降序（真实日期），再按 (rank, 翻译版优先) 升序 → 同 rank 内日期新的在前
        # docCode 带 "-JP"/"-KR" 后缀的是日/韩原文（如 "A131-JP"），不带的是英文翻译版，
        # 英文翻译版优先（rapidocr 中英模型对日/韩文识别差，且英文版对审查员更友好）
        arr = sorted(arr, key=lambda d: _date_key(d.get("legalDateStr")), reverse=True)
        return sorted(arr, key=lambda d: (_doc_rank(d),
                      1 if (d.get("docCode") or "").upper().endswith(("-JP", "-KR")) else 0))

    picked, used_code = [], set()
    for fam in FAMILY_ORDER:
        if len(picked) >= max_docs:
            break
        quota = FAMILY_QUOTA[fam]
        if quota <= 0:
            continue  # 配额 0 的类别不占用名额（修复先取后减的 off-by-one）
        for d in sort_docs(fam_docs[fam]):
            if len(picked) >= max_docs:
                break
            code = (d.get("docCode") or "").upper().split("-")[0]
            if code in used_code:
                continue
            used_code.add(code)
            picked.append(d)
            quota -= 1
            if quota <= 0:
                break
    # 剩余名额按排名补足（同样按 docCode 去重，避免同类型文书重复入选）
    if len(picked) < max_docs:
        for d in sort_docs(docs):
            if len(picked) >= max_docs:
                break
            code = (d.get("docCode") or "").upper().split("-")[0]
            if d in picked or (code and code in used_code):
                continue
            used_code.add(code)
            picked.append(d)
    picked.sort(key=lambda d: _date_key(d.get("legalDateStr")))
    return picked


# ────────────────────────── 单局处理 ──────────────────────────
def process_member(gd: GDClient, member: dict, cfg: dict, cache_dir: Path,
                   progress=None, exclude_nos: frozenset = frozenset(),
                   cancel=None) -> dict:
    country, app_num, kind = member["countryCode"], member["appNum"], member["kindCode"]
    _check_cancel(cancel)
    log(f"\n===== {country} {app_num} (kind={kind}) =====")
    try:
        dl = gd.doclist(country, app_num, kind)
    except Exception as e:
        log(f"  文书列表失败: {e}")
        return {"office": country, "app_num": app_num, "error": str(e)[:120], "documents": []}
    docs = dl.get("docs", [])
    doc_number = dl.get("docNumber")
    picked = pick_exam_docs(docs, cfg["max_docs"])
    log(f"  文书共 {len(docs)} 份, 挑选实审相关 {len(picked)} 份")
    documents = []
    for d in picked:
        _check_cancel(cancel)  # 每份文书下载前的检查点（长循环内, 停止 10~30s 生效）
        name, did, pages = d["docDesc"], d["docId"], d.get("numberOfPages") or 1
        # 检索/报告类文书（JP 调查报告、EP/WO 检索报告、IPRP、US 892 引证清单等）：
        # 引用文献表格在文档后部，提高截断长度（默认 2 倍）保证文献清单进入文本
        is_ref_tail = bool(re.search(
            r"search\s+report|registered\s+search|international\s+(search|preliminary)"
            r"|search\s+strategy|search\s+information|list\s+of\s+references"
            r"|\b892\b|引用文献|인용문헌|검색|調査報告", name.lower()))
        ref_len = cfg["doc_text_len"] * 2 if is_ref_tail else cfg["doc_text_len"]
        # 长文本模式缓存键加 _long 后缀：避免命中旧截断长度的缓存（旧缓存不含尾部文献清单）
        cache_key = f"{country}_{app_num}_{did}" + ("_long" if is_ref_tail else "")
        pdf = cache_dir / f"{country}_{app_num}_{did}.pdf"
        log(f"  [{country}] {name} ({pages}p) 下载 ...")
        try:
            gd.download(country, app_num, doc_number, did, pages, pdf)
            size = pdf.stat().st_size
            log(f"    下载完成 {size//1024}KB, 抽取文本 ...")
        except Exception as e:
            log(f"    下载失败: {str(e)[:100]}")
            documents.append({"date": d.get("legalDateStr", ""), "name": name,
                              "code": d.get("docCode", ""), "type": detect_doc_type(name),
                              "pages": pages, "text": "", "error": str(e)[:100]})
            continue
        text = extract_text(pdf, backend=cfg["ocr"], api_key=cfg["mineru_key"],
                            cache_dir=cache_dir, cache_key=cache_key,
                            max_chars=ref_len, progress=log)
        # 保留换行结构（供 claims/引用清单正则匹配），仅折叠行内多余空格
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip()
        # 入库二次截断：检索/报告类文书尾部才是引用文献清单，保留尾部
        if len(text) > cfg["doc_text_len"]:
            text = text[-cfg["doc_text_len"]:] if is_ref_tail else text[:cfg["doc_text_len"]]
        if not text:
            text = "[无文本层且 OCR 失败]"
        # SRNT/SRWS 等检索报告里的号码是检索记录而非对比文件，跳过引用提取
        is_search_log = bool(re.search(r"search\s+(strategy|results?)", name.lower()))
        documents.append({
            "date": d.get("legalDateStr", ""),
            "name": name,
            "code": d.get("docCode", ""),
            "type": detect_doc_type(name),
            "pages": pages,
            "pdf": pdf.name,  # .cache 中的文件名，供 Web UI 打包审查文件
            "citations": [] if is_search_log else extract_citations(
                text, exclude_nos={app_num, *exclude_nos}),
            "claims": extract_claims(text)[:10],
            "conclusion": detect_conclusion(text, name),
            "text": text,
        })
    # 最终结论 = 按真实日期取最后一份"结论性"文书（授权/驳回/视为撤回）；
    # "审查中"/"审查中（已发出驳回理由）"不算结论性，避免 SRFW 等程序文书干扰
    DEFINITIVE = {"授权", "驳回", "视为撤回或放弃"}
    conclusion = "审查中"
    dated = [x for x in documents if x.get("text") and x.get("conclusion") in DEFINITIVE]
    if dated:
        dated.sort(key=lambda x: _date_key(x.get("date")))
        conclusion = dated[-1]["conclusion"]
    return {"office": country, "app_num": app_num, "kind_code": kind,
            "title": member.get("title", ""),
            "conclusion": conclusion, "documents": documents}


# ────────────────────────── 主流程 ──────────────────────────
def run_pipeline(query: str, cfg: dict, progress=None, cancel=None) -> dict:
    log = progress if callable(progress) else (lambda msg: None)
    _check_cancel(cancel)
    gd = GDClient()
    t0 = time.time()
    log(f"[1/4] 查询同族: {query}")
    members = gd.family(query)
    _check_cancel(cancel)  # 同族查询返回后的检查点
    offices = cfg["offices"]
    targets = [m for m in members if m.get("countryCode", "").upper() in offices]
    log(f"同族 {len(members)} 个, 关注国外局 {len(targets)} 个: "
        + ", ".join(f"{m['countryCode']} {m['appNum']}" for m in targets))
    if not targets:
        raise SystemExit("未找到国外局同族成员，请确认公开号或调整 --offices")

    cache_dir = Path(cfg["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 排除集：同族各成员申请号/公开号，避免优先权与同族号被误当对比文件
    exclude_nos = frozenset(
        n for m in members
        for n in (m.get("appNum", ""), m.get("pubNum", "")) if n)
    log("[2/4] 逐个国外局: 文书列表 → 下载 → OCR → 结构化提取")
    processed = []
    for i, m in enumerate(targets, 1):
        _check_cancel(cancel)  # 每局处理前的检查点
        log(f"\n[{i}/{len(targets)}] {m['countryCode']} {m['appNum']}")
        processed.append(process_member(gd, m, cfg, cache_dir, progress=log,
                                        exclude_nos=exclude_nos, cancel=cancel))

    log(f"\n[3/4] 汇总数据 (用时 {time.time() - t0:.0f}s)")
    data = {
        "query": query,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "family_members": members,
        "members": processed,
    }
    return data


# ────────────────────────── 大模型报告 ──────────────────────────
def llm_generate_report(data: dict, cfg: dict, progress=None, cancel=None) -> str:
    log = progress if callable(progress) else (lambda msg: None)
    _check_cancel(cancel)  # LLM 调用前的检查点（进入前可秒级停止）
    import openai
    env = load_env()
    api_base = (cfg.get("api_base") or os.environ.get("LLM_API_BASE")
                or env.get("LLM_API_BASE") or DEFAULT_LLM_BASE)
    api_key = (cfg.get("api_key") or os.environ.get("LLM_API_KEY")
               or env.get("LLM_API_KEY") or DEFAULT_LLM_KEY)
    model = (cfg.get("model") or os.environ.get("LLM_MODEL")
             or env.get("LLM_MODEL") or DEFAULT_LLM_MODEL)
    max_tokens = cfg.get("max_tokens") or DEFAULT_MAX_TOKENS
    # 调用参数配置项（优先级：cfg > 环境变量 LLM_* > 项目 .env > 默认常量）
    timeout = float(cfg.get("llm_timeout") or os.environ.get("LLM_TIMEOUT")
                    or env.get("LLM_TIMEOUT") or LLM_TIMEOUT)
    max_retries = int(cfg.get("llm_max_retries") or os.environ.get("LLM_MAX_RETRIES")
                      or env.get("LLM_MAX_RETRIES") or LLM_MAX_RETRIES)
    stream_flag = (cfg.get("llm_stream")
                   if cfg.get("llm_stream") is not None
                   else os.environ.get("LLM_STREAM") or env.get("LLM_STREAM") or LLM_STREAM)
    stream = str(stream_flag).strip().lower() not in ("", "0", "false", "no", "off")
    progress_step = int(cfg.get("llm_progress_step") or os.environ.get("LLM_PROGRESS_STEP")
                        or env.get("LLM_PROGRESS_STEP") or LLM_PROGRESS_STEP)
    log(f"  模型: {model}\n  接口: {api_base}")
    log(f"  参数: timeout={timeout}s, max_retries={max_retries}, "
        f"stream={'on' if stream else 'off'}, progress_step={progress_step}")
    client = openai.OpenAI(api_key=api_key, base_url=api_base,
                           timeout=timeout, max_retries=max_retries)

    # 通知书原文总量预算；深拷贝避免就地截断污染后续保存的 raw.json
    TEXT_BUDGET = 256000
    data = copy.deepcopy(data)
    used = 0
    for m in data["members"]:
        for d in m.get("documents", []):
            txt = (d.get("text") or "").strip()
            if not txt or txt.startswith("[") or len(txt) < 50:
                continue
            remain = TEXT_BUDGET - used
            if remain < 3000:
                # 余量不足时不再塞碎片文本，仅保留 citations/claims 结构化字段
                d["text"] = ""
                d["text_note"] = "超出文本预算未收录，仅提供结构化预提取"
                used = TEXT_BUDGET
            elif used + len(txt) > TEXT_BUDGET:
                d["text"] = txt[:remain] + " …[截断]"
                used = TEXT_BUDGET
            else:
                used += len(txt)

    system = (
        "你是一名资深的中华人民共和国专利审查员，同时精通美国(USPTO)、欧洲(EPO)、"
        "日本(JPO)、韩国(KIPO)的实审制度、法条与审查实践。"
        "请基于用户提供的 Global Dossier 国外局实审案卷数据，撰写一份"
        "《国外局实审过程分析报告》，帮助中国审查员快速掌握该同族在国外的审查脉络。"
        "核心要求："
        "1) 绝不编造。只引用数据中真实出现的文书名称、日期、法条、对比文件与权利要求；"
        "缺失信息明确标注'数据未提供'。"
        "2) 对比文件必须注明编号与类别(X/Y/A，参照各局惯例：US 的 PTO-892、EP 检索报告、"
        "JP 引用文献、KR 인용문헌)，并说明其被引用的理由(最接近现有技术/结合启示等)。"
        "3) 法条按各局规范表述：US 35 U.S.C. §102/§103/§112、EPC Art. 54/56/84、"
        "特許法第29条(新規性/進歩性)、특허법 제29조。"
        "4) 逐份通知书梳理：引用法条 → 引用对比文件 → 审查意见要点；"
        "随后整理申请人答复与权利要求修改；最后给出最终结论(授权/驳回/视为撤回等)，"
        "若授权则摘录授权的权利要求——**只需摘录独立权利要求**(不引用其他权利要求"
        "的权项，如 US 的 'What is claimed is' 中的第 1 条及不依赖前项的权项)。"
        "5) 输出 Markdown，表格与分节并用，可直接作为工作参考。")

    user = (f"以下是 Global Dossier 获取的国外局实审数据(JSON)。每个成员的 documents 按日期升序，"
            f"text 为通知书/答复/检索文书 OCR 文本(截取片段)，citations/claims/conclusion 为正则预提取结果。"
            f"报告落款日期使用 fetched_at。\n\n"
            f"{json.dumps(data, ensure_ascii=False, indent=1)}\n\n"
            f"请撰写中文 Markdown 报告，结构如下：\n"
            f"# 国外局实审过程分析报告（{data.get('query', '')}）\n\n"
            f"## 一、跨局对比与中国审查员参考\n"
            f"(各国驳回理由与对比文件使用方式的异同、对中国实审的参考要点；可先给出结论性概括)\n\n"
            f"## 二、历次通知书详解（引用法条/对比文件/审查意见）\n"
            f"(逐局逐份：引用法条、引用对比文件[编号/类别X·Y·A/专利号/被引用理由]、审查意见要点)\n\n"
            f"## 三、申请人答复与权利要求修改\n"
            f"(历次答复要点、修改的权利要求、争辩理由；数据未提供时如实说明)\n\n"
            f"## 四、同族与实审范围总览\n"
            f"(同族成员、各局申请号、关注范围说明、各局最终结论汇总表)\n\n"
            f"## 五、各局实审时间线\n"
            f"(每局按时间列出历次通知书/答复/检索文书，含日期、类型、状态标签)\n\n"
            f"## 六、最终审查结论\n"
            f"(每局最终状态：授权/驳回/视为撤回等；若授权，摘录授权的独立权利要求文本)\n")

    # 报告生成固定关闭思考：dots3 开启 thinking 后 content 为 None，内容在 reasoning_content
    # （且思考会耗尽 max_tokens，finish_reason=length，报告为空）
    extra = {"chat_template_kwargs": {"enable_thinking": False}}
    if stream:
        # 流式输出：首 token 即可开始回传进度，避免"卡住"假象
        resp_stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            extra_body=extra,
            stream=True,
        )
        parts, done, last_report = [], 0, 0
        for chunk in resp_stream:
            _check_cancel(cancel)  # 流式期间每 chunk 检查（生成中也秒级可停）
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content or ""
            if piece:
                parts.append(piece)
                done += len(piece)
                if done - last_report >= progress_step:
                    log(f"  [LLM 生成中] 已生成约 {done} 字 ...")
                    last_report = done
        return "".join(parts) or ""
    else:
        # 一次性返回（关闭流式时）：兜底 reasoning_content，防 thinking 模式返回空
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            extra_body=extra,
        )
        msg = resp.choices[0].message
        return msg.content or getattr(msg, "reasoning_content", None) or ""


# ────────────────────────── 输出与 CLI ──────────────────────────
def save_report(data: dict, report: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{data['query']}_国外局实审分析报告.md"
    md.write_text(report, encoding="utf-8")
    js = out_dir / f"{data['query']}_raw.json"
    js.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return md


def main():
    ap = argparse.ArgumentParser(description="Global Dossier 五局实审分析助手")
    ap.add_argument("query", help="CN 公开号，如 CN118076910")
    ap.add_argument("--offices", default=",".join(DEFAULT_OFFICES), help="关注的国外局，逗号分隔")
    ap.add_argument("--ocr", choices=["auto", "rapidocr", "mineru"], default="auto",
                    help="OCR 后端: auto=有 MINERU_API_KEY 用 mineru 否则 rapidocr")
    ap.add_argument("--mineru-key", default="", help="MinerU API Token（缺省自动读 .env/peizhi.txt）")
    ap.add_argument("--max-docs", type=int, default=8, help="每局最多下载的实审文书数")
    ap.add_argument("--doc-text-len", type=int, default=25666, help="每份文书抽取字符上限")
    ap.add_argument("--out-dir", default="reports", help="报告输出目录")
    ap.add_argument("--cache-dir", default=".cache", help="PDF/OCR 缓存目录")
    ap.add_argument("--no-llm", action="store_true", help="只抓取+提取，不生成大模型报告")
    ap.add_argument("--api-base", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument("--thinking", action="store_true", help="开启大模型思考模式")
    args = ap.parse_args()

    cfg = {
        "offices": {o.strip().upper() for o in args.offices.split(",") if o.strip()},
        "ocr": args.ocr, "mineru_key": resolve_mineru_key(args.mineru_key),
        "max_docs": args.max_docs, "doc_text_len": args.doc_text_len,
        "cache_dir": args.cache_dir, "out_dir": args.out_dir,
        "api_base": args.api_base, "api_key": args.api_key, "model": args.model,
        "max_tokens": args.max_tokens, "thinking": args.thinking,
    }
    q = args.query.strip()
    # 大小写不敏感 + 完整匹配（允许可选 kind 后缀，如 CN117460982A）
    if not re.fullmatch(r"(?i)(CN)?\d{6,12}[A-Z]?\d*", q):
        print("请输入有效 CN 公开号，如 CN118076910 或 CN117460982A")
        sys.exit(1)
    q = q.upper()
    display = q if q.startswith("CN") else "CN" + q
    q = q[2:] if q[:2] == "CN" else q  # API 路径已含 /CN/，去掉前缀
    q = re.sub(r"[A-Z]\d*$", "", q)  # 去掉公开号 kind 后缀（如 CN117460982A → 117460982）

    data = run_pipeline(q, cfg)
    data["query"] = display
    if args.no_llm:
        out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
        js = out / f"{display}_raw.json"
        js.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n原始数据已保存: {js}")
        return
    log("\n[4/4] 大模型生成报告 ...")
    report = llm_generate_report(data, cfg, progress=log)
    md = save_report(data, report, Path(args.out_dir))
    log(f"\n报告已保存: {md}")


if __name__ == "__main__":
    main()
