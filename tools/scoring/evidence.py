from __future__ import annotations

import csv
import json
from pathlib import Path
import re
from typing import Any

import yaml

from tools.scoring.announcement_rules import extract_announcement_events
from tools.scoring.classification_db import (
    SOURCE_LABEL as CLASSIFICATION_DB_SOURCE,
    has_category,
    lookup,
)


ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = ROOT / "knowledge" / "research"
SOURCE_LABELS = {
    "finance_data": "easy_tdx/TDX + easy_tdx/Sina + efinance/AKShare/Tencent",
    "business_data": "EastMoney/F10",
    "tdx_analysis": "easy_tdx/TDX",
    "announcements": "easy_tdx/CNINFO + AKShare/CNINFO",
    "market_events": "EastMoney + Sina",
    "popularity": "EastMoney/stockrank",
    "supply_demand": "AKShare/futures",
    "congestion": "乐咕乐股/申万二级行业拥挤度",
    "social_sentiment": "公开社交热榜 + 个股讨论接口/搜索 + 财经快讯",
    "macro_policy": "AKShare/PBOC + gov.cn",
    "web_research": "SearXNG + DuckDuckGo MCP",
    "industry_prosperity": "乐咕乐股(B级) + AKShare/申万",
}
REPORTS = tuple(SOURCE_LABELS)
COMMENT_PATTERN = re.compile(r"<!--\s*(moda_[a-z_]+):\s*(\{.*?\})\s*-->", re.S)

TRACK_GROUPS: dict[str, dict[str, Any]] = {
    "AI 算力与数据中心": {
        "sw_first": ("计算机", "通信", "电子"),
        "sw_second": ("计算机设备", "IT服务Ⅱ", "软件开发", "通信设备", "通信服务", "半导体", "元件", "光学光电子"),
        "terms": ("算力", "数据中心", "服务器", "液冷", "光模块", "高速互联", "AI电源", "人工智能", "大模型"),
        "require_terms": True,
    },
    "半导体国产替代": {
        "sw_first": ("电子",),
        "sw_second": ("半导体", "电子化学品Ⅱ", "元件", "光学光电子"),
        "terms": ("半导体", "芯片", "光刻", "电子特气", "硅片", "封装", "国产替代", "功率器件"),
    },
    "商业航天与军工": {
        "sw_first": ("国防军工",),
        "sw_second": ("航天装备Ⅱ", "航空装备Ⅱ", "地面兵装Ⅱ", "航海装备Ⅱ", "军工电子Ⅱ"),
        "terms": ("商业航天", "卫星", "火箭", "高温合金", "军工", "航空发动机", "低空经济"),
    },
    "新能源与储能": {
        "sw_first": ("电力设备",),
        "sw_second": ("光伏设备", "风电设备", "电池", "电网设备", "其他电源设备Ⅱ"),
        "terms": ("储能", "电网", "新能源", "锂电", "光伏", "风电", "充电桩", "逆变器"),
    },
    "汽车电动化与出海": {
        "sw_first": ("汽车",),
        "sw_second": ("汽车零部件", "乘用车", "商用车", "摩托车及其他"),
        "terms": ("汽车出海", "整车出口", "汽车出口", "海外市场", "新能源车", "新能源汽车", "电动车", "智能汽车"),
        "require_terms": True,
        "require_export_or_electrification": True,
    },
    "资源与周期": {
        "sw_first": ("有色金属", "煤炭", "石油石化", "钢铁"),
        "sw_second": ("工业金属", "贵金属", "小金属", "能源金属", "煤炭开采", "焦炭Ⅱ", "油气开采Ⅱ", "油服工程", "冶钢原料", "特钢Ⅱ", "航运港口"),
        "terms": ("锂矿", "铜矿", "金矿", "稀土", "有色", "煤炭", "石油", "天然气", "航运", "资源品", "涨价"),
    },
    "机器人与先进制造": {
        "sw_first": ("机械设备",),
        "sw_second": ("自动化设备", "通用设备", "专用设备", "电机Ⅱ", "工程机械"),
        "terms": ("机器人", "工业母机", "数控", "核心零部件", "自动化", "减速器", "伺服", "丝杠"),
        "require_terms": True,
    },
    "创新药与生命科学": {
        "sw_first": ("医药生物",),
        "sw_second": ("化学制药", "生物制品", "医疗器械", "医疗服务"),
        "terms": ("创新药", "生物制药", "生物医药", "ADC", "抗体", "临床", "药物", "基因测序", "多组学"),
        "require_terms": True,
    },
    "大健康与功能性消费": {
        "sw_first": ("食品饮料", "美容护理", "医药生物"),
        "sw_second": ("食品加工", "饮料乳品", "休闲食品", "个护用品", "医疗美容"),
        "terms": ("保健品", "功能性原料", "保健食品", "营养健康", "维生素", "健康食品", "功能饮料"),
        "require_terms": True,
    },
}
CAPEX_TERMS = ("资本开支", "扩产", "投产", "产能利用率", "新增订单", "在手订单", "设备投资", "产线建设")
LEADERSHIP_DIMENSION_PATTERNS = {
    "market_share_rank": (
        r"(?:市场份额|市场占有率|市占率)[^。；\n]{0,45}(?:第一|首位|排名|领先|[1-9]\d?(?:\.\d+)?%)",
        r"(?:销量|出货量|装机量|用户数|保有量|产量)[^。；\n]{0,32}(?:第一|首位|排名|领先|份额|市占率)",
    ),
    "sales_scale": (
        r"(?:销量|出货量|装机量|用户数|保有量|产量|资产规模|储量)[^。；\n]{0,45}(?:同比|达到|超过|领先|第一|排名|万|亿|%)",
        r"(?:全球|全国|国内|区域)[^。；\n]{0,35}(?:销量|出货量|装机量|用户数|产量|资产规模|储量)",
    ),
    "customer_supply": (
        r"(?:核心|关键|主要|指定)供应商[^。；\n]{0,45}(?:供货|配套|量产|定点|客户|供应链|订单)",
        r"(?:定点|量产供货|进入|配套)[^。；\n]{0,45}(?:供应链|客户|主机厂|头部客户|核心客户)",
    ),
    "technical_barrier": (
        r"(?:发明专利|核心专利|专利数量|核心技术|自主可控|不可替代|技术领先|技术优势)[^。；\n]{0,50}(?:领先|第一|核心|数量|标准|认证|产品)",
        r"(?:首创|独家|唯一|关键技术|核心工艺)[^。；\n]{0,45}(?:产品|量产|应用|客户|认证)",
    ),
    "license_standard": (
        r"(?:国家标准|行业标准|标准制定|牌照|批件|注册证|认证|资质)[^。；\n]{0,50}(?:牵头|参与|取得|获得|覆盖|核心|领先|第一)",
        r"(?:行业准入|许可|注册|认证)[^。；\n]{0,40}(?:覆盖|领先|核心|全国|区域)",
    ),
    "coverage_scale": (
        r"(?:全球|全国|国内|海外)[^。；\n]{0,40}(?:客户|渠道|网点|门店|用户|产能|基地|覆盖|布局|资产|储量)",
        r"(?:客户覆盖|渠道覆盖|销售网络|服务网络|生产基地)[^。；\n]{0,45}(?:全球|全国|国内|区域|数量|覆盖)",
    ),
}
LEADERSHIP_PROFILE_RULES = (
    ("医药/医疗", ("医药", "医疗", "创新药", "仿制药", "器械", "医院", "诊断", "生物")),
    ("金融", ("银行", "证券", "保险", "信托", "金融", "基金", "支付")),
    ("资源/能源/公用事业", ("煤炭", "有色", "稀土", "锂", "矿", "石油", "天然气", "电力", "燃气", "水务", "公用事业")),
    ("软件/平台/服务", ("软件", "计算机", "互联网", "平台", "云", "数据", "信息服务", "SaaS", "游戏", "传媒")),
    ("制造/消费", ("汽车", "电池", "光伏", "风电", "家电", "机械", "设备", "电子", "制造", "消费", "食品", "建材", "化工")),
)
SPECIALIZED_TERMS = ("专精特新", "单项冠军", "制造业冠军", "小巨人")
CATALYST_TERMS = ("中标", "重大合同", "新增订单", "订单增长", "扩产", "投产", "涨价", "回购", "增持", "业绩预增", "扭亏")
PROMOTION_TEMPLATE_TERMS = ("必涨", "稳赚", "翻倍", "内部消息", "主力建仓", "最后上车")
PAID_GROUP_TERMS = ("加群", "微信群", "VIP", "直播间", "收费群")
PERSONA_TERMS = ("老师带", "股神", "跟单", "操盘手")
RUMOR_TERMS = ("谣言", "辟谣", "澄清", "虚假", "操纵", "荐股骗局", "杀猪盘")


