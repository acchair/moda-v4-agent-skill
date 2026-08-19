"""Sector-aware query planning for broad and judgment-card web research.

Profiles only control where and how to search.  They never turn a search hit
into evidence; body, object, metric and source validation remain downstream.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence


# The generic fallback deliberately includes unrestricted discovery queries.
# A previously hard-coded set of handset-chip vendors made unrelated sectors
# such as EDA and semiconductor materials search the wrong industry chain.
GENERIC_QUERY_BUCKETS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("政策与统计", ("miit.gov.cn", "ndrc.gov.cn", "gov.cn", "stats.gov.cn"), "政策 需求 渗透率 技术路线 产业趋势", "industry_trend"),
    ("法定披露", ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn"), "业务 产品 订单 收入 风险", "company_exposure"),
    ("技术标准", ("cnipa.gov.cn", "std.gov.cn", "std.samr.gov.cn", "ieee.org"), "专利 标准 认证 工艺 技术路线", "technology_route"),
    ("市场事实", ("eastmoney.com", "10jqka.com.cn", "stcn.com", "cs.com.cn"), "订单 产能 估值 机构持仓 市场预期", "market_pricing"),
    ("补充媒体", ("cnstock.com", "yicai.com", "cls.cn", "jrj.com.cn"), "供需 价格 库存 订单 产业链", "supply_demand"),
    ("开放发现", ("", "", "", ""), "市场规模 增速 竞争格局 国产化率 细分结构", "market_structure"),
)


# This is a discovery catalogue, not a list of evidence providers.  A broad
# research run selects only the few domains relevant to the resolved sector,
# then still requires a readable body and the domestic A-share checks below.
OVERSEAS_INTELLIGENCE_SOURCE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "life_science",
        "label": "生命科学/生物医药",
        "terms": ("mrna", "生物医药", "创新药", "疫苗", "cxo", "临床", "医药", "医疗", "生物"),
        "sources": ("fda.gov", "clinicaltrials.gov", "sec.gov", "fiercebiotech.com", "reuters.com"),
        "event_hint": "FDA approval breakthrough therapy phase 2 phase 3 clinical data licensing acquisition biotech funding",
        "mapping_chain": "海外临床/审批/交易 → 技术路线验证 → 测序生信、原料递送、研发服务、制造环节（均待A股主营与订单核验）",
    },
    {
        "id": "semiconductor_ai",
        "label": "半导体/AI基础设施",
        "terms": ("半导体", "芯片", "eda", "封装", "晶圆", "光刻", "算力", "ai", "服务器", "数据中心"),
        "sources": ("sec.gov", "trendforce.com", "semianalysis.com", "digitimes.com", "reuters.com"),
        "event_hint": "earnings guidance capex capacity bookings orders product roadmap supply chain",
        "mapping_chain": "海外资本开支/产品路线 → 需求与供给变化 → 设备、材料、设计工具、封装或基础设施环节（均待A股主营与订单核验）",
    },
    {
        "id": "energy_materials",
        "label": "能源/金属/化工",
        "terms": ("能源", "石油", "天然气", "煤", "电力", "储能", "锂", "铜", "金属", "化工", "材料"),
        "sources": ("eia.gov", "sec.gov", "spglobal.com", "argusmedia.com", "reuters.com"),
        "event_hint": "inventory production capacity price capex supply demand contract",
        "mapping_chain": "海外供需/库存/资本开支 → 商品或关键原料价格与采购变化 → 资源、材料、设备或加工环节（均待A股主营与订单核验）",
    },
    {
        "id": "defense",
        "label": "军工/航空航天",
        "terms": ("军工", "国防", "航空", "航天", "卫星", "无人机", "导弹", "雷达"),
        "sources": ("defense.gov", "sec.gov", "defensenews.com", "breakingdefense.com", "reuters.com"),
        "event_hint": "procurement budget contract production program supply chain",
        "mapping_chain": "海外预算/采购/型号进度 → 供应链需求变化 → 材料、部件、装备或信息化环节（均待A股主营与订单核验）",
    },
    {
        "id": "shipping",
        "label": "航运/物流",
        "terms": ("航运", "集运", "港口", "物流", "船舶", "运价", "集装箱"),
        "sources": ("marad.dot.gov", "lloydslist.com", "splash247.com", "sec.gov", "reuters.com"),
        "event_hint": "freight rate demand fleet capacity port congestion orderbook",
        "mapping_chain": "海外运价/运力/港口变化 → 运输与造船链景气变化 → 航运、港口、船舶或配套环节（均待A股主营与订单核验）",
    },
    {
        "id": "agriculture",
        "label": "农业/食品",
        "terms": ("农业", "种业", "粮食", "饲料", "农药", "化肥", "食品", "玉米", "大豆"),
        "sources": ("usda.gov", "fao.org", "agricensus.com", "sec.gov", "reuters.com"),
        "event_hint": "crop forecast acreage yield inventory export price fertilizer demand",
        "mapping_chain": "海外种植/库存/贸易变化 → 农产品与投入品供需变化 → 种业、农资、饲料或加工环节（均待A股主营与订单核验）",
    },
    {
        "id": "macro_finance",
        "label": "宏观/金融",
        "terms": ("宏观", "利率", "通胀", "汇率", "地产", "银行", "保险", "券商"),
        "sources": ("federalreserve.gov", "fred.stlouisfed.org", "bea.gov", "bls.gov", "reuters.com"),
        "event_hint": "rate inflation employment GDP credit liquidity earnings guidance",
        "mapping_chain": "海外宏观变量变化 → 风险偏好、资本成本或外需变化 → A股行业需求与估值变量（需单独验证传导）",
    },
    {
        "id": "general",
        "label": "通用全球财经",
        "terms": (),
        "sources": ("sec.gov", "reuters.com", "cnbc.com", "ft.com", "nikkei.com"),
        "event_hint": "earnings guidance capex orders acquisition licensing funding supply demand",
        "mapping_chain": "海外事件 → 产业变量变化 → A股具体供给环节（必须先验证主营、收入暴露与订单）",
    },
)


SECTOR_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "id": "eda",
        "label": "EDA（电子设计自动化）",
        "aliases": ("eda", "电子设计自动化", "电子系统设计", "esd alliance"),
        "canonical_terms": "EDA 电子设计自动化",
        "broad_queries": (
            ("行业规模", "market_size", "support", "semi.org", "ESD Alliance EDA electronic system design market revenue growth"),
            ("行业规模", "market_size", "crosscheck", "news.cn", "中国EDA市场规模 全球市场 份额 增速"),
            ("行业规模", "market_size", "crosscheck", "seccw.com", "中国EDA市场规模 预测 增速"),
            ("竞争与国产化", "localization", "support", "cspengyuan.com", "国产EDA 市场份额 竞争格局 技术突围"),
            ("竞争与国产化", "competition", "counter", "news.cn", "海外三巨头 中国市场份额 国产替代 难点"),
            ("技术路线", "technology_route", "support", "cspengyuan.com", "模拟EDA 数字前端 数字后端 制造测试EDA 国产化差异"),
            ("技术路线", "technology_route", "support", "semi.org", "EDA semiconductor IP services category methodology"),
            ("需求与兑现", "demand_driver", "support", "semi.org", "EDA revenue growth semiconductor design demand"),
            ("需求与兑现", "financial_realization", "support", "cninfo.com.cn", "EDA 年报 营业收入 研发投入 客户验证 订单"),
            ("公司暴露", "company_exposure", "support", "szse.cn", "华大九天 广立微 年报 主营 产品 收入"),
            ("公司暴露", "company_exposure", "support", "sse.com.cn", "概伦电子 年报 主营 产品 收入"),
            ("公司暴露", "company_exposure", "crosscheck", "cnstock.com", "EDA 上市公司 业绩 研发投入"),
            ("公司暴露", "company_exposure", "crosscheck", "money.finance.sina.com.cn", "华大九天 广立微 年度报告"),
            ("反证检索", "counterevidence", "counter", "cninfo.com.cn", "EDA 研发费用增长 亏损 毛利率下降 客户集中 风险"),
            ("反证检索", "counterevidence", "counter", "cspengyuan.com", "国产EDA 短板 工具链不完整 海外垄断 客户验证"),
        ),
        "section_queries": {
            "industry_trend": (
                ("(site:semi.org)", "ESD Alliance EDA market revenue growth methodology"),
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "EDA 市场规模 竞争格局 年报"),
            ),
            "supply_demand": (
                ("(site:semi.org)", "EDA bookings revenue growth semiconductor design demand"),
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "EDA 客户需求 订单 续费 研发预算"),
            ),
            "profit_pool": (
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "EDA 产品收入 毛利率 研发投入 商业模式"),
                ("(site:semi.org)", "EDA semiconductor IP services revenue category"),
            ),
            "scarcity": (
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "EDA 核心工具 技术壁垒 客户认证 国产替代"),
                ("(site:semi.org)", "EDA technology roadmap verification complexity"),
            ),
            "profit_realization": (
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "EDA 订单 收入 净利润 现金流 研发费用"),
            ),
        },
    },
    {
        "id": "semiconductor_materials",
        "label": "半导体材料",
        "aliases": ("半导体材料", "晶圆制造材料", "芯片材料", "电子材料"),
        "canonical_terms": "半导体材料 晶圆制造材料 封装材料",
        "broad_queries": (
            ("行业规模", "market_size", "support", "semi.org", "global semiconductor materials market revenue regional market data"),
            ("区域结构", "regional_structure", "support", "semi.org", "中国大陆 半导体材料 消费额 全球排名 增速"),
            ("材料结构", "segment_structure", "support", "cpcic.org", "光刻胶 技术路线 g线 i线 KrF ArF EUV 市场格局"),
            ("材料结构", "segment_structure", "crosscheck", "nepconasia.com", "硅片 光掩模 光刻胶 电子特气 靶材 CMP 湿电子化学品"),
            ("材料结构", "segment_structure", "crosscheck", "fsemi.tech", "晶圆制造材料 市场结构 占比"),
            ("细分市场", "segment_market", "crosscheck", "infoobs.com", "电子特气 光刻胶 硅片 CMP 湿电子化学品 靶材 市场规模"),
            ("国产化", "localization", "support", "cpcic.org", "半导体材料 国产化 技术路线 进口依赖"),
            ("国产化", "localization", "crosscheck", "siscmag.com", "CMP 光刻胶 电子气体 硅片 国产化率"),
            ("需求驱动", "demand_driver", "support", "semi.org", "wafer fabrication packaging materials demand growth"),
            ("需求驱动", "demand_driver", "crosscheck", "siscmag.com", "先进制程 3D NAND HBM CMP 硅片 封装材料 需求"),
            ("公司暴露", "company_exposure", "support", "cninfo.com.cn", "半导体材料 年报 产品 收入 产能 客户认证"),
            ("公司暴露", "company_exposure", "support", "sse.com.cn", "半导体材料 年报 毛利率 产能利用率 订单"),
            ("公司暴露", "company_exposure", "support", "szse.cn", "半导体材料 年报 毛利率 产能利用率 订单"),
            ("反证检索", "counterevidence", "counter", "cninfo.com.cn", "半导体材料 产能过剩 降价 库存 减值 客户验证不及预期"),
            ("反证检索", "counterevidence", "counter", "siscmag.com", "半导体材料 供过于求 降价 国产化不及预期"),
        ),
        "section_queries": {
            "industry_trend": (
                ("(site:semi.org)", "semiconductor materials market revenue regional growth"),
                ("(site:cpcic.org)", "半导体材料 市场格局 技术路线 国产化"),
            ),
            "supply_demand": (
                ("(site:semi.org)", "semiconductor materials demand wafer fabrication packaging growth"),
                ("(site:cpcic.org)", "半导体材料 供需 进口依赖 产能 技术路线"),
            ),
            "profit_pool": (
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "半导体材料 产品收入 毛利率 定价权 核心材料"),
                ("(site:cpcic.org)", "硅片 光刻胶 电子特气 CMP 材料 市场结构"),
            ),
            "scarcity": (
                ("(site:cpcic.org)", "光刻胶 KrF ArF EUV 技术壁垒 国产化"),
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "半导体材料 客户认证 良率 技术壁垒 国产替代"),
            ),
            "profit_realization": (
                ("(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn)", "半导体材料 订单 收入 净利润 现金流 产能利用率"),
            ),
        },
    },
)


def _compact_context(context: str) -> str:
    values: list[str] = []
    for token in re.split(r"[\s、,，;；|/]+", str(context or "")):
        token = token.strip()
        if len(token) < 2 or token in values:
            continue
        values.append(token)
        if len(" ".join(values)) >= 72 or len(values) >= 6:
            break
    return " ".join(values)


def resolve_sector_profile(sector: str, context: str = "") -> dict[str, Any] | None:
    haystack = f"{sector} {context}".lower()
    for profile in SECTOR_PROFILES:
        if any(str(alias).lower() in haystack for alias in profile["aliases"]):
            return profile
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_phrases(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        rows = [_text(item) for item in value]
    else:
        rows = re.split(r"[、,，;；|/]+", _text(value))
    return [item for item in rows if 2 <= len(item) <= 48]


def _representative_rows(screening: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for key in ("shortlist", "all_candidates", "watchlist"):
        values = screening.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        for raw in values:
            if not isinstance(raw, Mapping):
                continue
            identity = _text(raw.get("code") or raw.get("name"))
            if not identity or identity in seen:
                continue
            seen.add(identity)
            rows.append(raw)
            if len(rows) >= 8:
                return rows
    return rows


def build_entity_context(sector: str, screening: Mapping[str, Any]) -> dict[str, Any]:
    """Convert AKShare board resolution and F10 screening into search entities."""
    universe = screening.get("universe") if isinstance(screening.get("universe"), Mapping) else {}
    board_names = _as_phrases(universe.get("board_names"))
    board_name = _text(universe.get("board_name"))
    if board_name and board_name not in board_names:
        board_names.insert(0, board_name)
    rows = _representative_rows(screening)
    companies: list[dict[str, str]] = []
    business_phrases: list[str] = []
    industries: list[str] = []
    for row in rows:
        companies.append({"code": _text(row.get("code")), "name": _text(row.get("name"))})
        for phrase in _as_phrases(row.get("industry")):
            if phrase not in {"需人工确认", "待定位"} and phrase not in industries:
                industries.append(phrase)
        for phrase in (
            *_as_phrases(row.get("chain_name")),
            *_as_phrases(row.get("chain_stage")),
            *_as_phrases(row.get("main_business")),
            *_as_phrases(row.get("business_items")),
        ):
            if phrase in {"需人工确认", "待定位"}:
                continue
            if phrase not in business_phrases:
                business_phrases.append(phrase)
    source = _text(universe.get("source"))
    coverage_status = _text(universe.get("coverage_status"))
    query_kind = _text(screening.get("query_kind") or universe.get("query_kind")) or "sector"
    if "概念板块" in source or _text(universe.get("coverage_status")) == "live_theme":
        query_kind = "concept"
    if coverage_status in {"live_full", "live_theme", "user_supplied"}:
        resolution_status = "resolved"
    elif rows or board_names:
        resolution_status = "partial"
    else:
        resolution_status = "unresolved"
    return {
        "status": resolution_status,
        "input": _text(sector),
        "query_kind": query_kind,
        "board_names": board_names,
        "representative_companies": companies,
        "industries": industries,
        "business_phrases": business_phrases[:12],
        "universe_source": source,
        "coverage_status": coverage_status,
        "constituent_count": int(universe.get("total") or len(screening.get("all_candidates") or [])),
        "live_resolution_error": _text(universe.get("live_fallback") or universe.get("error")),
    }


def resolve_entity_context(
    sector: str,
    *,
    screening: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve a user expression through the existing AKShare board adapters.

    When baseline screening is available it is reused, including its F10 main
    business snapshots.  Otherwise this performs only the live constituent
    resolution step and leaves business exposure explicitly unverified.
    """
    if isinstance(screening, Mapping) and screening:
        return build_entity_context(sector, screening)
    try:
        from tools import sector_screening

        query_kind = sector_screening.infer_query_kind(sector)
        base = Path(root) if root else Path(__file__).resolve().parents[2]
        rows, metadata = sector_screening.resolve_sector_universe(
            base,
            sector,
            use_live_universe=True,
            query_kind=query_kind,
        )
        lightweight = {
            "query_kind": query_kind,
            "universe": {**metadata, "total": len(rows)},
            "all_candidates": rows[:12],
        }
        return build_entity_context(sector, lightweight)
    except Exception as exc:
        return {
            "status": "failed",
            "input": _text(sector),
            "query_kind": "unknown",
            "board_names": [],
            "representative_companies": [],
            "industries": [],
            "business_phrases": [],
            "universe_source": "AKShare板块解析",
            "coverage_status": "unavailable",
            "constituent_count": 0,
            "error": f"{type(exc).__name__}: 板块实体解析失败",
        }


