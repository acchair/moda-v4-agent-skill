"""
公告 + 互动易数据模块
====================
采集个股最新公告(easy_tdx/CNINFO)和投资者互动问答(AKShare/CNINFO)，
输出结构化 Markdown 报告供莫大分析参考。

用法:
    python3 tools/akshare/announcements.py --stock 002466 --name 天齐锂业
    python3 tools/akshare/announcements.py --stock 002466,603290 --days 7
"""
import time, sys, os, argparse, json, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/akshare"))

# ══ 反限流 ══
from anti_rate_limit import apply_patch
apply_patch()

import akshare as ak
import pandas as pd
import requests
from tools.data_call import run_with_timeout
from tools.scoring.announcement_rules import (
    catalyst_summary,
    capex_event_summary,
    classify_controller_action,
    extract_announcement_events,
    extract_corporate_action_events,
)
OUTPUT_BASE = ROOT / "knowledge/research/announcements"
ANNOUNCEMENT_PAGE_SIZE = 100
ANNOUNCEMENT_MAX_PAGES = 5
ANNUAL_REPORT_TIMEOUT_SECONDS = 30
ANNUAL_REPORT_MAX_PAGES = 80
ANNUAL_REPORT_MAX_TEXT_CHARS = 350_000
ANNUAL_REPORT_TITLE_PATTERN = re.compile(r"(?P<year>20\d{2})\s*年\s*年度报告")
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


def fetch_pdf_document(*args, **kwargs):
    """Reuse the bounded public-PDF reader without importing it at module startup."""
    from tools.scoring.web_research import fetch_pdf_document as fetch_document

    return fetch_document(*args, **kwargs)


def _normalize_irm_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    return frame.rename(columns={
        "问题": "问题", "mainContent": "问题",
        "回答内容": "回答内容", "attachedContent": "回答内容",
        "提问时间": "提问时间", "pubDate": "提问时间",
        "更新时间": "更新时间", "updateDate": "更新时间",
        "提问者": "提问者", "authorName": "提问者",
        "来源": "来源", "pubClient": "来源",
    }).copy()


def _fetch_cninfo_irm_http(code: str, timeout: int = 12, scheme: str = "https") -> pd.DataFrame:
    """Use CNINFO's public endpoints without browser state or credentials."""
    if scheme not in {"http", "https"}:
        raise ValueError(f"不支持的互动易协议: {scheme}")
    base_url = f"{scheme}://irm.cninfo.com.cn"
    headers = {"User-Agent": "Mozilla/5.0"}
    org_response = requests.post(
        f"{base_url}/newircs/index/queryKeyboardInfo",
        params={"_t": "1691144074"},
        data={"keyWord": code},
        timeout=timeout,
        headers=headers,
    )
    org_response.raise_for_status()
    rows = (org_response.json().get("data") or [])
    if not rows or not rows[0].get("secid"):
        raise ValueError("CNINFO 互动易未返回组织代码")
    params = {
        "_t": "1691142650", "stockcode": code, "orgId": rows[0]["secid"],
        "pageSize": "1000", "pageNum": "1", "keyWord": "", "startDay": "", "endDay": "",
    }
    response = requests.post(f"{base_url}/newircs/company/question", params=params, timeout=timeout, headers=headers)
    response.raise_for_status()
    payload = response.json()
    total_page = min(int(payload.get("totalPage") or 1), 10)
    frames = [pd.DataFrame(payload.get("rows") or [])]
    for page in range(2, total_page + 1):
        params["pageNum"] = str(page)
        response = requests.post(f"{base_url}/newircs/company/question", params=params, timeout=timeout, headers=headers)
        response.raise_for_status()
        frames.append(pd.DataFrame(response.json().get("rows") or []))
    return _normalize_irm_frame(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame())


