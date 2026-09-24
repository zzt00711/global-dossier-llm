#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 后端：本地 rapidocr / MinerU 云端 API
=========================================
- rapidocr : 本地 CPU 推理，零配置，速度较慢（15 页通知书约 200s），结果可缓存
- mineru   : MinerU 精准解析 API（v4，需 MINERU_API_KEY），云上高精度 OCR，
             适合扫描件较多的通知书（对比文件编号、引证表格识别更好）

自动选择: --ocr auto 时，若设置了 MINERU_API_KEY 则用 mineru，否则 rapidocr。
文本按 PDF 内容 md5 缓存到 cache_dir，重复运行不重复 OCR。
"""
import re
import time
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ────────────────────────── 本地 rapidocr 后端 ──────────────────────────
_rapid_engine = None


def _get_rapid_engine():
    global _rapid_engine
    if _rapid_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_engine = RapidOCR()
    return _rapid_engine


def ocr_rapidocr(pdf_path: Path, max_chars: int = 25666) -> str:
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        text = "\n".join(page.get_text() for page in doc)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) >= 50:
            return text
        engine = _get_rapid_engine()
        # 大文档（如 EP "Text intended for grant" 82 页）的权利要求/结论在文档末尾，
        # 从头截断会取不到；故 >20 页时倒序 OCR，优先命中结尾关键内容。
        pages = list(range(len(doc) - 1, -1, -1)) if len(doc) > 20 else range(len(doc))
        parts = []
        for i in pages:
            pix = doc[i].get_pixmap(dpi=150)
            result, _ = engine(pix.tobytes("png"))
            parts.append("\n".join(r[1] for r in result) if result else "")
            if len("\n".join(parts)) >= max_chars:
                break
        text = re.sub(r"[ \t]+", " ", "\n".join(parts))  # 保留换行结构
        return re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip()
    finally:
        doc.close()


# ────────────────────────── MinerU 云端后端 ──────────────────────────
def ocr_mineru(pdf_path: Path, api_key: str, max_chars: int = 25666,
               language: str = "en") -> str:
    """MinerU 精准解析 API（v4），经官方 SDK 上传→解析→取 markdown。

    依赖: pip install mineru-open-sdk；需在 https://mineru.net 申请 Token。
    超长文本首尾各保留一段（权利要求/授权结论通常在文档末尾）。
    """
    from mineru import MinerU

    client = MinerU(api_key)
    try:
        result = client.extract(str(pdf_path), model="vlm", ocr=True,
                                formula=True, table=True, language=language)
        text = getattr(result, "markdown", "") or ""
    finally:
        client.close()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip()
    if len(text) > max_chars:
        head = max_chars * 2 // 3
        text = text[:head] + "\n…[中段截断]…\n" + text[-(max_chars - head):]
    return text


# MinerU language 参数（日韩文通知书需对应语种模型）
MINERU_LANG = {"JP": "japan", "KR": "korea", "CN": "ch", "TW": "ch"}


# ────────────────────────── 统一入口（带逻辑键缓存） ──────────────────────────
def extract_text(pdf_path: Path, backend: str = "auto",
                 api_key: str = "", cache_dir: Path | None = None,
                 cache_key: str = "", max_chars: int = 25666,
                 country: str = "", progress=None) -> str:
    """按 backend 提取 PDF 文本，结果缓存。

    注意：Global Dossier 下载的 PDF 每次都带新的 CreationDate 元数据，
    文件内容 md5 会随下载而变化，因此缓存键必须用稳定逻辑键（如
    'US_17948887_J8D7EF3SRXEAPX3'），不能按 PDF 内容哈希。
    缓存文件名含后端标识，避免切换后端后读到旧后端的结果。
    """
    log = progress if callable(progress) else (lambda msg: None)
    # 先解析实际后端（auto 依赖 api_key 是否可用），再定缓存文件名
    if backend == "auto":
        backend = "mineru" if api_key else "rapidocr"
    cache_file = None
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # v2: 保留换行结构的文本；旧版单行折叠缓存不再使用
        cache_file = cache_dir / f"ocr_v2_{backend}_{cache_key}.txt"
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")
        # 兼容早期不含后端标识的缓存（内容为纯文本，可直接复用）
        legacy = cache_dir / f"ocr_v2_{cache_key}.txt"
        if legacy.exists():
            text = legacy.read_text(encoding="utf-8")
            if cache_key:
                cache_file.write_text(text, encoding="utf-8")
            return text

    t0 = time.time()
    if backend == "mineru":
        if not api_key:
            raise RuntimeError("MinerU 后端需要 MINERU_API_KEY（--mineru-key 或环境变量）")
        lang = MINERU_LANG.get(country.upper(), "en")
        text = ocr_mineru(pdf_path, api_key, max_chars, language=lang)
        log(f"      [OCR:mineru/{lang}] {time.time() - t0:.0f}s")
    else:
        text = ocr_rapidocr(pdf_path, max_chars)
        log(f"      [OCR:rapidocr] {time.time() - t0:.0f}s")

    if cache_file is not None and text and cache_key:
        cache_file.write_text(text, encoding="utf-8")
    return text