def _business_archetype(entity_context: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    text = " ".join(_as_phrases(entity_context.get("business_phrases"))).lower()
    rules: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        ("materials", ("材料", "光刻胶", "硅片", "靶材", "气体", "化学品", "cmp"), ("价格 库存 产能利用率 扩产周期", "纯度 配方 良率 客户认证 国产化率")),
        ("equipment", ("设备", "装备", "机床", "机器人", "仪器"), ("下游资本开支 订单 交付 产能", "核心零部件 客户验证 国产替代")),
        ("software", ("软件", "平台", "算法", "系统", "saas", "license", "eda"), ("商业模式 授权订阅 客户续费", "研发投入 工具链完整度 生态兼容 客户验证")),
        ("healthcare", ("药", "医疗", "临床", "器械", "生物"), ("临床进度 审批 集采 医保", "适应症 竞争格局 商业化 放量")),
        ("consumer", ("消费", "食品", "饮料", "家电", "服装", "零售"), ("销量 渠道 库存 价格带", "品牌份额 单店 同店增长 原材料成本")),
    )
    for archetype, terms, hints in rules:
        if any(term in text for term in terms):
            return archetype, hints
    return "general", ("需求驱动 供给约束 价格 库存 产能", "技术路线 竞争格局 利润池 商业兑现")


