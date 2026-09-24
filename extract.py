#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实审文书结构化提取（正则优先，供大模型报告引用）
================================================
从 OCR 文本中预提取：
- 引用的对比文件（国家前缀 + 编号 + 类别 X/Y/A）
- 权利要求文本（"What is claimed is" / "CLAIMS" / 特許請求の範囲 等段落）
- 审查结论（授权/驳回/视为撤回或放弃/审查中，优先依据文书名称）
- 文书类型判定（审查意见 / 申请人答复 / 检索相关）
"""
import re

# ── 对比文件引用（(正则, 国家代码)）────────────────────────────────
# 编号允许千分位逗号与连字符，输出统一为 "US 11579456 B2" 形式
PATENT_NO_PATTERNS = [
    # US：兼容授权号 US 11,579,456 B2、公开申请号 US 2019/0204592 A1、
    # 通知书正文写法 "US PUB 2019/0204592"、892 表连写 US-20190204592-A1
    (r"US[-－\s]?(?:[Pp][Uu][Bb](?:lication|LICATION)?\.?[-－\s]?)?"
     r"(?:Patent\s)?(?:No\.?\s?)?(\d{4}/\d{6,7}|[\d,]{5,13})\s?[-－]?\s?(B\d?|A1?|C\d?)?", "US"),
    # EP 检索报告等表格中的空格分组编号：US 5 186 197 A
    (r"US[-－\s]?(\d{1,3}(?:[-－\s]\d{3}){1,2})\s?[-－]?\s?(B\d?|A1?|C\d?)?", "US"),
    (r"EP[-－\s]?(\d{5,9})\s?[-－]?\s?(A1?|B1?)?", "EP"),
    (r"JP[-－\s]?(?:特開|特許|公開)?\s?(\d{4}[-－/]?\d{3,6})\s?[-－]?\s?(A|B)?", "JP"),
    (r"(?:特開|特許公開)\s?(\d{4}[-－]?\d{3,6})", "JP"),
    # KR：公开/注册号 10-2007-0000209，及 EP 检索报告表格中的空格分组编号
    # （KR 2007 0000209 U、KR 200 445 241 Y1，实用新型 U / 注册 Y1）
    (r"KR[-－\s]?(?:공개특허\s?)?(?:제)?\s?"
     r"(\d{2,4}(?:[-－\s]\d{3,8}){1,2}|\d{4,13})"
     r"\s?[-－]?\s?(U\d?|Y\d?|A\d?|B\d?)?", "KR"),
    (r"CN[-－\s]?(\d{9,12})\s?[A-Z]?", "CN"),
    (r"WO[-－\s]?(\d{4}[-／]\d{3,6})", "WO"),
    (r"(?:WO|PCT)[-－\s]?(\d{4}/?\d{6,7})", "WO"),
]

CATEGORY_PATTERNS = [r"<td>\s*([XYA])\s*</td>",  # EP/WO 检索报告表格的 category 列
                     r"Category[：: ]*([XYA])\b", r"類別[：: ]*([XYA])\b",
                     r"\b(X|Y|A)\b\s*(?:A\.?|B\.?|is cited|considered)"]


def extract_citations(text: str, max_items: int = 40, exclude_nos: tuple = ()) -> list[dict]:
    """提取文中出现的对比文件引用，输出带国家前缀与类别。

    参数:
      exclude_nos: 需排除的编号（如当前申请号自身，避免把本申请当对比文件）
    返回: [{no, category, kind, snippet}]
    """
    ex = {re.sub(r"[\s,/\-－]", "", str(x)) for x in exclude_nos}
    out, seen = [], set()
    for pat, country in PATENT_NO_PATTERNS:
        for m in re.finditer(pat, text):
            g = m.groups()
            # 归一化：去空格/逗号/连字符/斜杠（如 "5 186 197" → "5186197"）
            num = re.sub(r"[\s,/\-－]", "", g[0] or "")
            if not num or num in ex:
                continue
            kind = (g[1] if len(g) > 1 else "") or ""
            key = (country, num, kind)
            if key in seen:
                continue
            seen.add(key)
            ctx = text[max(0, m.start() - 80):m.end() + 80]
            cat = ""
            for cp in CATEGORY_PATTERNS:
                cm = re.search(cp, ctx)
                if cm:
                    cat = cm.group(1)
                    break
            out.append({"no": f"{country} {num}", "kind": kind, "category": cat,
                        "snippet": re.sub(r"\s+", " ", ctx).strip()[:160]})
            if len(out) >= max_items:
                return out
    return out


# ── 权利要求提取 ─────────────────────────────────────────────────
CLAIM_HEADERS = [
    r"What\s+is\s+claimed\s+is\s*:",
    r"^#*\s*\[?(?:CLAIMS|Patent\s+Claims)\]?\s*$",   # US CLM 文档；兼容 MinerU 的 "## [CLAIMS]"
    r"Claims\s*\n?\s*1\.\s",
    r"特許請求の範囲",
    r"청구항\s*1",
    r"权利要求\s*[1１]",
    r"^#*\s*\[Claim\s*1\]\s*$",   # JP/KR 翻译版 Claims 直接以 "[Claim 1]" 开头（无标题行）
]
# 截断标记：常见章节结束/页脚/引用文献表（在权利要求块之后）
CLAIM_CUTS = [r"\n\s*Form PTO", r"\n\s*Attorney Docket", r"\n\s*CERTIFICATE OF",
              r"\n\s*ABSTRACT", r"\n\s*[A-Z][A-Z ]{12,}\s*\n"]  # 大写章节标题

# 从属权利要求特征：引用其他权利要求的表述 → 据此过滤出独立权利要求
CLAIM_REF_PATTERNS = [
    r"\baccording\s+to\s+claim\s*\d",
    r"\b(as\s+)?(?:claimed|set\s+forth|defined|recited)\s+in\s+claim\s*\d",
    r"\bof\s+(?:any\s+)?(?:the\s+)?claims?\s*\d",
    r"\bof\s+any\s+preceding\s+claim",
    r"\bclaim\s*\d+\s+of\b",
    r"請求項\s*[1-9]",
    r"請求の範囲\s*(?:第)?[1-9]",
    r"第\s*[1-9]\s*項",
    r"제\s*[1-9]\s*항",
    r"항\s*제?\s*[1-9]",
    r"(?:根据|如|按)权利要求\s*[1-9]",
    r"如請求項\s*[1-9]",
]


def extract_claims(text: str, max_claims: int = 30, independent_only: bool = True) -> list[str]:
    """提取独立权利要求文本块（找首个权利要求起始标记，截取至下个大节）。

    独立权利要求 = 不引用其他权利要求的权项（默认过滤掉从属权利要求）。
    要求输入保留换行结构（OCR 文本行之间以 \n 分隔）。
    """
    for hdr in CLAIM_HEADERS:
        m = re.search(hdr, text, re.IGNORECASE | re.MULTILINE)
        if not m:
            continue
        start = m.start()
        end = len(text)
        for c in CLAIM_CUTS:
            cm = re.search(c, text[start + len(m.group(0)):])
            if cm:
                end = min(end, start + len(m.group(0)) + cm.start())
        block = text[start:end]
        claims = []
        # 兼容两种编号格式："1. xxx" / "1) xxx" 与 MinerU 输出的 "[Claim N]"/"## [Claim N]" 标记
        item_re = re.compile(
            r"(?:^|\n)\s*(?:"
            r"(\d{1,3})[\.\)]\s+"
            r"|#*\s*\[?\s*Claim\s*(\d{1,3})\s*\]?\s*[\.:]?\s*"
            r")(.{8,6000}?)(?=\n\s*(?:"
            r"\d{1,3}[\.\)]\s+"
            r"|#*\s*\[?\s*Claim\s*\d{1,3}\s*\]?\s*[\.:]?\s*"
            r")|$)",
            re.S)
        for cm in item_re.finditer(block):
            num = cm.group(1) or cm.group(2)
            claim = f"{num}. " + re.sub(r"\s+", " ", cm.group(3)).strip()
            if independent_only and any(re.search(p, claim, re.IGNORECASE) for p in CLAIM_REF_PATTERNS):
                continue
            claims.append(claim)
            if len(claims) >= max_claims:
                break
        if claims:
            return claims
    return []


# ── 审查结论判定 ─────────────────────────────────────────────────
# 名称强信号（文书名比 OCR 文本可靠）
GRANT_NAME = re.compile(
    r"notice\s+of\s+allowance|decision\s+to\s+grant|decision\s+(on|of|for)\s+registration"
    r"|登録査定|등록결정|intention\s+to\s+grant|text\s+intended\s+for\s+grant|issue\s+notification|patented"
    r"|rule\s+71\(3\)")
REJECT_NAME = re.compile(
    r"final\s+rejection|拒絶査定|거절결정|decision\s+(?:of|on)\s+rejection|revocation|revoked")
WITHDRAW_NAME = re.compile(
    r"notice\s+of\s+abandonment|deemed\s+to\s+be\s+withdrawn|abandoned|みなし取下げ|포기")
PENDING_NAME = re.compile(
    r"non[- ]final|reasons?\s+(for|of)\s+refusal|拒絶理由|거절이유|notice\s+of\s+reasons"
    r"|office\s+action|communication\s+from\s+the\s+examin")
# 文本强信号：只用"最终驳回"级措辞，避免非最终驳回正文 "claims are rejected" 误判
GRANT_TEXT = re.compile(
    r"notice\s+of\s+allowance|decision\s+to\s+grant|登録査定|등록결정|intention\s+to\s+grant"
    r"|decision\s+(on|of|for)\s+registration|text\s+intended\s+for\s+grant")
REJECT_TEXT = re.compile(
    r"final\s+rejection|decision\s+of\s+rejection|拒絶査定|거절결정")
# 非最终驳回正文特征 → 审查中（已发出驳回理由）
PENDING_TEXT = re.compile(
    r"claims?[^.\n]{0,80}?(?:are|is|have\s+been)\s+rejected")
WITHDRAW_TEXT = re.compile(
    r"notice\s+of\s+abandonment|deemed\s+to\s+be\s+withdrawn|abandoned|みなし取下げ")


def detect_conclusion(text: str, doc_name: str = "") -> str:
    """返回: 授权 / 驳回 / 视为撤回或放弃 / 审查中（已发出驳回理由） / 审查中"""
    n = (doc_name or "").lower()
    # 1) 文书名称优先（最可靠）；"Non-Final Rejection" 含 "final rejection" 子串，
    #    故 non-final 判定须先于 final
    if GRANT_NAME.search(n):
        return "授权"
    if PENDING_NAME.search(n):
        return "审查中（已发出驳回理由）"
    if REJECT_NAME.search(n):
        return "驳回"
    if WITHDRAW_NAME.search(n):
        return "视为撤回或放弃"
    # 2) OCR 文本信号：仅当文书名不含"答复/检索"特征时使用——
    #    答复/检索文书里的 "deemed to be withdrawn" 等多为后果提醒而非结论
    if re.search(r"amendment|reply|response|remarks|written\s+opinion|search\s+report"
                 r"|list\s+of\s+references|意見|보정|補正|答弁|의견|search\s+strategy", n):
        return "审查中"
    low = re.sub(r"in condition for allowance", " ", text.lower())
    if GRANT_TEXT.search(low):
        return "授权"
    if REJECT_TEXT.search(low):
        return "驳回"
    if WITHDRAW_TEXT.search(low):
        return "视为撤回或放弃"
    # 非最终驳回正文（"claims are rejected"）→ 审查中（已发出驳回理由）
    if PENDING_TEXT.search(low):
        return "审查中（已发出驳回理由）"
    return "审查中"


# ── 文书类型判定 ─────────────────────────────────────────────────
OA_PATTERNS = [r"office\s*action", r"non[- ]final", r"final\s+rejection",
               r"notice\s+of\s+allowance", r"notice\s+of\s+abandonment",
               r"intention\s+to\s+grant", r"text\s+intended\s+for\s+grant",
               r"notification\s+of\s+(the\s+)?reasons?\s*for\s*refusal",
               r"decision\s+(on|of|for)\s+(registration|rejection)",
               r"decision\s+to\s+grant", r"reasons?\s+for\s+refusal",
               r"communication\s+(from|under|pursuant)",  # EP Art.94(3)/R.71(3) 通知书
               r"examination\s+started|examination\s+procedure",
               r"拒絶理由通知", r"拒絶査定", r"登録査定", r"査定",
               r"의견제출통지", r"거절이유", r"거절결정", r"등록결정", r"결정"]
APPLICANT_PATTERNS = [r"amendment", r"reply", r"response", r"argument", r"remarks",
                      r"written\s+opinion", r"request\s+for\s+continued\s+examination",
                      r"request\s+for\s+reconsideration",
                      r"意見書", r"答弁書", r"補正書", r"手続補正", r"意見",
                      r"의견서", r"보정서", r"답변서", r"의견"]
SEARCH_PATTERNS = [r"search\s+report", r"international\s+search", r"list\s+of\s+references",
                   r"892", r"srn|srfw", r"search\s+strategy", r"引用文献", r"인용문헌", r"검색"]


def detect_doc_type(name: str) -> str:
    """文书类型：applicant(申请人答复) > office_action(审查意见) > search(检索相关)。

    注意顺序：如 "Amendment ... After Non-Final Rejection" 含 Non-Final，
    但本质是申请人答复，须先判 applicant。
    """
    n = name.lower()
    if re.search("|".join(APPLICANT_PATTERNS), n):
        return "applicant"
    if re.search("|".join(OA_PATTERNS), n):
        return "office_action"
    if re.search("|".join(SEARCH_PATTERNS), n):
        return "search"
    return "other"