def _read_report(code: str, directory: str, since: float = 0) -> str | None:
    path = REPORT_ROOT / directory / f"{code}.md"
    if not path.exists() or (since and path.stat().st_mtime < since - 1):
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def read_reports(code: str, directories: tuple[str, ...] = REPORTS, since: float = 0) -> dict[str, str]:
    reports: dict[str, str] = {}
    for directory in directories:
        text = _read_report(code, directory, since)
        if text is not None:
            reports[directory] = text
    return reports


def _set(evidence: dict[str, Any], key: str, value: Any, source: str, *, overwrite: bool = False) -> None:
    if value is None or value == "":
        return
    if overwrite or key not in evidence:
        evidence[key] = value
    source_map = evidence.setdefault("metric_sources", {})
    sources = source_map.setdefault(key, [])
    if source and source not in sources:
        sources.append(source)


def _extract_comments(report: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for _, raw in COMMENT_PATTERN.findall(report):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_legacy_fields(evidence: dict[str, Any], reports: dict[str, str]) -> None:
    finance = reports.get("finance_data", "")
    source = SOURCE_LABELS["finance_data"]
    match = re.search(r"\|\s*行业\s*\|\s*([^|\n]+)", finance)
    if match:
        _set(evidence, "industry", match.group(1).strip(), source)

    tdx = reports.get("tdx_analysis", "")
    if tdx:
        source = SOURCE_LABELS["tdx_analysis"]
        match = re.search(r"当前评分\*\*:\s*([+-]?\d+(?:\.\d+)?)", tdx)
        if match:
            _set(evidence, "alpha_score", float(match.group(1)), source)
        match = re.search(r"位置=([0-9.]+)", tdx)
        if match:
            _set(evidence, "technical_position", float(match.group(1)), source)
        for label in ("清仓", "减仓", "加仓", "建仓"):
            if re.search(rf"\*\*[^\n]*{label}\*\*:\s*[^\n]*触发", tdx):
                _set(evidence, "technical_signal", label, source)
                break
        if "technical_signal" not in evidence:
            _set(evidence, "technical_signal", "中性/无触发", source)

    announcements = reports.get("announcements", "")
    if announcements:
        source = SOURCE_LABELS["announcements"]
        titles = re.findall(r"\|\s*\d{4}-\d{2}-\d{2}\s*\|[^|]*\|\s*\[([^]]+)]", announcements)
        if titles:
            _set(evidence, "announcement_titles", titles, source)
        growth_matches = re.findall(r"(?:订单|新增订单)[^\n。]{0,40}?(?:同比(?:增幅)?|增长)[^\d]{0,8}([0-9]+(?:\.[0-9]+)?)%", announcements)
        if growth_matches:
            _set(evidence, "order_growth", max(float(value) for value in growth_matches), source)


def _chain_match(evidence: dict[str, Any]) -> None:
    path = ROOT / "tools" / "scoring" / "chains.yaml"
    if not path.exists():
        return
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    industry_text = str(evidence.get("industry", "")).lower()
    primary_parts = [
        industry_text,
        str(evidence.get("main_business", "")),
        " ".join(str(item) for item in evidence.get("business_items", [])[:30]),
        str(evidence.get("security_name") or evidence.get("name") or ""),
    ]
    context = " ".join(primary_parts).lower()
    concept_context = " ".join(str(item) for item in evidence.get("concepts", [])[:30]).lower()
    if not context.strip():
        return
    candidates: list[dict[str, Any]] = []
    exact_alias_chains: list[dict[str, Any]] = []
    specific_terms = (
        "电子特气", "半导体特气", "光刻气", "光刻及其他混合气体",
        "氢化物", "氮氧化合物", "半导体材料",
    )
    for chain in raw.get("chains", []):
        chain_name = str(chain.get("name", ""))
        aliases = [str(item).lower() for item in chain.get("aliases", [])]
        primary_aliases = [alias for alias in aliases if alias and alias in context]
        exact_name_alias = any(
            alias and alias == str(evidence.get("security_name") or evidence.get("name") or "").strip().lower()
            for alias in aliases
        )
        alias_score = min(2.0, 0.5 * max(map(len, primary_aliases))) if primary_aliases else 0.25 if any(alias and alias in concept_context for alias in aliases) else 0
        if exact_name_alias:
            exact_alias_chains.append({"chain_name": chain_name, "score": 5.0})
        for stage in ("upstream", "midstream", "downstream"):
            data = chain.get(stage, {}) or {}
            industries = [str(item).lower() for item in data.get("industries", [])]
            keywords = [str(item).lower() for item in data.get("keywords", [])]
            stage_score = 2 * sum(item and item in industry_text for item in industries)
            stage_score += sum(item and item in context for item in keywords)
            if stage_score <= 0:
                continue
            primary_score = alias_score + stage_score
            if exact_name_alias:
                primary_score += 5.0
            score = primary_score + 0.25 * sum(item and item in concept_context and item not in context for item in keywords)
            keyword_hits = [item for item in keywords if item and item in context]
            specific_hits = [term for term in specific_terms if term.lower() in context and term.lower() in keywords]
            specificity = len(specific_hits) * 2 + len(keyword_hits)
            if any(term in chain_name.lower() for term in ("半导体", "电子特气")) and specific_hits:
                score += 3
                specificity += 3
            candidates.append({
                "score": score,
                "chain_name": chain_name,
                "stage": stage,
                "specificity": specificity,
                "stage_score": stage_score,
                "keyword_hits": keyword_hits,
                "specific_hits": specific_hits,
            })
    candidates.sort(key=lambda item: (item["score"], item["specificity"], len(item["specific_hits"])), reverse=True)
    best = candidates[0] if candidates else None
    exact_alias = max(exact_alias_chains, key=lambda item: item["score"], default=None)
    if exact_alias and (best is None or best["chain_name"] != exact_alias["chain_name"]):
        evidence["chain_name"] = exact_alias["chain_name"]
        evidence["chain_matches"] = [{
            "chain_name": exact_alias["chain_name"],
            "stage": None,
            "score": exact_alias["score"],
            "specific_hits": [],
            "keyword_hits": [],
        }]
        _set(evidence, "chain_name", exact_alias["chain_name"], "chains.yaml", overwrite=True)
        evidence["chain_match_types"] = ["行业主题命中"]
        evidence["chain_match_type"] = "行业主题命中"
        evidence["chain_partial"] = True
        evidence["business_match_partial"] = True
        evidence["business_match_reason"] = f"命中 {exact_alias['chain_name']} 主题别名，主营环节和收入占比待确认"
        return
    if best:
        source = "chains.yaml"
        _set(evidence, "chain_stage", best["stage"], source)
        _set(evidence, "chain_name", best["chain_name"], source)
        _set(evidence, "business_chain_match", min(1.0, best["score"] / 3), source)
        evidence["chain_matches"] = [
            {
                "chain_name": item["chain_name"],
                "stage": item["stage"],
                "score": round(item["score"], 2),
                "stage_score": round(item["stage_score"], 2),
                "specific_hits": item["specific_hits"],
                "keyword_hits": item["keyword_hits"][:8],
            }
            for item in candidates[:5]
        ]
        breakdown = evidence.get("business_breakdown") if isinstance(evidence.get("business_breakdown"), list) else []
        product_rows = [item for item in breakdown if item.get("category") == "按产品分类"]
        revenue_rows = product_rows or [item for item in breakdown if item.get("category") == "按行业分类"]
        matched_ratio = 0.0
        chain = next((item for item in raw.get("chains", []) if str(item.get("name")) == best["chain_name"]), {})
        stage_data = chain.get(best["stage"], {}) or {}
        revenue_terms = [str(item).lower() for item in stage_data.get("keywords", [])]
        for row in revenue_rows:
            item_name = str(row.get("item") or "").lower()
            ratio = _float(row.get("revenue_ratio"))
            if ratio is not None and any(term and term in item_name for term in revenue_terms):
                matched_ratio += ratio
        if matched_ratio > 0:
            _set(evidence, "business_chain_revenue_ratio", min(1.0, matched_ratio), "EastMoney/F10")
        revenue_confirmed = matched_ratio >= 0.30
        partial = best["score"] < 3 or not revenue_confirmed
        match_types = ["主营嵌入支持" if revenue_confirmed else "主营嵌入线索"]
        if best["stage"] == "upstream" and best["stage_score"] >= 3:
            match_types.append("产业链关键位置")
        evidence["chain_match_types"] = match_types
        evidence["chain_match_type"] = "、".join(match_types)
        evidence["chain_partial"] = partial
        evidence["business_match_partial"] = partial
        ratio_text = f"，主营收入支持 {matched_ratio:.1%}" if matched_ratio > 0 else "，缺少收入占比确认"
        specific_text = f"；具体命中：{'、'.join(best['specific_hits'])}" if best["specific_hits"] else ""
        evidence["business_match_reason"] = f"匹配 {best['chain_name']}，位置为 {best['stage']}{ratio_text}{specific_text}"


def _chokepoint_match(evidence: dict[str, Any]) -> None:
    path = ROOT / "tools" / "scoring" / "chokepoint_segments.csv"
    if not path.exists():
        return
    name = str(evidence.get("name", ""))
    context = " ".join([
        str(evidence.get("industry", "")),
        str(evidence.get("main_business", "")),
        " ".join(str(item) for item in evidence.get("business_items", [])[:30]),
    ])
    best: tuple[float, str, bool] | None = None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            stocks = str(row.get("key_stocks", ""))
            segment = str(row.get("segment_name", ""))
            exact = bool(name and name in stocks)
            contextual = bool(segment and segment in context)
            if not exact and not contextual:
                continue
            score = _float(row.get("chokepoint_score")) or 0
            candidate = (score, segment, not exact)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best:
        _set(evidence, "chokepoint_score", best[0], "chokepoint_segments.csv")
        evidence["chokepoint_segment"] = best[1]
        evidence["chokepoint_partial"] = best[2]


def _leadership_profile(context: str) -> tuple[str, tuple[str, ...]]:
    lowered = context.lower()
    for profile, terms in LEADERSHIP_PROFILE_RULES:
        if any(term.lower() in lowered for term in terms):
            return profile, terms
    return "综合行业", tuple()


def _leadership_evidence(evidence: dict[str, Any], reports: dict[str, str]) -> None:
    """Derive leadership from sector-appropriate evidence dimensions.

    Research headlines remain clues only. A score requires evidence in the
    structured company/industry reports, so a single promotional label cannot
    turn into a leadership conclusion.
    """
    if (
        evidence.get("classification_db_leadership") is True
        or evidence.get("classification_db_core_supplier") is True
    ):
        return
    context = " ".join([
        str(evidence.get("industry", "")),
        str(evidence.get("main_business", "")),
        " ".join(str(item) for item in evidence.get("business_items", [])[:30]),
    ])
    profile, profile_terms = _leadership_profile(context)
    expected_dimensions = {
        "医药/医疗": ("market_share_rank", "sales_scale", "technical_barrier", "license_standard", "customer_supply"),
        "金融": ("market_share_rank", "sales_scale", "license_standard", "coverage_scale", "customer_supply"),
        "资源/能源/公用事业": ("market_share_rank", "sales_scale", "customer_supply", "license_standard", "coverage_scale"),
        "软件/平台/服务": ("market_share_rank", "sales_scale", "customer_supply", "technical_barrier", "coverage_scale"),
        "制造/消费": ("market_share_rank", "sales_scale", "customer_supply", "technical_barrier", "coverage_scale"),
        "综合行业": tuple(LEADERSHIP_DIMENSION_PATTERNS),
    }[profile]
    # Industry/supply/macro reports describe the environment, not necessarily
    # the company. They remain secondary context and cannot independently
    # confirm company leadership.
    primary_directories = {"finance_data", "business_data", "announcements"}
    primary_sources = [
        (directory, report)
        for directory, report in reports.items()
        if directory in primary_directories and report
    ]
    secondary_sources = [
        (directory, report)
        for directory, report in reports.items()
        if directory not in primary_directories and directory != "web_research" and report
    ]
    dimensions: dict[str, dict[str, Any]] = {}
    for dimension, patterns in LEADERSHIP_DIMENSION_PATTERNS.items():
        primary_matches: list[dict[str, str]] = []
        secondary_matches: list[dict[str, str]] = []
        for directory, report in primary_sources + secondary_sources:
            for pattern in patterns:
                match = re.search(pattern, report, re.IGNORECASE)
                if not match:
                    continue
                snippet = re.sub(r"\s+", " ", match.group(0)).strip()
                record = {"source": directory, "signal": snippet[:120]}
                (primary_matches if directory in primary_directories else secondary_matches).append(record)
                break
        if primary_matches or secondary_matches:
            dimensions[dimension] = {
                "matched": bool(primary_matches),
                "primary_evidence": primary_matches[:3],
                "secondary_clues": secondary_matches[:3],
            }

    matched_dimensions = [
        dimension for dimension in expected_dimensions
        if dimensions.get(dimension, {}).get("matched") is True
    ]
    quantitative_dimensions = {"market_share_rank", "sales_scale"}
    relationship_dimensions = {"customer_supply", "coverage_scale"}
    primary_directories_used = sorted({
        item["source"]
        for value in dimensions.values()
        for item in value.get("primary_evidence", [])
    })
    clue_terms = [
        term for term in ("全球龙头", "行业龙头", "国内龙头", "核心供应商", "市场第一", "市占率第一", "行业领先", "全球领先", "隐形冠军")
        if term in " ".join(report for _, report in secondary_sources)
    ]
    if not matched_dimensions:
        if clue_terms:
            evidence["leadership_clues"] = clue_terms[:6]
            evidence["leadership_profile"] = profile
            evidence["leadership_missing_reason"] = (
                f"行业画像为{profile}，当前只有研报/行情标题线索（{'、'.join(clue_terms[:4])}），"
                "缺少市场份额、规模、客户供应关系、技术或资质的结构化证据"
            )
        return

    has_quantitative = bool(set(matched_dimensions) & quantitative_dimensions)
    has_relationship = bool(set(matched_dimensions) & relationship_dimensions)
    source_count = len(primary_directories_used)
    if len(matched_dimensions) >= 3 and (has_quantitative or has_relationship) and source_count >= 2:
        strength = 1.0
    elif len(matched_dimensions) >= 2 and (has_quantitative or has_relationship):
        strength = 0.75
    else:
        strength = 0.5
    missing_dimensions = [dimension for dimension in expected_dimensions if dimension not in matched_dimensions]
    dimension_labels = {
        "market_share_rank": "市场份额/排名",
        "sales_scale": "销量/出货/规模",
        "customer_supply": "客户/核心供应关系",
        "technical_barrier": "技术/专利壁垒",
        "license_standard": "牌照/批件/标准资质",
        "coverage_scale": "渠道/区域/资源覆盖",
    }
    matched_text = "、".join(dimension_labels[item] for item in matched_dimensions)
    missing_text = "、".join(dimension_labels[item] for item in missing_dimensions[:3]) or "无"
    reason = (
        f"行业画像={profile}；已确认维度：{matched_text}；"
        f"结构化来源 {source_count} 个；仍缺：{missing_text}"
    )
    _set(evidence, "leadership_profile", profile, "行业/主营结构化匹配")
    _set(evidence, "leadership_dimensions", dimensions, "公告/主营/行业结构化证据")
    _set(evidence, "leadership_dimension_count", len(matched_dimensions), "公告/主营/行业结构化证据")
    _set(evidence, "leadership_source_quality", "结构化披露", "公告/主营/行业结构化证据")
    _set(evidence, "leadership_strength", strength, "公告/主营/行业结构化证据")
    for directory in primary_directories_used:
        _set(evidence, "leadership_strength", strength, SOURCE_LABELS.get(directory, directory))
    evidence["leadership_reason"] = reason
    evidence["leadership_partial"] = strength < 1.0 or len(missing_dimensions) > 0


def _derive_framework_fields(evidence: dict[str, Any], reports: dict[str, str]) -> None:
    industry_context = str(evidence.get("industry", ""))
    business_context = " ".join([
        str(evidence.get("main_business", "")),
        " ".join(str(item) for item in evidence.get("business_items", [])[:30]),
    ])
    concept_context = " ".join(str(item) for item in evidence.get("concepts", [])[:30])
    structured_context = " ".join([industry_context, business_context])
    searchable_reports = [report for directory, report in reports.items() if directory != "web_research"]
    full_context = structured_context + " " + " ".join(searchable_reports)
    mapping = evidence.get("industry_mapping") if isinstance(evidence.get("industry_mapping"), dict) else {}
    mapping_status = str(mapping.get("status") or "不可用")
    sw_first = str(mapping.get("sw_first_name") or "")
    sw_second = str(mapping.get("sw_second_name") or "")

    def sw_matches(value: str, candidates: tuple[str, ...]) -> bool:
        normalized = value.replace("Ⅱ", "").strip()
        return bool(normalized and any(normalized == item.replace("Ⅱ", "").strip() for item in candidates))

    overseas_ratio = _float(evidence.get("overseas_revenue_ratio"))
    if overseas_ratio is not None and overseas_ratio > 1:
        overseas_ratio /= 100

    track_candidates: list[tuple[int, int, int, int, str, list[str], list[str], list[str]]] = []
    concept_clues: list[str] = []
    for label, rule in TRACK_GROUPS.items():
        terms = tuple(rule.get("terms") or ())
        industry_hits = [term for term in terms if term.lower() in industry_context.lower()]
        business_hits = [term for term in terms if term.lower() in business_context.lower()]
        concept_hits = [term for term in terms if term.lower() in concept_context.lower()]
        sw_first_hit = sw_matches(sw_first, tuple(rule.get("sw_first") or ()))
        sw_second_hit = sw_matches(sw_second, tuple(rule.get("sw_second") or ()))
        structured_hits = len(industry_hits) + len(business_hits)
        if rule.get("require_export_or_electrification"):
            export_or_electrification = bool(structured_hits) or (overseas_ratio is not None and overseas_ratio >= 0.20)
            if not export_or_electrification:
                if sw_first_hit or sw_second_hit or concept_hits:
                    concept_clues.append(label)
                continue
        if rule.get("require_terms") and structured_hits == 0:
            if sw_first_hit or sw_second_hit or concept_hits:
                concept_clues.append(label)
            continue
        primary_score = int(sw_second_hit) * 6 + int(sw_first_hit) * 3 + len(industry_hits) * 2 + len(business_hits)
        if primary_score:
            sw_hits = [item for item in (f"申万一级：{sw_first}" if sw_first_hit else "", f"申万二级：{sw_second}" if sw_second_hit else "") if item]
            track_candidates.append((
                primary_score, int(sw_second_hit), int(sw_first_hit), len(business_hits),
                label, industry_hits, business_hits, sw_hits,
            ))
        elif concept_hits:
            concept_clues.append(label)
    if track_candidates:
        _, second_hit, first_hit, business_count, dominant_track, industry_hits, business_hits, sw_hits = max(
            track_candidates, key=lambda item: item[:4]
        )
        industry_count = len(industry_hits)
        structured_count = industry_count + business_count
        if second_hit and structured_count:
            strength = 1.0
        elif first_hit and business_count >= 2:
            strength = 1.0
        elif second_hit or (first_hit and structured_count) or business_count >= 2:
            strength = 0.8
        else:
            strength = 0.6
        source = "申万行业映射 + 主营结构化匹配" if sw_hits and mapping_status in {"已验证", "部分覆盖"} else "行业/主营结构化匹配"
        _set(evidence, "track_strength", strength, source)
        evidence["dominant_track"] = dominant_track
        structured_labels = list(dict.fromkeys([*industry_hits, *business_hits]))
        reason_parts = [*sw_hits]
        if structured_labels:
            reason_parts.append(f"主营/行业：{'、'.join(structured_labels)}")
        evidence["track_reason"] = f"主导赛道：{dominant_track}（{'；'.join(reason_parts)}）"
        evidence["track_partial"] = strength < 1.0 or not sw_hits or mapping_status != "已验证"
    elif concept_clues:
        evidence["track_clues"] = list(dict.fromkeys(concept_clues))
        evidence["track_reason"] = "仅申万宽口径或概念板块提示可能赛道，缺少主营共同验证，不参与大时代赛道得分"

    order_growth = _float(evidence.get("order_growth"))
    announcement_items = [
        {"date": "", "title": str(item)}
        for item in evidence.get("announcement_titles", [])
        if str(item).strip()
    ]
    announcement_events = evidence.get("announcement_events")
    if not isinstance(announcement_events, list):
        announcement_events = extract_announcement_events(announcement_items)
    capex_events = [
        item for item in announcement_events
        if item.get("category") == "capacity" and item.get("hard_detail") is True
    ]
    # A dated event with hard details is stronger than a title word. Order
    # growth is supporting evidence, not capital-expenditure evidence by itself.
    company_capex_core_signals: list[str] = []
    company_capex_support_signals: list[str] = []
    if order_growth is not None and order_growth > 0:
        company_capex_support_signals.append("订单增长（辅助）")
    for key, label in (("capex_yoy", "资本开支同比"), ("construction_in_progress_yoy", "在建工程变化"),
                       ("fixed_asset_investment_yoy", "固定资产投资")):
        value = _float(evidence.get(key))
        if value is not None and value > 0:
            company_capex_core_signals.append(label)
    if capex_events:
        company_capex_core_signals.append(f"公告扩产/投产明确事件{len(capex_events)}项")
    company_capex_core_signals = list(dict.fromkeys(company_capex_core_signals))
    company_capex_support_signals = list(dict.fromkeys(company_capex_support_signals))
    company_capex = bool(company_capex_core_signals)
    evidence["company_capex_evidence_count"] = len(company_capex_core_signals)
    evidence["company_capex_confirmed"] = len(company_capex_core_signals) >= 2
    evidence["capex_event_count"] = len(capex_events)
    web_capex = evidence.get("web_industry_capex_validation") or {}
    if web_capex.get("status") == "已验证" and web_capex.get("signal") in {"上行", "下行"}:
        _set(evidence, "industry_capex_signal", web_capex["signal"], SOURCE_LABELS["web_research"], overwrite=True)
    industry_capex = evidence.get("industry_capex_signal") == "上行"
    industry_capex_down = evidence.get("industry_capex_signal") == "下行"
    evidence["capex_conflict"] = bool(industry_capex_down and company_capex)
    if company_capex and industry_capex and not evidence["capex_conflict"]:
        _set(evidence, "capex_strength", 1.0, SOURCE_LABELS["announcements"])
        _set(evidence, "capex_strength", 1.0, SOURCE_LABELS["web_research"])
        evidence["capex_reason"] = (
            f"公司侧 {len(company_capex_core_signals)} 类（{'、'.join(company_capex_core_signals)}）"
            "与行业资本开支双侧确认"
        )
        evidence["capex_partial"] = not evidence["company_capex_confirmed"]
    elif company_capex or industry_capex:
        source = SOURCE_LABELS["web_research"] if industry_capex else SOURCE_LABELS["announcements"]
        _set(evidence, "capex_strength", 0.5, source)
        detail = "、".join(company_capex_core_signals[:3]) if company_capex else "行业资本开支上行"
        if company_capex_support_signals and not company_capex:
            detail += f"；{'、'.join(company_capex_support_signals)}但未形成公司资本开支确认"
        evidence["capex_reason"] = f"仅单侧资本开支证据：{detail}"
        evidence["capex_partial"] = True
    elif company_capex_support_signals:
        evidence["capex_reason"] = (
            f"{'、'.join(company_capex_support_signals)}，但缺少资本开支、在建工程、固定资产投资或明确扩产事件"
        )
        evidence["capex_partial"] = True
    elif industry_capex_down:
        _set(evidence, "capex_strength", 0.0, SOURCE_LABELS["web_research"], overwrite=True)
        evidence["capex_reason"] = "行业资本开支下行，未确认资本开支浪潮"
        evidence["capex_partial"] = True

    prosperity = evidence.get("industry_prosperity_status")
    coverage = evidence.get("industry_prosperity_coverage")
    web_prosperity = evidence.get("industry_web_signal") if isinstance(evidence.get("industry_web_signal"), dict) else {}
    if prosperity:
        evidence["industry_prosperity_reason"] = f"行业景气 {prosperity}（{coverage or '覆盖未知'}）"
        if web_prosperity.get("status"):
            evidence["industry_prosperity_reason"] += f"；网络旁证 {web_prosperity['status']}（{web_prosperity.get('coverage', '覆盖未知')}，未核验）"
        for conflict in web_prosperity.get("conflicts", []) if isinstance(web_prosperity.get("conflicts"), list) else []:
            evidence.setdefault("industry_prosperity_conflicts", [])
            if conflict not in evidence["industry_prosperity_conflicts"]:
                evidence["industry_prosperity_conflicts"].append(conflict)
        if prosperity in {"走弱", "不可用"} or coverage != "完整":
            evidence["track_partial"] = True

    titles = [str(item) for item in evidence.get("announcement_titles", [])]
    title_text = " ".join(titles)
    reduction = re.search(r"(?:控股股东|实际控制人|实控人)[^。；\n]{0,35}减持|减持[^。；\n]{0,35}(?:控股股东|实际控制人|实控人)", title_text)
    increase = re.search(r"(?:控股股东|实际控制人|实控人)[^。；\n]{0,35}增持|增持[^。；\n]{0,35}(?:控股股东|实际控制人|实控人)", title_text)
    if reduction:
        _set(evidence, "controller_action", "reduction", SOURCE_LABELS["announcements"], overwrite=True)
    elif increase:
        _set(evidence, "controller_action", "increase", SOURCE_LABELS["announcements"], overwrite=True)

    if "st_risk" not in evidence:
        normalized_name = str(evidence.get("security_name") or evidence.get("name") or "").upper().replace(" ", "")
        if normalized_name and not normalized_name.isdigit():
            _set(evidence, "st_risk", normalized_name.startswith(("ST", "*ST")), "证券简称")
    audit_terms = ("非标准审计", "保留意见", "无法表示意见", "否定意见", "退市风险警示")
    if any(term in title_text for term in audit_terms):
        _set(evidence, "audit_risk", True, SOURCE_LABELS["announcements"])
    elif evidence.get("announcement_coverage_complete") is True:
        _set(evidence, "audit_risk", False, SOURCE_LABELS["announcements"])

    _leadership_evidence(evidence, reports)
    specialized_hits = [term for term in SPECIALIZED_TERMS if term in full_context]
    if specialized_hits and not evidence.get("classification_db_specialized"):
        evidence["specialized_clues"] = specialized_hits[:4]
        evidence["specialized_missing_reason"] = (
            "仅发现专精特新相关文字线索，名单数据库未命中，需通过权威网络来源核验"
        )

    event_list = evidence.get("announcement_events")
    if not isinstance(event_list, list):
        event_list = []
    if event_list or evidence.get("announcement_coverage_complete") is True:
        event_categories = sorted({str(item.get("category")) for item in event_list if item.get("category")})
        confirmed_events = [
            item for item in event_list
            if item.get("hard_detail") is True
        ]
        confirmed_categories = sorted({str(item.get("category")) for item in confirmed_events if item.get("category")})
        _set(evidence, "catalyst_event_count", len(event_list), SOURCE_LABELS["announcements"])
        _set(evidence, "catalyst_categories", event_categories, SOURCE_LABELS["announcements"])
        _set(evidence, "catalyst_confirmed_event_count", len(confirmed_events), SOURCE_LABELS["announcements"])
        _set(
            evidence,
            "verified_catalyst_count",
            min(2, len(confirmed_categories)),
            SOURCE_LABELS["announcements"],
            overwrite=True,
        )
        evidence["catalyst_partial"] = len(confirmed_categories) < len(event_categories)
        evidence["catalyst_reason"] = (
            f"识别到 {len(event_list)} 项有日期公告事件，其中 {len(confirmed_events)} 项含金额、数量、产能或明确动作细节"
            if event_list
            else "公告覆盖完整但未识别到有日期的明确催化事件"
        )

    _chain_match(evidence)
    _chokepoint_match(evidence)

    web_supply = evidence.get("web_supply_validation") or {}
    current_supply_count = _float(evidence.get("supply_evidence_count"))
    if web_supply.get("status") == "已验证" and (current_supply_count is None or current_supply_count < 2):
        _set(evidence, "supply_evidence_count", web_supply.get("evidence_count"), SOURCE_LABELS["web_research"], overwrite=True)
        _set(evidence, "supply_tightening", web_supply.get("tightening"), SOURCE_LABELS["web_research"], overwrite=True)
        evidence["supply_web_fallback"] = True

    web_chokepoint = evidence.get("web_chokepoint_validation") or {}
    if web_chokepoint.get("status") == "已验证" and ("chokepoint_score" not in evidence or evidence.get("chokepoint_partial") is True):
        _set(evidence, "chokepoint_score", web_chokepoint.get("score"), SOURCE_LABELS["web_research"], overwrite=True)
        evidence["chokepoint_partial"] = False
        evidence["chokepoint_web_fallback"] = True

    web_risk = evidence.get("web_risk_validation") or {}
    if web_risk.get("status") == "已验证":
        for key in ("st_risk", "audit_risk", "goodwill_risk"):
            if web_risk.get(key) is True:
                if key == "goodwill_risk":
                    goodwill_ratio = _float(evidence.get("goodwill_to_assets"))
                    if goodwill_ratio is not None and goodwill_ratio <= 0.10:
                        evidence["web_goodwill_risk_conflict"] = True
                        continue
                _set(evidence, key, True, SOURCE_LABELS["web_research"], overwrite=True)

    web_specialized = evidence.get("web_specialized_validation") or {}
    if web_specialized.get("status") == "已验证" and ("specialized_strength" not in evidence or evidence.get("specialized_partial") is True):
        _set(evidence, "specialized_strength", web_specialized.get("strength"), SOURCE_LABELS["web_research"], overwrite=True)
        evidence["specialized_reason"] = web_specialized.get("reason", "权威网页确认资质")
        evidence["specialized_partial"] = False

    web_results = evidence.get("web_subfactor_results") if isinstance(evidence.get("web_subfactor_results"), dict) else {}
    web_chain_hits: list[str] = []
    for key in ("F1.upstream", "F4.business_match", "F4.profit_position", "F3.leadership"):
        result = web_results.get(key) if isinstance(web_results, dict) else None
        if not isinstance(result, dict) or result.get("status") != "网络命中（未核验）":
            continue
        text = " ".join([
            str(result.get("reason") or ""),
            " ".join(str(item) for item in result.get("signals", [])),
        ])
        if any(term in text for term in ("电子特气", "半导体特气", "光刻气", "半导体材料", "关键气体")):
            web_chain_hits.append(key)
    if web_chain_hits and (not evidence.get("chain_name") or evidence.get("chain_partial") is True):
        _set(evidence, "chain_name", "半导体电子特气产业链", SOURCE_LABELS["web_research"], overwrite=True)
        _set(evidence, "chain_stage", "upstream", SOURCE_LABELS["web_research"], overwrite=True)
        _set(evidence, "business_chain_match", 0.75, SOURCE_LABELS["web_research"], overwrite=True)
        evidence["chain_partial"] = True
        evidence["business_match_partial"] = True
        evidence["business_match_reason"] = "网络搜索命中半导体电子特气关键供应链线索，主营收入占比仍需披露确认"
        evidence["web_chain_fallback"] = True


    promotion_hits = set(str(item) for item in evidence.get("promotional_keyword_hits", []))
    promotion_hits.update(str(item) for item in evidence.get("discussion_promotion_hits", []))
    rumor_hits = set(str(item) for item in evidence.get("rumor_keyword_hits", []))
    rumor_hits.update(str(item) for item in evidence.get("discussion_rumor_hits", []))
    announcement_rumors = {term for term in RUMOR_TERMS if term in title_text}
    platform_hits = max(
        int(_float(evidence.get("social_platform_hits")) or 0),
        int(_float(evidence.get("discussion_source_count")) or 0),
    )
    promotion_record_count = int(_float(evidence.get("discussion_promotion_record_count")) or 0)
    promotion_source_count = int(_float(evidence.get("discussion_promotion_source_count")) or 0)
    promotion_platforms = {str(item) for item in evidence.get("social_promotional_platforms", []) if item}
    author_count = int(_float(evidence.get("discussion_author_count")) or 0)
    template_clusters = int(_float(evidence.get("discussion_template_cluster_count")) or 0)
    synchronized_recommendation = promotion_record_count >= 3 and (author_count >= 3 or promotion_source_count >= 2)
    template_hit = template_clusters >= 1 or (
        promotion_record_count >= 2 and len(promotion_hits & set(PROMOTION_TEMPLATE_TERMS)) >= 2
    )
    cross_platform_promotion = len(promotion_platforms) >= 3
    attention = _float(evidence.get("attention_heat"))
    social_heat = _float(evidence.get("social_heat"))
    combined_heat = max(value for value in (attention, social_heat) if value is not None) if any(value is not None for value in (attention, social_heat)) else None
    price = _float(evidence.get("price_percentile_3y"))
    profit = _float(evidence.get("net_profit"))
    profit_yoy = _float(evidence.get("profit_yoy"))
    revenue_yoy = _float(evidence.get("revenue_yoy"))
    financial_negative = any(value is not None and value < 0 for value in (profit, profit_yoy, revenue_yoy))
    fundamental_gap = combined_heat is not None and combined_heat >= 0.75 and financial_negative
    if combined_heat is None:
        fundamental_reason = "缺少热度或财务交叉证据"
    elif combined_heat < 0.75:
        fundamental_reason = f"热度 {combined_heat:.2f} 未达 0.75 阈值" + ("，虽有负向财务证据" if financial_negative else "")
    else:
        fundamental_reason = f"热度 {combined_heat:.2f} 与负向财务证据{'并存' if financial_negative else '未同时出现'}"
    kline_overlap = combined_heat is not None and combined_heat >= 0.75 and price is not None and price >= 0.80 and evidence.get("technical_overheat") is True
    kline_reason = "缺少热度、价格分位或技术信号"
    if combined_heat is not None and price is not None:
        kline_reason = f"热度 {combined_heat:.2f}、价格分位 {price:.1%}、技术过热={evidence.get('technical_overheat')}"
    checks = [
        {"signal": "大量账号/平台同步推荐", "hit": synchronized_recommendation, "evidence": f"推广内容 {promotion_record_count} 条、作者 {author_count} 个、来源 {promotion_source_count} 个" if promotion_record_count else "未取得多作者或多来源同步推荐证据"},
        {"signal": "推荐话术模板化", "hit": template_hit, "evidence": f"模板簇 {template_clusters} 个；话术 {'、'.join(sorted(promotion_hits & set(PROMOTION_TEMPLATE_TERMS))) or '无'}"},
        {"signal": "付费社群/VIP 引流", "hit": bool(promotion_hits & set(PAID_GROUP_TERMS)), "evidence": "、".join(sorted(promotion_hits & set(PAID_GROUP_TERMS))) or "未命中付费引流词"},
        {"signal": "基本面与热度脱节", "hit": fundamental_gap, "evidence": fundamental_reason},
        {"signal": "K 线异常配合", "hit": kline_overlap, "evidence": kline_reason},
        {"signal": "老师/股神人设推广", "hit": bool(promotion_hits & set(PERSONA_TERMS)), "evidence": "、".join(sorted(promotion_hits & set(PERSONA_TERMS))) or "未命中人设推广词"},
        {"signal": "跨平台联动推广", "hit": cross_platform_promotion, "evidence": "、".join(sorted(promotion_platforms)) if promotion_platforms else "未形成至少三个推广平台证据"},
        {"signal": "虚假研报/谣言/澄清", "hit": bool(rumor_hits or announcement_rumors), "evidence": "、".join(sorted(rumor_hits | announcement_rumors)) or "未命中公开谣言或澄清证据"},
    ]
    signal_count = sum(bool(item["hit"]) for item in checks)
    social_abnormality = any((synchronized_recommendation, template_hit, bool(promotion_hits & set(PAID_GROUP_TERMS)), bool(promotion_hits & set(PERSONA_TERMS)), cross_platform_promotion, bool(rumor_hits)))
    market_abnormality = fundamental_gap or kline_overlap
    official_abnormality = bool(announcement_rumors)
    independent_categories = sum((social_abnormality, market_abnormality, official_abnormality))
    coverage_partial = any((
        evidence.get("social_partial") is True,
        evidence.get("discussion_partial") is True,
        evidence.get("news_partial") is True,
        int(_float(evidence.get("social_platforms_checked")) or 0) < int(_float(evidence.get("social_platforms_total")) or 0),
    ))
    if evidence.get("social_platforms_checked") is not None or evidence.get("discussion_source_status"):
        _set(evidence, "trap_signal_count", signal_count, SOURCE_LABELS["social_sentiment"])
        _set(evidence, "trap_checks", checks, SOURCE_LABELS["social_sentiment"])
        _set(evidence, "trap_independent_categories", independent_categories, SOURCE_LABELS["social_sentiment"])
        if signal_count >= 4 and independent_categories >= 2:
            risk = "高"
        elif signal_count >= 2:
            risk = "注意"
        elif coverage_partial:
            risk = "未见高风险信号（部分覆盖）"
        else:
            risk = "低"
        _set(evidence, "trap_risk_level", risk, SOURCE_LABELS["social_sentiment"])


def _classification_database_evidence(evidence: dict[str, Any]) -> None:
    result = lookup(
        evidence.get("code"),
        str(evidence.get("security_name") or evidence.get("name") or ""),
    )
    evidence["classification_db_source"] = CLASSIFICATION_DB_SOURCE
    evidence["classification_db_found"] = bool(result.get("found"))
    evidence["classification_db_match_by"] = result.get("match_by", "")
    evidence["classification_db_categories"] = list(result.get("categories") or ())
    if result.get("industry"):
        evidence["classification_db_industry"] = result["industry"]
    if not result.get("found"):
        evidence["classification_db_reason"] = (
            "完整版名单数据库未命中，专精特新、行业龙头、核心供应商转网络搜索核验"
        )
        return

    categories = list(result.get("categories") or ())
    category_text = "、".join(categories)
    evidence["classification_db_reason"] = (
        f"完整版名单数据库按{result.get('match_by') or '证券代码'}命中：{category_text}"
    )
    if has_category(result, "specialized"):
        _set(evidence, "specialized_strength", 1.0, CLASSIFICATION_DB_SOURCE, overwrite=True)
        evidence["classification_db_specialized"] = True
        evidence["specialized_reason"] = evidence["classification_db_reason"]
        evidence["specialized_partial"] = False
    if has_category(result, "leadership") or has_category(result, "core_supplier"):
        _set(evidence, "leadership_strength", 1.0, CLASSIFICATION_DB_SOURCE, overwrite=True)
        evidence["classification_db_leadership"] = has_category(result, "leadership")
        evidence["classification_db_core_supplier"] = has_category(result, "core_supplier")
        evidence["leadership_reason"] = evidence["classification_db_reason"]
        evidence["leadership_partial"] = False
        evidence["leadership_source_quality"] = "名单数据库"


def build_evidence(code: str, name: str, reports: dict[str, str]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "code": code,
        "name": name or code,
        "metric_sources": {},
        "completed_modules": list(reports),
    }
    for directory, report in reports.items():
        source = SOURCE_LABELS.get(directory, directory)
        for payload in _extract_comments(report):
            for key, value in payload.items():
                _set(evidence, key, value, source, overwrite=True)
    _derive_legacy_fields(evidence, reports)
    _classification_database_evidence(evidence)
    _derive_framework_fields(evidence, reports)
    if evidence.get("web_subfactor_results"):
        evidence["search_evidence_quality"] = "网络命中仅作未核验补缺，结构化数据优先"
    return evidence