def overseas_event_profile(
    sector: str,
    context: str = "",
    *,
    entity_context: Mapping[str, Any] | None = None,
    profile_id: str = "generic",
) -> dict[str, Any]:
    """Select a compact overseas event radar for a sector expression.

    The returned chain is deliberately a *validation template*.  It says
    which domestic links need testing; it does not identify beneficiaries or
    claim an A-share earnings impact.
    """
    entity = dict(entity_context or {})
    archetype, _ = _business_archetype(entity)
    haystack = " ".join((
        _text(sector), _text(context), _text(profile_id), archetype,
        " ".join(_as_phrases(entity.get("board_names"))),
        " ".join(_as_phrases(entity.get("business_phrases"))),
    )).lower()
    for item in OVERSEAS_INTELLIGENCE_SOURCE_CATALOG:
        if item["id"] == "life_science" and archetype == "healthcare":
            return dict(item)
        if item["id"] == "semiconductor_ai" and profile_id in {"eda", "semiconductor_materials"}:
            return dict(item)
        if any(term in haystack for term in item["terms"]):
            return dict(item)
    return next(dict(item) for item in OVERSEAS_INTELLIGENCE_SOURCE_CATALOG if item["id"] == "general")


def _overseas_event_queries(
    sector: str,
    context: str,
    entity_context: Mapping[str, Any],
    profile_id: str,
) -> tuple[dict[str, Any], tuple[tuple[str, str, str, str, str], ...]]:
    profile = overseas_event_profile(sector, context, entity_context=entity_context, profile_id=profile_id)
    sources = tuple(str(domain) for domain in profile["sources"][:4])
    return profile, tuple(
        ("海外增量雷达", "overseas_event", "neutral", domain, str(profile["event_hint"]))
        for domain in sources
    )


