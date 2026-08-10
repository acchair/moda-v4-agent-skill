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
from tools.scoring.announcement_rules import catalyst_summary, capex_event_summary, extract_announcement_events
OUTPUT_BASE = ROOT / "knowledge/research/announcements"
ANNOUNCEMENT_PAGE_SIZE = 100
ANNOUNCEMENT_MAX_PAGES = 5
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


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
    }).copy()
    for column in ("date", "title", "type", "url"):
        if column not in renamed.columns:
            renamed[column] = ""
    renamed["date"] = pd.to_datetime(renamed["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return renamed[["date", "title", "type", "url"]].fillna("")


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
    reduction = bool(re.search(r"(?:控股股东|实际控制人|实控人)[^。；\n]{0,35}减持|减持[^。；\n]{0,35}(?:控股股东|实际控制人|实控人)", title_text))
    increase = bool(re.search(r"(?:控股股东|实际控制人|实控人)[^。；\n]{0,35}增持|增持[^。；\n]{0,35}(?:控股股东|实际控制人|实控人)", title_text))
    fetch_ok = ann_data.get("announcement_fetch_ok") is True
    coverage_complete = ann_data.get("announcement_coverage_complete") is True
    controller_checked = fetch_ok and coverage_complete
    controller_action = "reduction" if reduction else "increase" if increase else "stable" if controller_checked else None
    qa_text = " ".join(f"{item.get('question', '')} {item.get('answer', '')}" for item in irm_data.get("qa_list", []))
    growth_matches = re.findall(r"(?:订单|新增订单)[^\n。]{0,40}?(?:同比(?:增幅)?|增长)[^\d]{0,8}([0-9]+(?:\.[0-9]+)?)%", qa_text)
    announcement_events = extract_announcement_events(ann_list)
    catalyst_data = catalyst_summary(announcement_events)
    capex_data = capex_event_summary(announcement_events)
    ann_state = ann_data.get("fetch_state", "failed")
    qa_state = irm_data.get("fetch_state", "failed")
    module_state = "failed" if "failed" in {ann_state, qa_state} else "fallback_ok" if "fallback_ok" in {ann_state, qa_state} else "empty" if {ann_state, qa_state} == {"empty"} else "ok"
    structured = {
        "announcement_titles": titles,
        "announcement_events": announcement_events,
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