def _qa_payload(frame: pd.DataFrame, *, fetch_state: str, source_chain: list[dict], error: str = "") -> dict:
    if frame is None or frame.empty:
        return {"total": 0, "qa_list": [], "fetch_state": fetch_state, "source_chain": source_chain, "error": error or None}
    qa_list = []
    for _, row in frame.iterrows():
        question = str(row.get("问题", "")) if pd.notna(row.get("问题")) else ""
        answer = str(row.get("回答内容", "")) if pd.notna(row.get("回答内容")) else "(未回复)"
        qa_list.append({
            "q_time": str(row.get("提问时间", ""))[:10],
            "a_time": str(row.get("更新时间", ""))[:10],
            "question": question.strip(),
            "answer": answer.strip() or "(未回复)",
            "asker": str(row.get("提问者", "匿名")),
            "source": str(row.get("来源", "")),
        })
    qa_list.sort(key=lambda x: x["q_time"], reverse=True)
    return {"total": len(qa_list), "qa_list": qa_list, "fetch_state": fetch_state, "source_chain": source_chain, "error": error or None}


def fetch_irm_qa(code: str, name: str = None) -> dict:
    """获取巨潮资讯互动易问答列表"""
    print(f"  [互动易] 获取 {name or code} 的问答 ...")
    primary = run_with_timeout("互动易", lambda: _normalize_irm_frame(ak.stock_irm_cninfo(symbol=code)), seconds=15, source="AKShare/stock_irm_cninfo")
    if primary.ok:
        frame = primary.value if isinstance(primary.value, pd.DataFrame) else pd.DataFrame()
        state = "empty" if frame.empty else "ok"
        print(f"  [互动易] {'无数据' if frame.empty else f'✅ 共 {len(frame)} 条问答'}")
        return _qa_payload(frame, fetch_state=state, source_chain=primary.source_chain or [])
    print(f"  [互动易] AKShare失败: {primary.error}")
    fallback = run_with_timeout("互动易", lambda: _fetch_cninfo_irm_http(code, scheme="https"), seconds=15, source="CNINFO/public HTTPS")
    if fallback.ok:
        frame = fallback.value if isinstance(fallback.value, pd.DataFrame) else pd.DataFrame()
        state = "fallback_ok" if not frame.empty else "empty"
        return _qa_payload(frame, fetch_state=state, source_chain=(primary.source_chain or []) + (fallback.source_chain or []))
    plain_http = run_with_timeout("互动易", lambda: _fetch_cninfo_irm_http(code, scheme="http"), seconds=15, source="CNINFO/public HTTP")
    chain = (primary.source_chain or []) + (fallback.source_chain or []) + (plain_http.source_chain or [])
    if plain_http.ok:
        frame = plain_http.value if isinstance(plain_http.value, pd.DataFrame) else pd.DataFrame()
        state = "fallback_ok" if not frame.empty else "empty"
        return _qa_payload(frame, fetch_state=state, source_chain=chain)
    return _qa_payload(
        pd.DataFrame(),
        fetch_state="failed",
        source_chain=chain,
        error=f"{primary.error}; HTTPS fallback: {fallback.error}; HTTP fallback: {plain_http.error}",
    )