def _dynamic_queries(entity_context: Mapping[str, Any]) -> tuple[tuple[str, str, str, str, str], ...]:
    boards = _as_phrases(entity_context.get("board_names"))[:3]
    companies = [
        _text(item.get("name")) for item in entity_context.get("representative_companies", [])
        if isinstance(item, Mapping) and _text(item.get("name"))
    ][:2]
    businesses = _as_phrases(entity_context.get("business_phrases"))[:3]
    if _text(entity_context.get("status")) != "resolved" and not businesses:
        companies = []
    entity_terms = " ".join([*boards, *companies, *businesses]).strip()
    if not entity_terms:
        return ()
    archetype, archetype_hints = _business_archetype(entity_context)
    company_terms = " ".join(companies[:3]) or entity_terms
    return (
        ("实体校准", "entity_definition", "neutral", "", f"{entity_terms} 板块定义 成分股 主营边界"),
        ("行业规模", "market_size", "support", "", f"{entity_terms} 市场规模 增速 统计口径 行业协会"),
        ("产业链拆分", "value_chain", "neutral", "", f"{entity_terms} 上游 中游 下游 价值链 利润池"),
        ("技术路线", "technology_route", "neutral", "", f"{entity_terms} 技术路线 细分产品 核心指标 演进"),
        ("供需变化", "supply_demand", "neutral", "", f"{entity_terms} 需求驱动 供给约束 价格 库存 订单 产能"),
        ("竞争与国产化", "competition", "support", "", f"{entity_terms} 竞争格局 CR3 国产化率 进口依赖"),
        ("公司兑现", "company_exposure", "support", "cninfo.com.cn", f"{company_terms} 年报 主营构成 收入 毛利率 订单 客户"),
        ("反证检索", "counterevidence", "counter", "", f"{entity_terms} 需求下滑 产能过剩 降价 技术替代 业绩不及预期"),
        ("业务模型", f"{archetype}_driver", "neutral", "", f"{entity_terms} {archetype_hints[0]}"),
        ("业务模型", f"{archetype}_barrier", "neutral", "", f"{entity_terms} {archetype_hints[1]}"),
    )


def build_broad_query_plan(
    sector: str,
    context: str = "",
    *,
    entity_context: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    theme = str(sector or "").strip()
    if not theme:
        raise ValueError("sector 不能为空")
    context_text = _compact_context(context)
    resolved_entity = dict(entity_context or {})
    profile = resolve_sector_profile(
        theme,
        " ".join([context_text, *_as_phrases(resolved_entity.get("board_names")), *_as_phrases(resolved_entity.get("business_phrases"))]),
    )
    profile_id = str(profile["id"]) if profile else "generic"
    profile_label = str(profile["label"]) if profile else "通用板块"
    canonical = str(profile.get("canonical_terms") or "") if profile else ""
    plan: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def append(bucket: str, dimension: str, stance: str, domain: str, hint: str) -> None:
        prefix = " ".join(item for item in (theme, canonical, context_text) if item)
        site = f"site:{domain}" if domain else ""
        query = " ".join(item for item in (prefix, site, hint) if item).strip()
        dedupe_key = (domain, query)
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        plan.append({
            "bucket": bucket,
            "dimension": dimension,
            "stance": stance,
            "domain_hint": domain,
            "query": query,
            "profile_id": profile_id,
            "profile_label": profile_label,
        })

    # The overseas radar is run first after local entity resolution.  It is
    # intentionally narrow (four sources, selected by sector), so it can find
    # overnight increments without turning broad search into an all-site sweep.
    _, overseas_queries = _overseas_event_queries(theme, context_text, resolved_entity, profile_id)
    for bucket, dimension, stance, domain, hint in overseas_queries:
        append(bucket, dimension, stance, domain, hint)

    # A known source profile is a shortcut after entity resolution, not an
    # entity substitute.  It follows the compact overseas event scan and stays
    # ahead of broad open discovery.
    if profile:
        for bucket, dimension, stance, domain, hint in profile["broad_queries"]:
            append(bucket, dimension, stance, domain, hint)

    for bucket, dimension, stance, domain, hint in _dynamic_queries(resolved_entity):
        append(bucket, dimension, stance, domain, hint)

    # Keep generic dimensions as coverage insurance.  For a known profile,
    # profile-specific industry sources are already first in the queue and the
    # unrestricted discovery rows are unnecessary noise.
    for bucket, domains, hint, dimension in GENERIC_QUERY_BUCKETS:
        if profile and bucket == "开放发现":
            continue
        for domain in domains:
            append(bucket, dimension, "neutral", domain, hint)
    return plan


def section_query_specs(
    sector: str,
    context: str,
    section_key: str,
    *,
    entity_context: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, str], ...]:
    profile = resolve_sector_profile(sector, context)
    profile_specs = tuple(profile.get("section_queries", {}).get(section_key, ())) if profile else ()
    entity = dict(entity_context or {})
    companies = " ".join(
        _text(item.get("name")) for item in entity.get("representative_companies", [])[:3]
        if isinstance(item, Mapping)
    )
    businesses = " ".join(_as_phrases(entity.get("business_phrases"))[:4])
    dynamic_hint = " ".join(item for item in (companies, businesses) if item)
    if not dynamic_hint:
        return profile_specs
    section_hints = {
        "industry_trend": "市场规模 增速 需求驱动 技术路线",
        "supply_demand": "价格 库存 订单 产能 供需",
        "profit_pool": "产业链 毛利率 定价权 核心环节",
        "scarcity": "技术壁垒 客户认证 良率 国产替代",
        "profit_realization": "订单 收入 净利润 现金流",
        "market_pricing": "估值 历史分位 市场预期 拥挤度",
    }
    disclosure_group = "(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn OR site:bse.cn)"
    dynamic_specs = ((disclosure_group, f"{dynamic_hint} {section_hints.get(section_key, '')}"),)
    return tuple(dict.fromkeys((*dynamic_specs, *profile_specs)))