def _normalize_announcement_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    renamed = frame.rename(columns={
        "公告日期": "date", "公告标题": "title", "公告类型": "type", "公告链接": "url",
        "发布时间": "date", "标题": "title", "链接": "url", "类别": "type",
        "pdfUrl": "pdf_url", "PDF链接": "pdf_url", "PDF地址": "pdf_url",
    }).copy()
    for column in ("date", "title", "type", "url", "pdf_url"):
        if column not in renamed.columns:
            renamed[column] = ""
    renamed["date"] = pd.to_datetime(renamed["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return renamed[["date", "title", "type", "url", "pdf_url"]].fillna("")


def _fetch_cninfo_announcements_ak(code: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    return _normalize_announcement_frame(ak.stock_zh_a_disclosure_report_cninfo(
        symbol=code,
        market="沪深京",
        start_date=cutoff.strftime("%Y%m%d"),
        end_date=datetime.now().strftime("%Y%m%d"),
    ))


def fetch_announcements(code: str, name: str = None, days: int = 7) -> dict:
    """单次查询个股公告：easy_tdx/CNINFO 分页 → AKShare/CNINFO。"""
    days = max(1, int(days))
    print(f"  [公告] CNINFO 分页查询近{days}天公告 ({code}) ...")
    ann_list: list[dict] = []
    error = ""
    fetch_ok = False
    coverage_complete = False
    cutoff = pd.Timestamp(datetime.now().date() - timedelta(days=days - 1))
    source_chain: list[dict] = []
    used_fallback = False
    from tools.providers.easy_tdx_provider import fetch_announcements as fetch_cninfo

    for page in range(1, ANNOUNCEMENT_MAX_PAGES + 1):
        primary = run_with_timeout(
            "公告分页",
            lambda page=page: _normalize_announcement_frame(fetch_cninfo(code, count=ANNOUNCEMENT_PAGE_SIZE, page=page)),
            seconds=12,
            source="easy_tdx/CNINFO",
        )
        source_chain.extend(primary.source_chain or [])
        if primary.ok:
            frame = primary.value if isinstance(primary.value, pd.DataFrame) else pd.DataFrame()
        else:
            fetch_ok = False
            print(f"  [公告] easy_tdx/CNINFO失败: {primary.error}")
            fallback = run_with_timeout(
                "公告",
                lambda: _fetch_cninfo_announcements_ak(code, cutoff),
                seconds=15,
                source="AKShare/stock_zh_a_disclosure_report_cninfo",
            )
            source_chain.extend(fallback.source_chain or [])
            if not fallback.ok:
                error = f"{primary.error}; fallback: {fallback.error}"
                break
            frame = fallback.value if isinstance(fallback.value, pd.DataFrame) else pd.DataFrame()
            used_fallback = True
            fetch_ok = True
            coverage_complete = True

        fetch_ok = True
        if frame.empty:
            coverage_complete = True
            break
        raw_dates = pd.to_datetime(frame["date"], errors="coerce")
        for _, row in frame[raw_dates >= cutoff].iterrows():
            ann_list.append({
                "date": str(row.get("date", ""))[:10],
                "title": str(row.get("title", "")).strip(),
                "type": str(row.get("type", "")).strip(),
                "url": str(row.get("url", "")).strip(),
                "pdf_url": str(row.get("pdf_url", "")).strip(),
            })
        valid_dates = raw_dates.dropna()
        if used_fallback or len(frame) < ANNOUNCEMENT_PAGE_SIZE or (not valid_dates.empty and valid_dates.min() <= cutoff):
            coverage_complete = True
            break

    ann_list.sort(key=lambda item: item["date"], reverse=True)
    deduplicated: dict[tuple[str, str, str], dict] = {}
    for item in ann_list:
        key = (item.get("date", ""), item.get("title", ""), item.get("url", ""))
        deduplicated.setdefault(key, item)
    ann_list = list(deduplicated.values())
    if fetch_ok:
        print(f"  [公告] {'AKShare/CNINFO备用' if used_fallback else 'easy_tdx/CNINFO'}: {len(ann_list)} 条")
    state = "failed" if not fetch_ok else "fallback_ok" if used_fallback else "ok"
    if fetch_ok and not ann_list:
        state = "empty"

    return {
        "total": len(ann_list),
        "ann_list": ann_list,
        "days": days,
        "error": error,
        "source": "AKShare/stock_zh_a_disclosure_report_cninfo" if used_fallback else "easy_tdx/CNINFO",
        "announcement_fetch_ok": fetch_ok,
        "announcement_coverage_complete": coverage_complete,
        "fetch_state": state,
        "source_chain": source_chain,
    }


def _annual_report_candidate(item: dict) -> dict | None:
    """Return a normalized annual-report candidate, excluding summaries and notices."""
    title = str(item.get("title") or "").strip()
    match = ANNUAL_REPORT_TITLE_PATTERN.search(title)
    if not match or "摘要" in title or "更正公告" in title:
        return None
    pdf_url = str(item.get("pdf_url") or "").strip()
    if pdf_url.startswith("http://static.cninfo.com.cn/"):
        pdf_url = "https://" + pdf_url.removeprefix("http://")
    if not pdf_url:
        return None
    return {
        "title": title,
        "fiscal_year": int(match.group("year")),
        "publication_date": str(item.get("date") or "")[:10],
        "corrected": "更正后" in title,
        "url": pdf_url,
    }


def select_latest_annual_report(announcements: list[dict]) -> dict | None:
    """Pick the newest full annual report, preferring a corrected version in the same year."""
    candidates = [candidate for item in announcements if (candidate := _annual_report_candidate(item))]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (item["fiscal_year"], int(item["corrected"]), item["publication_date"], item["url"]),
    )


def _page_for_offset(text: str, offset: int) -> int:
    return text[:max(0, offset)].count("\f") + 1


def _first_match(text: str, *patterns: str) -> re.Match | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return match
    return None


def _decimal(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def _extract_annual_report_facts(text: str) -> dict:
    """Extract only explicit, disclosure-grade facts from a statutory annual report.

    This deliberately does not infer order growth, utilization, market share or
    industry supply/demand from narrative wording. Those remain separate gaps.
    """
    facts: dict[str, object] = {}
    source_pages: dict[str, int] = {}

    controller_match = _first_match(
        text,
        r"实际控制人(?:为|：|是)?\s*(?P<name>[\u4e00-\u9fff]{2,5})(?:先生|女士)",
    )
    shareholder_match = _first_match(
        text,
        r"控股股东(?:为|：|是)?\s*(?P<name>[\u4e00-\u9fffA-Za-z0-9（）()]{2,40}?(?:集团|公司|有限公司|股份有限公司))",
    )
    if controller_match or shareholder_match:
        control_chain: dict[str, str] = {}
        if shareholder_match:
            control_chain["controlling_shareholder"] = shareholder_match.group("name").strip()
            source_pages["control_chain"] = _page_for_offset(text, shareholder_match.start())
        if controller_match:
            control_chain["actual_controller"] = controller_match.group("name").strip()
            source_pages.setdefault("control_chain", _page_for_offset(text, controller_match.start()))
        if "稀散金属" in text and ("全产业链" in text or "关键材料" in text):
            control_chain["industrial_support"] = "年报披露控股股东具备稀散金属与半导体关键材料产业资源"
        facts["control_chain"] = control_chain

    bismuth_match = _first_match(
        text,
        r"(?:全年|本期)?\s*铋(?:相关)?(?:材料)?(?:业务)?实现(?:销售|营业)?收入\s*(?P<revenue>[0-9,.]+)\s*亿元\s*[，,]\s*占(?:整体)?营业收入的?\s*(?P<ratio>[0-9.]+)\s*%",
    )
    exact_bismuth_revenue = _first_match(text, r"铋材料销售\s*(?P<revenue>[0-9,]+(?:\.[0-9]+)?)")
    entity_match = _first_match(text, r"公司\s*旗下\s*(?P<entity>[\u4e00-\u9fffA-Za-z0-9（）()]{2,30}?)(?:全面发力|成为|开展)")
    if bismuth_match or exact_bismuth_revenue or entity_match:
        bismuth: dict[str, object] = {}
        if entity_match:
            bismuth["entity"] = entity_match.group("entity").strip()
            source_pages["bismuth_business"] = _page_for_offset(text, entity_match.start())
        if "唯一的铋金属深加工及化合物" in text or "唯一的铋金属深加工及化合物产品" in text:
            bismuth["position"] = "集团旗下唯一的铋金属深加工及化合物产品业务平台"
        if exact_bismuth_revenue and (value := _decimal(exact_bismuth_revenue.group("revenue"))) is not None:
            bismuth["revenue_cny"] = value
            source_pages.setdefault("bismuth_business", _page_for_offset(text, exact_bismuth_revenue.start()))
        elif bismuth_match and (value := _decimal(bismuth_match.group("revenue"))) is not None:
            bismuth["revenue_cny"] = value * 100_000_000
            bismuth["revenue_precision"] = "年报叙述按亿元披露"
            source_pages.setdefault("bismuth_business", _page_for_offset(text, bismuth_match.start()))
        if bismuth_match and (value := _decimal(bismuth_match.group("revenue"))) is not None:
            bismuth["revenue_reported_100m"] = value
        if bismuth_match and (value := _decimal(bismuth_match.group("ratio"))) is not None:
            bismuth["revenue_ratio"] = value / 100
        facts["bismuth_business"] = bismuth

    base_names = ("广东清远", "安徽五河", "湖北荆州", "浙江衢州")
    base_start = text.find(base_names[0])
    if base_start >= 0:
        base_context = text[max(0, base_start - 100):base_start + 260]
        if all(name in base_context for name in base_names) and any(term in base_context for term in ("生产基地", "布局", "基地")):
            facts["production_bases"] = list(base_names)
            source_pages["production_bases"] = _page_for_offset(text, base_start)

    if "凯世通" in text and "离子注入机" in text:
        ion_implant: dict[str, object] = {"entity": "凯世通", "product": "离子注入机"}
        customer_order = _first_match(text, r"新增\s*(?P<count>\d+)\s*家客户订单")
        if customer_order:
            ion_implant["new_customer_order_count"] = int(customer_order.group("count"))
            source_pages["ion_implant_orders"] = _page_for_offset(text, customer_order.start())
        delivery = _first_match(text, r"(?:全年)?实现\s*(?P<summary>10\s*多台12\s*英寸离子注入\s*机交付)")
        if delivery:
            ion_implant["delivery_summary"] = re.sub(r"\s+", "", delivery.group("summary"))
            source_pages.setdefault("ion_implant_orders", _page_for_offset(text, delivery.start()))
        acceptance = _first_match(text, r"验收数量超\s*(?P<count>\d+)\s*台")
        if acceptance:
            ion_implant["acceptance_over_units"] = int(acceptance.group("count"))
            source_pages.setdefault("ion_implant_orders", _page_for_offset(text, acceptance.start()))
        if len(ion_implant) > 2:
            facts["ion_implant_orders"] = ion_implant

    specialized_match = _first_match(text, r"凯世通.{0,160}?国家级专精特新[“\"']?小巨人[”\"']?企业")
    if specialized_match:
        facts["specialized"] = {
            "entity": "凯世通",
            "qualification": "国家级专精特新小巨人企业",
            "scope": "子公司",
        }
        source_pages["specialized"] = _page_for_offset(text, specialized_match.start())

    domestic_match = _first_match(text, r"境内(?:主营业务)?(?:收入)?\s*(?P<revenue>[0-9,]+(?:\.[0-9]+)?)")
    overseas_match = _first_match(text, r"境外(?:主营业务)?(?:收入)?\s*(?P<revenue>[0-9,]+(?:\.[0-9]+)?)")
    domestic = _decimal(domestic_match.group("revenue")) if domestic_match else None
    overseas = _decimal(overseas_match.group("revenue")) if overseas_match else None
    if overseas is not None:
        overseas_fact: dict[str, object] = {"value_cny": overseas, "period": "FY"}
        if domestic is not None and domestic + overseas > 0:
            overseas_fact["domestic_value_cny"] = domestic
            overseas_fact["ratio_pct"] = round(overseas / (domestic + overseas) * 100, 4)
        facts["overseas_revenue"] = overseas_fact
        source_pages["overseas_revenue"] = _page_for_offset(text, overseas_match.start())

    cashflow_match = _first_match(text, r"经营活动产生的现金流量\s*净额(?:为|：)?\s*(?P<cashflow>-?[0-9,]+(?:\.[0-9]+)?)")
    if cashflow_match and (value := _decimal(cashflow_match.group("cashflow"))) is not None:
        facts["operating_cashflow"] = {"value_cny": value, "period": "FY"}
        source_pages["operating_cashflow"] = _page_for_offset(text, cashflow_match.start())

    if source_pages:
        facts["source_pages"] = source_pages
    return facts


def extract_latest_annual_report(announcements: list[dict]) -> dict:
    """Fetch the selected annual report and retain explicit facts for evidence merging."""
    candidate = select_latest_annual_report(announcements)
    if not candidate:
        return {
            "status": "需人工确认",
            "fetch_status": "annual_report_not_found",
            "reason": "公告窗口内未找到可下载的完整年度报告 PDF",
        }

    result: dict[str, object] = {
        "status": "需人工确认",
        "title": candidate["title"],
        "fiscal_year": candidate["fiscal_year"],
        "report_period": f"{candidate['fiscal_year']}-12-31",
        "publication_date": candidate["publication_date"],
        "corrected": candidate["corrected"],
        "url": candidate["url"],
        "max_pages": ANNUAL_REPORT_MAX_PAGES,
    }
    try:
        fetch_status, text = fetch_pdf_document(
            candidate["url"],
            ANNUAL_REPORT_TIMEOUT_SECONDS,
            max_pages=ANNUAL_REPORT_MAX_PAGES,
            max_text_chars=ANNUAL_REPORT_MAX_TEXT_CHARS,
        )
    except Exception as exc:
        result["fetch_status"] = f"{type(exc).__name__}"
        result["reason"] = "年度报告 PDF 提取失败，需人工确认"
        return result

    result["fetch_status"] = fetch_status
    if fetch_status != "ok":
        result["reason"] = "年度报告 PDF 未能在受限时间和页数内提取，需人工确认"
        return result

    result["text_pages"] = len([page for page in text.split("\f") if page.strip()])
    facts = _extract_annual_report_facts(text)
    for key, value in facts.items():
        if key in {"overseas_revenue", "operating_cashflow"} and isinstance(value, dict):
            value = {**value, "period": f"FY{candidate['fiscal_year']}"}
        result[key] = value
    if facts:
        result["status"] = "已验证"
    else:
        result["reason"] = "年度报告已读取，但未识别到可安全结构化的字段，需人工确认"
    return result


def extract_keywords_from_qa(qa_list: list) -> dict:
    """从问答中提取关注主题和关键词"""
    if not qa_list:
        return {}

    from collections import Counter
    import re

    # 高频关注主题
    topic_keywords = {
        "产能/投产": ["产能", "投产", "产线", "扩产", "产量"],
        "订单/客户": ["订单", "客户", "供应", "合作", "配套"],
        "业绩/利润": ["业绩", "利润", "营收", "盈利", "增长"],
        "分红/回购": ["分红", "回购", "派息", "回报"],
        "股东人数": ["股东人数", "股东户数", "股东数"],
        "技术/研发": ["技术", "研发", "专利", "产品"],
        "行业/政策": ["行业", "政策", "补贴", "监管"],
        "股价/市值": ["股价", "市值", "涨", "跌", "估值"],
        "并购/重组": ["收购", "并购", "重组", "整合"],
        "风险/诉讼": ["风险", "诉讼", "处罚", "退市"],
    }

    topic_count = Counter()
    for qa in qa_list:
        text = qa["question"] + " " + qa["answer"]
        for topic, kws in topic_keywords.items():
            if any(kw in text for kw in kws):
                topic_count[topic] += 1

    return {
        "hot_topics": topic_count.most_common(5),
        "unanswered": sum(1 for q in qa_list if "(未回复)" in q["answer"]),
    }


def generate_report(code: str, name: str, irm_data: dict, ann_data: dict) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ann_list = list(ann_data.get("ann_list", []))
    titles = [str(item.get("title", "")) for item in ann_list]
    title_text = " ".join(titles)
    fetch_ok = ann_data.get("announcement_fetch_ok") is True
    coverage_complete = ann_data.get("announcement_coverage_complete") is True
    controller_checked = fetch_ok and coverage_complete
    controller_action = classify_controller_action(titles) or ("stable" if controller_checked else None)
    qa_text = " ".join(f"{item.get('question', '')} {item.get('answer', '')}" for item in irm_data.get("qa_list", []))
    growth_matches = re.findall(r"(?:订单|新增订单)[^\n。]{0,40}?(?:同比(?:增幅)?|增长)[^\d]{0,8}([0-9]+(?:\.[0-9]+)?)%", qa_text)
    announcement_events = extract_announcement_events(ann_list)
    corporate_action_events = extract_corporate_action_events(ann_list)
    catalyst_data = catalyst_summary(announcement_events)
    capex_data = capex_event_summary(announcement_events)
    annual_report = extract_latest_annual_report(ann_list)
    ann_state = ann_data.get("fetch_state", "failed")
    qa_state = irm_data.get("fetch_state", "failed")
    module_state = "failed" if "failed" in {ann_state, qa_state} else "fallback_ok" if "fallback_ok" in {ann_state, qa_state} else "empty" if {ann_state, qa_state} == {"empty"} else "ok"
    structured = {
        "announcement_titles": titles,
        "announcement_events": announcement_events,
        "corporate_action_events": corporate_action_events,
        "announcement_lookback_days": ann_data.get("days"),
        "announcement_fetch_ok": fetch_ok,
        "announcement_coverage_complete": coverage_complete,
        "fetch_state": module_state,
        "source_chain": {
            "announcements": ann_data.get("source_chain", []),
            "irm": irm_data.get("source_chain", []),
        },
        "qa_fetch_state": irm_data.get("fetch_state", "failed"),
        "qa_error": irm_data.get("error"),
        "controller_checked": controller_checked,
        "controller_action": controller_action,
        "annual_report": annual_report,
    }
    audit_risk = any(term in title_text for term in ("非标准审计", "保留意见", "无法表示意见", "否定意见", "退市风险警示"))
    if audit_risk or coverage_complete:
        structured["audit_risk"] = audit_risk
    if catalyst_data["catalyst_event_count"] or coverage_complete:
        structured.update(catalyst_data)
    structured.update(capex_data)
    if growth_matches:
        structured["order_growth"] = max(float(value) for value in growth_matches)

    lines = [
        f"# 公告与互动: {name or code}({code})",
        f"",
        f"> 采集时间: {ts}",
        f"> 数据源: easy_tdx/CNINFO → AKShare/CNINFO 公告；AKShare/CNINFO → CNINFO 公共 HTTP 互动易",
        f"> 公告覆盖: {'完整' if coverage_complete else '部分/失败，未据此反推无风险'}",
        f"",
        "---",
        f"",
        f"<!-- moda_announcements: {json.dumps(structured, ensure_ascii=False)} -->",
        f"",
    ]

    # ── 公告 ──
    lines.append("## 最新公告")
    lines.append("")
    ann_total = ann_data.get("total", 0)
    ann_days = ann_data.get("days", 7)
    if ann_total == 0:
        lines.append(f"近 {ann_days} 天无公告。")
    else:
        lines.append(f"近 {ann_days} 天共 **{ann_total}** 条公告：")
        lines.append("")
        lines.append("| 日期 | 类型 | 标题 |")
        lines.append("|------|------|------|")
        for a in ann_data.get("ann_list", [])[:20]:
            title = a["title"].replace("|", "/")[:60]
            lines.append(f"| {a['date']} | {a['type']} | [{title}]({a['url']}) |")
    lines.append("")

    # ── 年度报告 ──
    lines.append("## 年度报告证据")
    lines.append("")
    if annual_report.get("status") != "已验证":
        lines.append(f"需人工确认：{annual_report.get('reason', '未取得可用年度报告正文。')}")
    else:
        report_title = str(annual_report.get("title") or "年度报告")
        report_url = str(annual_report.get("url") or "")
        report_label = f"[{report_title}]({report_url})" if report_url else report_title
        lines.append(f"- 已提取：{report_label}（{annual_report.get('report_period', '期间待确认')}；{'更正后版本' if annual_report.get('corrected') else '原始版本'}）")
        control_chain = annual_report.get("control_chain") if isinstance(annual_report.get("control_chain"), dict) else {}
        if control_chain:
            lines.append(
                f"- 控制链：控股股东 {control_chain.get('controlling_shareholder', '待确认')}；"
                f"实际控制人 {control_chain.get('actual_controller', '待确认')}。"
            )
        bismuth = annual_report.get("bismuth_business") if isinstance(annual_report.get("bismuth_business"), dict) else {}
        if bismuth:
            revenue = bismuth.get("revenue_cny")
            ratio = bismuth.get("revenue_ratio")
            detail = []
            if revenue is not None:
                detail.append(f"收入 {float(revenue) / 100_000_000:.2f} 亿元")
            if ratio is not None:
                detail.append(f"占比 {float(ratio):.2%}")
            if bismuth.get("entity"):
                detail.insert(0, str(bismuth["entity"]))
            lines.append(f"- 铋业务：{'；'.join(detail) or '已披露，细节待人工确认'}。")
        bases = annual_report.get("production_bases")
        if isinstance(bases, list) and bases:
            lines.append(f"- 铋材料基地：{'、'.join(str(item) for item in bases)}。")
        specialized = annual_report.get("specialized") if isinstance(annual_report.get("specialized"), dict) else {}
        if specialized:
            lines.append(f"- 资质：{specialized.get('entity', '子公司')}获 {specialized.get('qualification', '资质待确认')}。")
        overseas = annual_report.get("overseas_revenue") if isinstance(annual_report.get("overseas_revenue"), dict) else {}
        if overseas:
            ratio = overseas.get("ratio_pct")
            ratio_text = f"，占地区主营 {float(ratio):.2f}%" if ratio is not None else ""
            lines.append(f"- FY 境外收入：{float(overseas.get('value_cny', 0)) / 100_000_000:.2f} 亿元{ratio_text}。")
    lines.append("")

    # ── 互动易 ──
    lines.append("## 投资者互动问答")
    lines.append("")
    qa_list = irm_data.get("qa_list", [])
    irm_total = irm_data.get("total", 0)

    if irm_total == 0:
        qa_state = irm_data.get("fetch_state", "failed")
        if qa_state == "empty":
            lines.append("接口成功但返回空结果，不能据此证明没有投资者问答。")
        else:
            lines.append("互动问答接口失败，不能据此证明没有投资者问答。")
    else:
        # 关键词分析
        kw = extract_keywords_from_qa(qa_list)
        if kw.get("hot_topics"):
            lines.append("### 热议主题")
            lines.append("")
            for topic, count in kw["hot_topics"]:
                bar = "█" * min(count, 10)
                lines.append(f"- {topic}: {bar} ({count}次)")
            lines.append("")

        unanswered = kw.get("unanswered", 0)
        if unanswered > 0:
            lines.append(f"> ⚠️ 有 {unanswered} 条问题未获回复")
            lines.append("")

        lines.append(f"### 最近问答 (共 {irm_total} 条)")
        lines.append("")

        # 只展示最近15条
        for i, qa in enumerate(qa_list[:15]):
            lines.append(f"#### Q{i+1}. {qa['q_time']} | {qa['asker']} ({qa['source']})")
            lines.append("")
            # 截断过长问题
            q_text = qa["question"][:200]
            if len(qa["question"]) > 200:
                q_text += "..."
            lines.append(f"> {q_text}")
            lines.append("")
            # 回答
            a_text = qa["answer"]
            if a_text == "(未回复)":
                lines.append("**⚠️ 尚未回复**")
            else:
                a_text = a_text[:300]
                if len(qa["answer"]) > 300:
                    a_text += "..."
                lines.append(f"**回复**: {a_text}")
            lines.append("")

    lines += [
        "---",
        "## 免责声明",
        "",
        "数据来自 easy_tdx、AKShare 和巨潮资讯网公开信息，仅供信息参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def process_stock(code: str, name: str = None, days: int = 7):
    if not name:
        name = code

    print(f"\n{'='*55}")
    print(f"  公告+互动易: {name}({code})  (近{days}天)")
    print(f"{'='*55}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        irm_future = executor.submit(fetch_irm_qa, code, name)
        ann_future = executor.submit(fetch_announcements, code, name, days)
        irm_data = irm_future.result()
        ann_data = ann_future.result()

    # 生成报告
    report = generate_report(code, name, irm_data, ann_data)
    outpath = OUTPUT_BASE / f"{code}.md"
    outpath.write_text(report, encoding="utf-8")
    print(f"  ✅ → {outpath}")
    return str(outpath)


def main():
    p = argparse.ArgumentParser(description="AKShare 公告+互动易采集模块")
    p.add_argument("--stock", required=True, help="股票代码，逗号分隔")
    p.add_argument("--name", help="股票名称")
    p.add_argument("--days", type=int, default=7, help="公告回溯天数 (默认7)")
    args = p.parse_args()

    codes = [c.strip() for c in args.stock.split(",")]
    for code in codes:
        try:
            process_stock(code, args.name, args.days)
        except Exception as e:
            print(f"[Error] {code}: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
