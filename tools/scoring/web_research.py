from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import ipaddress
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from hashlib import sha256

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.scoring.classification_db import has_category, lookup
from tools.scoring.search_rules import RULES, evaluate as evaluate_gap, queries_for
from tools.scoring import sector_search_planner


OUTPUT_BASE = ROOT / "knowledge" / "research" / "web_research"
USER_AGENT = "moda-v4-research/1.0"
PUBLIC_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_FETCH_BYTES = 600_000
MAX_PDF_FETCH_BYTES = 10_000_000
MAX_PAGES = 30
MAX_PDF_PAGES = 30
MAX_PDF_TEXT_CHARS = 120_000
MAX_PAGES_PER_PURPOSE = 6
MAX_GAP_TARGETS = 12
MAX_GAP_QUERIES_PER_TARGET = 3
MAX_GAP_BUDGET_SECONDS = 75.0
MIN_TARGET_BUDGET_SECONDS = 3.0
MAX_GAP_WORKERS = 3
MAX_GAP_EVIDENCE_PAGES_PER_TARGET = 2
MAX_SECTOR_QUERIES_PER_SECTION = 2
MAX_SECTOR_EVIDENCE_PAGES_PER_SECTION = 2
MAX_SECTOR_WORKERS = 3
MAX_SECTOR_BUDGET_SECONDS = 45.0
MAX_QUERY_CONTEXT_CHARS = 96


class SearchBackendBlockedError(RuntimeError):
    """Raised when a public search page is an anti-automation response."""
GAP_PRIORITY = {
    "F2.controller_action": 120,
    "F3.survival_risk": 115,
    "F3.leadership": 108,
    "F3.specialized": 107,
    "F1.capex_wave": 106,
    "F1.era_track": 102,
    "F1.chokepoint": 100,
    "F1.supply_gap": 98,
    "F4.realization": 96,
    "F4.business_match": 94,
    "F4.profit_position": 92,
    "F5.inflection": 88,
    "F5.expectation_gap": 84,
    "F3.background": 82,
}
CACHE_PATH = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "web_search_daily.json"
_RUNTIME_LOCAL = threading.local()
_CACHE_LOCK = threading.RLock()
_CACHE_PAYLOADS: dict[str, dict[str, Any]] = {}
_CACHE_PATHS: dict[str, Path] = {}
_CACHE_DIRTY: set[str] = set()
_CACHE_BATCH_DEPTH = 0
_PAGE_SNAPSHOT_LOCK = threading.RLock()
_PAGE_SNAPSHOT: dict[str, tuple[str, str]] = {}
_PAGE_INFLIGHT: dict[str, threading.Event] = {}
AUTHORITY_DOMAINS = (
    "gov.cn", "cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn",
    "stats.gov.cn", "miit.gov.cn", "ndrc.gov.cn", "customs.gov.cn",
    "nmpa.gov.cn", "nhsa.gov.cn", "samr.gov.cn", "cde.org.cn", "cnipa.gov.cn",
    "csrc.gov.cn", "chinaclear.cn", "semi.org", "cpcic.org",
)
STATUTORY_DOMAINS = ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn")
FINANCE_MEDIA_DOMAINS = (
    "eastmoney.com", "10jqka.com.cn", "stcn.com", "cs.com.cn", "cnstock.com",
    "yicai.com", "cls.cn", "jrj.com.cn", "reuters.com", "bloomberg.com",
    "news.cn", "sina.com.cn", "ft.com", "wsj.com", "cnbc.com", "nikkei.com",
)
RESEARCH_INSTITUTION_DOMAINS = (
    "cspengyuan.com", "seccw.com", "siscmag.com", "nepconasia.com",
    "fsemi.tech", "infoobs.com",
)
OVERSEAS_REGULATORY_DOMAINS = (
    "sec.gov", "fda.gov", "clinicaltrials.gov", "eia.gov", "energy.gov", "defense.gov",
    "usda.gov", "federalreserve.gov", "fred.stlouisfed.org", "bea.gov", "bls.gov",
    "census.gov", "marad.dot.gov", "fao.org", "imf.org", "worldbank.org",
)
OVERSEAS_SECTOR_RESEARCH_DOMAINS = (
    "trendforce.com", "semianalysis.com", "digitimes.com", "fiercebiotech.com", "endpts.com",
    "biopharmadive.com", "spglobal.com", "argusmedia.com", "fastmarkets.com", "defensenews.com",
    "breakingdefense.com", "lloydslist.com", "splash247.com", "agricensus.com",
)
CLUE_ONLY_DOMAINS = (
    "xueqiu.com", "guba.eastmoney.com", "caifuhao.eastmoney.com", "gw.com.cn", "dzh.com.cn",
    "weibo.com", "zhihu.com", "reddit.com", "x.com", "twitter.com",
)
CHINA_FINANCE_SITE_GROUPS = {
    "disclosure": "(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn OR site:bse.cn)",
    "market": "(site:eastmoney.com OR site:10jqka.com.cn OR site:stcn.com OR site:cs.com.cn OR site:cnstock.com OR site:yicai.com OR site:cls.cn OR site:jrj.com.cn)",
}
INDUSTRY_AUTHORITY_DOMAINS = (
    "gov.cn", "stats.gov.cn", "miit.gov.cn", "ndrc.gov.cn", "customs.gov.cn",
    "nmpa.gov.cn", "nhsa.gov.cn", "samr.gov.cn", "cde.org.cn",
    "semi.org", "cpcic.org",
)
INDUSTRY_AUTHORITY_SITE_GROUP = (
    "(site:gov.cn OR site:stats.gov.cn OR site:miit.gov.cn OR "
    "site:ndrc.gov.cn OR site:customs.gov.cn OR site:nmpa.gov.cn OR "
    "site:nhsa.gov.cn OR site:samr.gov.cn OR site:cde.org.cn)"
)
OVERSEAS_FIRST_PARTY_DOMAINS = (
    "nvidia.com", "amd.com", "intel.com", "tsmc.com", "asml.com", "appliedmaterials.com",
    "lamresearch.com", "microsoft.com", "google.com", "meta.com", "tesla.com", "sec.gov",
)
OVERSEAS_FIRST_PARTY_SITE_GROUP = (
    "(site:nvidia.com OR site:amd.com OR site:intel.com OR site:tsmc.com OR site:asml.com OR "
    "site:appliedmaterials.com OR site:lamresearch.com OR site:microsoft.com OR site:google.com OR "
    "site:meta.com OR site:tesla.com OR site:sec.gov)"
)
PATENT_STANDARD_DOMAINS = (
    "cnipa.gov.cn", "std.samr.gov.cn", "std.gov.cn", "iso.org", "iec.ch", "ieee.org",
)
PATENT_STANDARD_SITE_GROUP = (
    "(site:cnipa.gov.cn OR site:std.samr.gov.cn OR site:std.gov.cn OR site:iso.org OR "
    "site:iec.ch OR site:ieee.org)"
)
MARKET_FACT_DOMAINS = ("csrc.gov.cn", "chinaclear.cn")
MARKET_FACT_SITE_GROUP = "(site:csrc.gov.cn OR site:chinaclear.cn)"
MARKET_DATA_DOMAINS = (
    "eastmoney.com", "10jqka.com.cn", "legulegu.com", "choice.eastmoney.com", "wind.com.cn",
)
MARKET_DATA_SITE_GROUP = (
    "(site:eastmoney.com OR site:10jqka.com.cn OR site:legulegu.com OR "
    "site:choice.eastmoney.com OR site:wind.com.cn OR site:csrc.gov.cn OR site:chinaclear.cn)"
)
# These queries collect sector-level evidence for the judgment card.  They do
# not turn a search hit into a sector conclusion or a stock recommendation.
SECTOR_EVIDENCE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "industry_trend",
        "label": "产业趋势",
        "source_policies": ("industry_authority", "overseas_first_party"),
        "query_specs": (
            (INDUSTRY_AUTHORITY_SITE_GROUP, "政策 需求 渗透率 技术路线 产业趋势"),
            (OVERSEAS_FIRST_PARTY_SITE_GROUP, "技术路线 资本开支 需求 产品路线"),
        ),
        "signal_terms": ("政策", "需求", "渗透率", "增长", "技术路线", "投资", "出货"),
    },
    {
        "key": "supply_demand",
        "label": "供需变化",
        "source_policies": ("industry_authority", "overseas_first_party"),
        "query_specs": (
            (INDUSTRY_AUTHORITY_SITE_GROUP, "价格 库存 订单 产能 供需"),
            (OVERSEAS_FIRST_PARTY_SITE_GROUP, "订单 产能 利用率 交付 供应链"),
        ),
        "signal_terms": ("价格", "库存", "订单", "产能", "供不应求", "供过于求", "利用率", "交付"),
        "positive_terms": ("供不应求", "紧缺", "缺货", "涨价", "库存下降", "去库存", "订单增长", "排产饱满", "交期延长", "产能利用率提升"),
        "negative_terms": ("供过于求", "库存上升", "降价", "产能过剩", "订单下降", "需求下滑", "开工率下降"),
    },
    {
        "key": "profit_pool",
        "label": "利润池",
        "source_policies": ("company_disclosure", "industry_authority"),
        "query_specs": (
            (CHINA_FINANCE_SITE_GROUPS["disclosure"], "产业链 业务 毛利率 定价权 核心环节"),
            (INDUSTRY_AUTHORITY_SITE_GROUP, "产业链 价值链 关键环节 附加值"),
        ),
        "signal_terms": ("毛利率", "定价权", "议价", "附加值", "核心环节", "收入", "利润"),
    },
    {
        "key": "scarcity",
        "label": "稀缺环节",
        "source_policies": ("technology_authority", "company_disclosure"),
        "query_specs": (
            (PATENT_STANDARD_SITE_GROUP, "专利 标准 认证 工艺 技术壁垒"),
            (CHINA_FINANCE_SITE_GROUPS["disclosure"], "认证 工艺 良率 扩产 供应商"),
        ),
        "signal_terms": ("专利", "标准", "认证", "工艺", "良率", "扩产", "壁垒", "替代"),
    },
    {
        "key": "profit_realization",
        "label": "利润兑现",
        "source_policies": ("company_disclosure",),
        "query_specs": (
            (CHINA_FINANCE_SITE_GROUPS["disclosure"], "订单 收入 净利润 现金流"),
            (CHINA_FINANCE_SITE_GROUPS["disclosure"], "投产 出货量 产能利用率 毛利率"),
        ),
        "signal_terms": ("订单", "收入", "营收", "净利润", "现金流", "投产", "出货", "毛利率"),
    },
    {
        "key": "market_pricing",
        "label": "市场已计价",
        "source_policies": ("market_data",),
        "query_specs": (
            (MARKET_DATA_SITE_GROUP, "估值 历史分位 股价 机构持仓"),
            (CHINA_FINANCE_SITE_GROUPS["market"], "市场预期 热度 拥挤 估值"),
        ),
        "signal_terms": ("估值", "分位", "股价", "预期", "拥挤", "机构", "持仓", "热度"),
    },
)
SUPPLY_CATEGORIES = {
    "price": ("价格", "涨价", "降价", "报价", "基差"),
    "inventory": ("库存", "仓单", "去库存"),
    "orders": ("订单", "在手订单", "交付", "排产", "交期"),
    "capacity": ("产能", "产能利用率", "扩产", "供不应求", "供过于求", "紧缺", "产能过剩"),
}
TIGHTENING_TERMS = ("供不应求", "紧缺", "缺货", "涨价", "库存下降", "去库存", "订单增长", "排产饱满", "交期延长", "产能利用率提升")
LOOSENING_TERMS = ("供过于求", "库存上升", "降价", "产能过剩", "订单下降", "需求下滑", "开工率下降")
COMPANY_RELATION_TERMS = ("产品", "设备", "业务", "供应商", "客户", "产业化", "量产")
REPLACEMENT_TERMS = ("国产替代", "进口替代", "自主可控", "国产化")
DEPENDENCY_TERMS = ("进口依赖", "卡脖子", "受制于人", "海外垄断", "国外垄断", "关键核心技术", "国产化率")
DELISTING_TERMS = ("退市风险警示", "终止上市", "暂停上市", "重大违法强制退市", "*ST", "ST ")
AUDIT_RISK_PATTERNS = (
    r"审计意见(?:为|类型为|[:：])\s*(?:保留意见|无法表示意见|否定意见)",
    r"(?:被出具|出具了?|形成了?)\s*(?:保留意见|无法表示意见|否定意见)",
)
GOODWILL_RISK_PATTERNS = (
    r"计提(?:了)?[^。；;\n]{0,20}商誉减值",
    r"商誉减值(?:准备|损失)",
    r"发生(?:了)?[^。；;\n]{0,12}商誉减值",
)
SPECIALIZED_TERMS = ("专精特新小巨人", "专精特新", "制造业单项冠军", "单项冠军")
CATALYST_CATEGORIES = {
    "orders": ("中标", "重大合同", "新增订单", "订单增长"),
    "capacity": ("扩产", "投产", "项目落地", "产线建设"),
    "performance": ("业绩预增", "扭亏", "利润增长"),
    "shareholder": ("回购", "增持"),
    "policy": ("纳入名单", "政策支持", "补贴", "获批"),
}
CAPEX_UP_TERMS = ("投资增长", "投资同比增长", "加快投资", "扩大投资", "新增产能", "扩产", "产能建设", "设备更新")
CAPEX_DOWN_TERMS = ("投资下降", "投资同比下降", "压减产能", "削减投资", "延缓投资", "停止扩产")
CAPEX_CATEGORIES = {
    "investment": ("固定资产投资", "投资增长", "投资同比增长", "设备投资"),
    "capacity": ("新增产能", "扩产", "产能建设", "投产"),
    "equipment": ("设备更新", "设备采购", "产线建设"),
}


def _load_local_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    allowed = {
        "MODA_SEARCH_PROVIDER", "MODA_PUBLIC_SEARCH", "DDG_LITE_URL", "BRAVE_SEARCH_API_KEY", "BRAVE_SEARCH_URL",
        "DEEPSEEK_API_KEY", "DEEPSEEK_ANTHROPIC_BASE_URL", "DEEPSEEK_WEB_SEARCH_MODEL",
        "MODA_MODEL_SEARCH_PROVIDER", "MODA_MODEL_SEARCH_URL",
        "MODA_MODEL_SEARCH_API_KEY", "MODA_MODEL_SEARCH_MODEL",
        "OPENAI_API_KEY", "OPENAI_WEB_SEARCH_MODEL", "OPENAI_RESPONSES_URL",
    }
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_authority(domain: str) -> bool:
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in AUTHORITY_DOMAINS) or domain.endswith(".org.cn")


def _matches_domain(domain: str, suffixes: tuple[str, ...]) -> bool:
    domain = domain.lower().removeprefix("www.")
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in suffixes)


def _source_role(domain: str) -> tuple[str, str]:
    if _matches_domain(domain, STATUTORY_DOMAINS):
        return "法定信息披露", "A"
    if _matches_domain(domain, CLUE_ONLY_DOMAINS):
        return "线索来源", "C"
    if _matches_domain(domain, OVERSEAS_REGULATORY_DOMAINS):
        return "海外监管/法定披露", "A"
    if _matches_domain(domain, OVERSEAS_FIRST_PARTY_DOMAINS):
        return "海外产业链一手资料", "A"
    if _matches_domain(domain, PATENT_STANDARD_DOMAINS):
        return "技术/标准权威", "A"
    if _is_authority(domain):
        return "权威来源", "A"
    if _matches_domain(domain, FINANCE_MEDIA_DOMAINS):
        return "财经媒体", "B"
    if _matches_domain(domain, RESEARCH_INSTITUTION_DOMAINS):
        return "行业研究", "B"
    if _matches_domain(domain, OVERSEAS_SECTOR_RESEARCH_DOMAINS):
        return "海外行业专业", "B"
    return "一般来源", "B"


def _search_rank(row: dict[str, Any]) -> tuple[int, int]:
    """Prefer disclosure and mainstream financial sources without trusting them automatically."""
    role, tier = _source_role(_domain(str(row.get("url") or "")))
    priority = {
        ("法定信息披露", "A"): 0,
        ("海外监管/法定披露", "A"): 0,
        ("权威来源", "A"): 1,
        ("海外产业链一手资料", "A"): 1,
        ("技术/标准权威", "A"): 1,
        ("财经媒体", "B"): 2,
        ("行业研究", "B"): 2,
        ("海外行业专业", "B"): 2,
        ("一般来源", "B"): 3,
        ("线索来源", "C"): 4,
    }.get((role, tier), 5)
    return priority, int(row.get("rank") or 999)


def _prioritize_search_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_search_rank)


def _gap_source_policy(key: str) -> dict[str, Any]:
    """Choose the primary evidence side before interpreting a search result."""
    if key in {"F1.era_track", "F1.supply_gap", "F1.capex_wave"}:
        return {
            "id": "industry_authority",
            "label": "产业事实",
            "site_group": INDUSTRY_AUTHORITY_SITE_GROUP,
            "query_hint": "行业数据 供需 投资 政策",
            "source_layers": (
                {"id": "industry_authority", "label": "国内产业权威", "site_group": INDUSTRY_AUTHORITY_SITE_GROUP},
                {"id": "overseas_first_party", "label": "海外产业链一手资料", "site_group": OVERSEAS_FIRST_PARTY_SITE_GROUP},
            ),
            "query_plans": (
                {"site_group": INDUSTRY_AUTHORITY_SITE_GROUP, "query_hint": "行业数据 供需 投资 政策"},
                {"site_group": OVERSEAS_FIRST_PARTY_SITE_GROUP, "query_hint": "技术路线 资本开支 需求 产品路线"},
            ),
        }
    if key == "F3.leadership":
        domestic_group = f"({CHINA_FINANCE_SITE_GROUPS['disclosure']} OR {INDUSTRY_AUTHORITY_SITE_GROUP})"
        overseas_technology_group = f"({OVERSEAS_FIRST_PARTY_SITE_GROUP} OR {PATENT_STANDARD_SITE_GROUP})"
        return {
            "id": "leadership_crosscheck",
            "label": "竞争格局与技术壁垒",
            "site_group": domestic_group,
            "query_hint": "市占率 客户认证 产能 专利 标准",
            "source_layers": (
                {"id": "company_disclosure", "label": "公司法定披露", "site_group": CHINA_FINANCE_SITE_GROUPS["disclosure"]},
                {"id": "industry_authority", "label": "国内产业权威", "site_group": INDUSTRY_AUTHORITY_SITE_GROUP},
                {"id": "overseas_first_party", "label": "海外产业链一手资料", "site_group": OVERSEAS_FIRST_PARTY_SITE_GROUP},
                {"id": "technology_authority", "label": "专利与标准权威", "site_group": PATENT_STANDARD_SITE_GROUP},
            ),
            "query_plans": (
                {"site_group": domestic_group, "query_hint": "市占率 客户认证 产能 核心供应商"},
                {"site_group": overseas_technology_group, "query_hint": "技术路线 认证 专利 标准 供应链"},
            ),
        }
    if key in {"F3.specialized", "F3.background"}:
        return {
            "id": "qualification_authority",
            "label": "资质与竞争格局",
            "site_group": f"({INDUSTRY_AUTHORITY_SITE_GROUP} OR {CHINA_FINANCE_SITE_GROUPS['disclosure']})",
            "query_hint": "名单 资质 控股股东 实际控制人",
        }
    if key == "F1.chokepoint":
        domestic_group = f"({CHINA_FINANCE_SITE_GROUPS['disclosure']} OR {INDUSTRY_AUTHORITY_SITE_GROUP})"
        overseas_technology_group = f"({OVERSEAS_FIRST_PARTY_SITE_GROUP} OR {PATENT_STANDARD_SITE_GROUP})"
        return {
            "id": "chain_crosscheck",
            "label": "产业链瓶颈与竞争格局",
            "site_group": domestic_group,
            "query_hint": "国产替代 进口依赖 关键环节",
            "source_layers": (
                {"id": "company_disclosure", "label": "公司法定披露", "site_group": CHINA_FINANCE_SITE_GROUPS["disclosure"]},
                {"id": "industry_authority", "label": "国内产业权威", "site_group": INDUSTRY_AUTHORITY_SITE_GROUP},
                {"id": "overseas_first_party", "label": "海外产业链一手资料", "site_group": OVERSEAS_FIRST_PARTY_SITE_GROUP},
                {"id": "technology_authority", "label": "专利与标准权威", "site_group": PATENT_STANDARD_SITE_GROUP},
            ),
            "query_plans": (
                {"site_group": domestic_group, "query_hint": "国产替代 进口依赖 关键环节"},
                {"site_group": overseas_technology_group, "query_hint": "技术路线 海外垄断 专利 标准"},
            ),
        }
    if key in {"F5.price_position", "F5.valuation", "F5.coldness", "F5.expectation_gap"}:
        return {
            "id": "market_transaction",
            "label": "市场交易与预期",
            "site_group": CHINA_FINANCE_SITE_GROUPS["market"],
            "query_hint": "估值 分位 预期 关注度 交易",
        }
    return {
        "id": "company_disclosure",
        "label": "公司事实与利润兑现",
        "site_group": CHINA_FINANCE_SITE_GROUPS["disclosure"],
        "query_hint": "公告 年报 季报 业务 订单 利润",
    }


def _source_matches_policy(domain: str, source_role: str, policy_id: str) -> bool:
    if policy_id == "industry_authority":
        return _matches_domain(domain, INDUSTRY_AUTHORITY_DOMAINS)
    if policy_id == "overseas_first_party":
        return _matches_domain(domain, OVERSEAS_FIRST_PARTY_DOMAINS)
    if policy_id == "technology_authority":
        return _matches_domain(domain, PATENT_STANDARD_DOMAINS) or _matches_domain(domain, INDUSTRY_AUTHORITY_DOMAINS)
    if policy_id == "company_disclosure":
        return source_role == "法定信息披露"
    if policy_id == "qualification_authority":
        return _matches_domain(domain, INDUSTRY_AUTHORITY_DOMAINS) or source_role == "法定信息披露"
    if policy_id in {"chain_crosscheck", "leadership_crosscheck"}:
        return (
            _matches_domain(domain, INDUSTRY_AUTHORITY_DOMAINS)
            or _matches_domain(domain, OVERSEAS_FIRST_PARTY_DOMAINS)
            or _matches_domain(domain, PATENT_STANDARD_DOMAINS)
            or source_role == "法定信息披露"
        )
    if policy_id == "market_transaction":
        return source_role in {"法定信息披露", "财经媒体"}
    return source_role == "法定信息披露"


def _gap_policy_layers(policy: dict[str, Any]) -> list[dict[str, Any]]:
    layers = policy.get("source_layers")
    if isinstance(layers, (list, tuple)):
        return [dict(layer) for layer in layers if isinstance(layer, dict)]
    return [{
        "id": str(policy["id"]),
        "label": str(policy["label"]),
        "site_group": str(policy["site_group"]),
    }]


def _is_company_ir_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return (
        host.startswith(("ir.", "investor.", "investors."))
        or any(marker in path for marker in ("/ir/", "/investor", "investor-relations", "investor_relation"))
    )


def _gap_source_validation(key: str, row: dict[str, Any]) -> dict[str, Any]:
    policy = _gap_source_policy(key)
    url = str(row.get("url") or "")
    domain = _domain(url)
    source_role, source_tier = _source_role(domain)
    company_ir = _is_company_ir_url(url)
    matching_layers = [
        layer for layer in _gap_policy_layers(policy)
        if _source_matches_policy(domain, source_role, str(layer["id"]))
    ]
    primary_match = bool(matching_layers)
    credible = primary_match and (
        source_tier == "A" or (policy["id"] == "market_transaction" and source_role == "财经媒体")
    ) and not company_ir
    if company_ir:
        status = "公司IR一手线索，主体待核验，不可直接补分"
    elif credible:
        status = "来源类型匹配"
    elif source_role == "线索来源":
        status = "线索来源，不可作为事实核验"
    elif source_tier == "A":
        status = "权威来源但不匹配该证据类型"
    else:
        status = "来源类型不匹配"
    return {
        "evidence_type": policy["label"],
        "policy_id": policy["id"],
        "primary_source": policy["site_group"],
        "source_layer": str(matching_layers[0].get("id")) if matching_layers else "unmatched",
        "source_layer_label": str(matching_layers[0].get("label")) if matching_layers else "未匹配来源层",
        "company_ir_clue": company_ir,
        "source_role": source_role,
        "source_tier": source_tier,
        "source_match": primary_match,
        "credible": credible,
        "status": status,
    }


def _context_tokens(context: str) -> list[str]:
    ignored = {"综合", "公司", "业务", "行业", "产品", "产业链", "主营"}
    return [
        token for token in re.split(r"[\s、,，/|]+", context)
        if len(token) >= 2 and not token.isdigit() and token not in ignored
    ]


def _gap_body_validation(key: str, row: dict[str, Any], code: str, name: str, context: str) -> dict[str, Any]:
    fetch_status = str(row.get("fetch_status") or "")
    content = str(row.get("content_excerpt") or "")
    if fetch_status != "ok" or not content.strip():
        return {
            "status": "正文未读取",
            "company_match": False,
            "industry_match": False,
            "scope_match": False,
            "signal_match": False,
            "reason": f"正文状态：{fetch_status or '未读取'}",
        }
    policy_id = _gap_source_policy(key)["id"]
    company_match = bool((name and name in content) or (code and code in content))
    industry_match = any(token in content for token in _context_tokens(context))
    scope_match = company_match if policy_id not in {"industry_authority", "chain_crosscheck"} else (company_match or industry_match)
    rule = RULES.get(key, {})
    terms = tuple(rule.get("positive", ())) + tuple(rule.get("negative", ()))
    signal_match = any(term and term.lower() in content.lower() for term in terms)
    if not scope_match:
        reason = "正文未同时指向目标公司或产业范围"
    elif not signal_match:
        reason = "正文未命中该子因子的关键判断词"
    else:
        reason = "正文同时命中对象范围和判断词"
    return {
        "status": "正文已核验" if scope_match and signal_match else "正文关联不足",
        "company_match": company_match,
        "industry_match": industry_match,
        "scope_match": scope_match,
        "signal_match": signal_match,
        "reason": reason,
    }


def _chain_crosscheck_complete(rows: list[dict[str, Any]]) -> bool:
    company_rows = [
        row for row in rows
        if row["source_validation"]["source_role"] == "法定信息披露"
        and row["body_validation"]["company_match"]
    ]
    industry_rows = [
        row for row in rows
        if _matches_domain(_domain(str(row.get("url") or "")), INDUSTRY_AUTHORITY_DOMAINS)
        and row["body_validation"]["industry_match"]
    ]
    domains = {
        _domain(str(row.get("url") or ""))
        for row in company_rows + industry_rows
        if row.get("url")
    }
    return bool(company_rows and industry_rows and len(domains) >= 2)


def _usable_gap_rows(key: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable = [
        row for row in rows
        if row["source_validation"]["credible"] and row["body_validation"]["status"] == "正文已核验"
    ]
    if key == "F1.chokepoint" and not _chain_crosscheck_complete(usable):
        return []
    return usable


def _gap_candidate_rank(key: str, row: dict[str, Any]) -> tuple[int, int, int]:
    validation = _gap_source_validation(key, row)
    source_rank = 0 if validation["credible"] else 1 if validation["source_tier"] == "A" else 2
    role_rank, result_rank = _search_rank(row)
    return source_rank, role_rank, result_rank


def _compact_query_context(context: str) -> str:
    """Keep search queries focused when structured reports repeat Chinese labels."""
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _context_tokens(context):
        normalized = token.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidate = " ".join([*tokens, normalized])
        if len(candidate) > MAX_QUERY_CONTEXT_CHARS:
            break
        tokens.append(normalized)
        if len(tokens) >= 6:
            break
    return " ".join(tokens)


def _query_site_group(key: str, site_group: str) -> str:
    """Use one real site filter instead of passing a long OR expression to public search."""
    domains = re.findall(r"site:([a-z0-9.-]+)", site_group, re.I)
    if not domains:
        return ""
    preferred = {
        "F3.specialized": "miit.gov.cn",
        "F3.background": "cninfo.com.cn",
        "F4.realization": "cninfo.com.cn",
        "F4.business_match": "cninfo.com.cn",
        "F4.profit_position": "cninfo.com.cn",
        "F4.overseas": "cninfo.com.cn",
    }.get(key)
    if preferred and preferred in domains:
        return f"site:{preferred}"
    return f"site:{domains[0]}"


def _gap_policy_queries(key: str, name: str, code: str, context: str) -> list[str]:
    short_context = _compact_query_context(context)
    policy = _gap_source_policy(key)
    plans = policy.get("query_plans")
    if not isinstance(plans, (list, tuple)):
        plans = [{"site_group": policy["site_group"], "query_hint": policy["query_hint"]}]
    queries = [
        f"{name} {code} {short_context} {_query_site_group(key, str(plan.get('site_group') or ''))} {plan.get('query_hint', '')}".strip()
        for plan in plans
        if isinstance(plan, dict)
    ]
    return list(dict.fromkeys(query for query in queries if query))


def _china_finance_query(key: str, name: str, code: str, context: str) -> str:
    return _gap_policy_queries(key, name, code, context)[0]


def _confirmable(row: dict[str, Any]) -> bool:
    return row.get("fetch_status") == "ok" and row.get("source_role") != "线索来源"


def _safe_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def _http_session() -> requests.Session:
    session = getattr(_RUNTIME_LOCAL, "http_session", None)
    if session is None:
        session = requests.Session()
        _RUNTIME_LOCAL.http_session = session
    return session


def _raise_if_search_blocked(response: requests.Response) -> None:
    text = response.text[:20_000].lower()
    if response.status_code == 202 or "anomaly" in text or "unusual traffic" in text:
        raise SearchBackendBlockedError("anti_bot")


def _duckduckgo_lite_search(query: str, timeout: float) -> list[dict[str, Any]]:
    """Use DuckDuckGo Lite, which is less JavaScript- and bandwidth-dependent."""
    response = _http_session().get(
        os.getenv("DDG_LITE_URL", DDG_LITE_URL).strip() or DDG_LITE_URL,
        params={"q": query, "kl": "cn-zh"},
        headers={"User-Agent": PUBLIC_SEARCH_USER_AGENT, "Accept": "text/html"},
        timeout=timeout,
    )
    response.raise_for_status()
    _raise_if_search_blocked(response)
    pattern = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
    rows: list[dict[str, Any]] = []
    for attrs, title_html in pattern.findall(response.text):
        class_match = re.search(r'class=["\'][^"\']*\bresult-link\b[^"\']*["\']', attrs, re.I)
        href_match = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not class_match or not href_match:
            continue
        href = unescape(href_match.group(1))
        title = unescape(re.sub(r"<[^>]+>", " ", title_html))
        title = re.sub(r"\s+", " ", title).strip()
        href = urljoin("https://lite.duckduckgo.com/lite/", href)
        parsed = urlparse(href)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            href = parse_qs(parsed.query).get("uddg", [""])[0] or href
        if not href.startswith(("http://", "https://")):
            continue
        rows.append({"title": title, "url": href, "snippet": "", "date": "", "engine": "DuckDuckGo Lite"})
        if len(rows) >= 8:
            break
    return _prioritize_search_rows(rows)


def _brave_search(query: str, timeout: float, count: int = 8, offset: int = 0) -> list[dict[str, Any]]:
    """Use Brave's paginated web endpoint when the user has configured it."""
    api_key = _secret("BRAVE_SEARCH_API_KEY")
    if not api_key:
        return []
    response = _http_session().get(
        os.getenv("BRAVE_SEARCH_URL", BRAVE_SEARCH_URL).strip() or BRAVE_SEARCH_URL,
        params={
            "q": query,
            "country": "ALL",
            "search_lang": "zh-hans",
            "ui_lang": "zh-CN",
            "count": min(20, max(1, int(count))),
            "offset": max(0, min(9, int(offset))),
            "safesearch": "moderate",
            "extra_snippets": "true",
        },
        headers={"Accept": "application/json", "X-Subscription-Token": api_key, "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    rows = ((response.json().get("web") or {}).get("results") or [])
    return _prioritize_search_rows([
        {
            "title": str(row.get("title") or "").strip(),
            "url": str(row.get("url") or "").strip(),
            "snippet": " ".join(
                [str(row.get("description") or "").strip(), *[str(item).strip() for item in row.get("extra_snippets") or []]]
            ).strip(),
            "date": str(row.get("page_age") or row.get("age") or "").strip(),
            "engine": "Brave Search API",
        }
        for row in rows
        if isinstance(row, dict) and row.get("url")
    ])[:max(1, min(20, int(count)))]


def _response_search_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output_text = str(payload.get("output_text") or "").strip()
    rows: list[dict[str, Any]] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        for source in action.get("sources") or []:
            if isinstance(source, dict) and source.get("url"):
                rows.append({
                    "title": str(source.get("title") or _domain(str(source.get("url"))) or "模型搜索来源"),
                    "url": str(source.get("url")),
                    "snippet": output_text[:500],
                    "date": "",
                    "engine": "OpenAI Responses web_search",
                })
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = str(content.get("text") or "")
            for annotation in content.get("annotations") or []:
                citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
                citation = citation if isinstance(citation, dict) else annotation if isinstance(annotation, dict) else {}
                if citation.get("url"):
                    rows.append({
                        "title": str(citation.get("title") or _domain(str(citation.get("url"))) or "模型搜索引用"),
                        "url": str(citation.get("url")),
                        "snippet": text[:500] or output_text[:500],
                        "date": "",
                        "engine": "OpenAI Responses web_search",
                    })
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        unique.append(row)
    return _prioritize_search_rows(unique)[:8]


def _secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value in {"YOUR_API_KEY_HERE", "***", "<REDACTED>"}:
        return ""
    return value


def _openai_web_search(query: str, timeout: float) -> list[dict[str, Any]]:
    api_key = _secret("OPENAI_API_KEY")
    if not api_key:
        return []
    response = _http_session().post(
        os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses").strip(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("OPENAI_WEB_SEARCH_MODEL", "gpt-5.5").strip() or "gpt-5.5",
            "input": (
                "搜索以下中国A股研究问题。只使用可公开访问的网页，优先巨潮、交易所、政府和主流财经媒体；"
                "返回带引用的简短结果，不要根据常识补写事实。\n" + query
            ),
            "tools": [{"type": "web_search"}],
            "include": ["web_search_call.action.sources"],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return _response_search_rows(response.json())


def _deepseek_web_search_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract only server-returned Web Search URLs from DeepSeek responses."""
    rows: list[dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "web_search_tool_result":
            content = block.get("content") if isinstance(block.get("content"), list) else []
            for source in content:
                if not isinstance(source, dict) or not source.get("url"):
                    continue
                rows.append({
                    "title": str(source.get("title") or _domain(str(source["url"])) or "DeepSeek 搜索来源"),
                    "url": str(source["url"]),
                    "snippet": str(source.get("cited_text") or "")[:500],
                    "date": str(source.get("page_age") or ""),
                    "engine": "DeepSeek Web Search",
                })
        for citation in block.get("citations") or []:
            if not isinstance(citation, dict) or not citation.get("url"):
                continue
            rows.append({
                "title": str(citation.get("title") or _domain(str(citation["url"])) or "DeepSeek 搜索引用"),
                "url": str(citation["url"]),
                "snippet": str(citation.get("cited_text") or "")[:500],
                "date": "",
                "engine": "DeepSeek Web Search",
            })
    seen: set[str] = set()
    return _prioritize_search_rows([
        row for row in rows if row["url"] not in seen and not seen.add(row["url"])
    ])[:8]


def _deepseek_web_search(query: str, timeout: float) -> list[dict[str, Any]]:
    api_key = _secret("DEEPSEEK_API_KEY")
    if not api_key:
        return []
    base_url = os.getenv("DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic").strip().rstrip("/")
    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]
    payload: dict[str, Any] = {}
    for _ in range(2):
        response = _http_session().post(
            base_url + "/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("DEEPSEEK_WEB_SEARCH_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash",
                "max_tokens": 600,
                "system": "只搜索公开网页并返回带 URL 的来源；不要把常识或未引用文字当作事实。",
                "messages": messages,
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("stop_reason") != "pause_turn":
            break
        content = payload.get("content")
        if not isinstance(content, list):
            break
        messages.append({"role": "assistant", "content": content})
    return _deepseek_web_search_rows(payload)


def _model_search_bridge(query: str, timeout: float) -> list[dict[str, Any]]:
    url = os.getenv("MODA_MODEL_SEARCH_URL", "").strip()
    if not url:
        return []
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = _secret("MODA_MODEL_SEARCH_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = _http_session().post(
        url,
        headers=headers,
        json={
            "query": query,
            "max_results": 8,
            "region": "cn-zh",
            "model": os.getenv("MODA_MODEL_SEARCH_MODEL", "").strip(),
            "require_citations": True,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("output"):
        return _response_search_rows(payload)
    raw_rows = payload.get("results") or payload.get("sources") or payload.get("data") or []
    rows = []
    for row in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(row, dict):
            continue
        url_value = str(row.get("url") or row.get("link") or "").strip()
        if not url_value.startswith(("http://", "https://")):
            continue
        rows.append({
            "title": str(row.get("title") or _domain(url_value) or "模型搜索来源"),
            "url": url_value,
            "snippet": str(row.get("snippet") or row.get("summary") or row.get("content") or "")[:1000],
            "date": str(row.get("date") or row.get("published_at") or ""),
            "engine": str(row.get("engine") or row.get("provider") or "Model search bridge"),
        })
    return _prioritize_search_rows(rows)[:8]


def _model_search(provider: str, query: str, timeout: float) -> tuple[str, list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    providers: list[tuple[str, Any]] = []
    if provider in {"auto", "model", "deepseek"}:
        providers.append(("deepseek_web_search", _deepseek_web_search))
    if provider in {"auto", "model", "openai"}:
        providers.append(("openai_web_search", _openai_web_search))
    if provider in {"auto", "model", "bridge"} and os.getenv("MODA_MODEL_SEARCH_URL", "").strip():
        providers.append(("model_search_bridge", _model_search_bridge))
    if not providers:
        return "none", [], ["model_search:not_configured"]
    for name, search in providers:
        try:
            rows = search(query, timeout)
            if rows:
                return name, rows, errors
            errors.append(f"{name}:no_cited_results")
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    return "none", [], errors


def _search_backend_label(provider: str) -> str:
    return {
        "brave": "Brave Search",
        "duckduckgo": "DuckDuckGo",
        "deepseek": "DeepSeek Web Search",
        "openai": "OpenAI Web Search",
        "bridge": "模型搜索网关",
        "model": "模型搜索",
        "auto": "已配置搜索后端",
    }.get(provider, "当前搜索后端")


def _search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict[str, Any]], list[str]]:
    cache_enabled = os.getenv("MODA_SEARCH_CACHE", "on").strip().lower() not in {"0", "false", "off", "no"}
    cache_key = _search_cache_key(provider, query, cache_scope) if cache_scope and cache_enabled else ""
    if cache_key:
        cached = _load_search_cache().get(cache_key)
        if cached and cached.get("date") == datetime.now().date().isoformat() and cached.get("rows"):
            return str(cached.get("used") or "none"), list(cached.get("rows") or []), []
    errors: list[str] = []
    if provider == "brave" and _secret("BRAVE_SEARCH_API_KEY"):
        try:
            rows = _brave_search(query, timeout)
            if rows:
                if cache_key:
                    _save_search_cache(cache_key, "brave", rows)
                return "brave", rows, errors
            errors.append("brave:no_results")
        except Exception as exc:
            errors.append(f"brave:{type(exc).__name__}")
    if provider in {"auto", "duckduckgo"}:
        try:
            rows = _duckduckgo_lite_search(query, timeout)
            if rows:
                if cache_key:
                    _save_search_cache(cache_key, "duckduckgo_lite", rows)
                return "duckduckgo_lite", rows, errors
            errors.append("duckduckgo_lite:no_results")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, SearchBackendBlockedError) else type(exc).__name__
            errors.append(f"duckduckgo_lite:{detail}")
    if provider in {"auto", "deepseek", "openai", "model", "bridge"}:
        used, rows, model_errors = _model_search(provider, query, timeout)
        errors.extend(model_errors)
        if rows:
            if cache_key:
                _save_search_cache(cache_key, used, rows)
            return used, rows, errors
    if not errors:
        errors.append("search_backend_not_configured")
    return "none", [], errors


def _search_cache_key(provider: str, query: str, cache_scope: str = "") -> str:
    configured = "|".join((
        provider,
        os.getenv("DDG_LITE_URL", DDG_LITE_URL).strip(),
        os.getenv("BRAVE_SEARCH_URL", BRAVE_SEARCH_URL).strip(),
        os.getenv("DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic").strip(),
        os.getenv("DEEPSEEK_WEB_SEARCH_MODEL", "deepseek-v4-flash").strip(),
        os.getenv("MODA_MODEL_SEARCH_PROVIDER", "auto").strip(),
        os.getenv("MODA_MODEL_SEARCH_URL", "").strip(),
        os.getenv("MODA_MODEL_SEARCH_MODEL", "").strip(),
        os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses").strip(),
        os.getenv("OPENAI_WEB_SEARCH_MODEL", "").strip(),
    ))
    return sha256(f"{cache_scope}|{configured}|{query.strip()}".encode("utf-8")).hexdigest()


def _load_search_cache() -> dict[str, Any]:
    cache_path = CACHE_PATH.resolve()
    cache_key = str(cache_path)
    with _CACHE_LOCK:
        if cache_key not in _CACHE_PAYLOADS:
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                _CACHE_PAYLOADS[cache_key] = payload if isinstance(payload, dict) else {}
            except (OSError, ValueError, TypeError):
                _CACHE_PAYLOADS[cache_key] = {}
            _CACHE_PATHS[cache_key] = cache_path
        return _CACHE_PAYLOADS[cache_key]


def _flush_search_cache() -> None:
    with _CACHE_LOCK:
        dirty_keys = list(_CACHE_DIRTY)
        for cache_key in dirty_keys:
            path = _CACHE_PATHS[cache_key]
            payload = _CACHE_PAYLOADS[cache_key]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
                _CACHE_DIRTY.discard(cache_key)
            except OSError:
                continue


@contextmanager
def _search_cache_batch():
    global _CACHE_BATCH_DEPTH
    with _CACHE_LOCK:
        _CACHE_BATCH_DEPTH += 1
    try:
        yield
    finally:
        with _CACHE_LOCK:
            _CACHE_BATCH_DEPTH -= 1
            should_flush = _CACHE_BATCH_DEPTH == 0
        if should_flush:
            _flush_search_cache()


def _save_search_cache(key: str, used: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with _CACHE_LOCK:
        payload = _load_search_cache()
        payload[key] = {"date": datetime.now().date().isoformat(), "used": used, "rows": rows[:8]}
        cache_key = str(CACHE_PATH.resolve())
        _CACHE_DIRTY.add(cache_key)
        should_flush = _CACHE_BATCH_DEPTH == 0
    if should_flush:
        _flush_search_cache()


def _gap_relevant(row: dict[str, Any], key: str, code: str, name: str, context: str) -> bool:
    text = " ".join(str(row.get(field) or "") for field in ("title", "snippet"))
    if (name and name in text) or (code and code in text):
        return True
    tokens = [token for token in re.split(r"[\s、,，/|]+", context) if len(token) >= 2 and not token.isdigit()]
    rule = RULES.get(key, {})
    terms = tuple(rule.get("positive", ())) + tuple(rule.get("negative", ()))
    context_hit = any(token in text for token in tokens)
    rule_hit = any(term and term.lower() in text.lower() for term in terms)
    if key.startswith("F1."):
        return context_hit or rule_hit
    return context_hit or rule_hit


def _gap_target_key(target: dict[str, Any]) -> str:
    return f"{target.get('factor_key')}.{target.get('subfactor_key')}"


def _gap_priority(target: dict[str, Any]) -> float:
    key = _gap_target_key(target)
    maximum = float(target.get("maximum") or 0)
    status = str(target.get("original_status") or "")
    status_bonus = 4 if status == "需人工确认" else 2
    return GAP_PRIORITY.get(key, 70) + maximum * 4 + status_bonus


def _select_gap_targets(targets: list[dict[str, Any]], limit: int = MAX_GAP_TARGETS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select high-value gaps while reserving one search slot per factor."""
    if limit <= 0:
        return [], list(targets)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        grouped.setdefault(str(target.get("factor_key") or ""), []).append(target)
    ranked_groups = {
        factor: sorted(items, key=_gap_priority, reverse=True)
        for factor, items in grouped.items()
    }
    selected_keys: set[str] = set()
    selected: list[dict[str, Any]] = []
    for factor in ("F1", "F2", "F3", "F4", "F5"):
        candidate = ranked_groups.get(factor, [])
        if candidate and len(selected) < limit:
            selected.append(candidate[0])
            selected_keys.add(_gap_target_key(candidate[0]))
    ranked_all = sorted(targets, key=_gap_priority, reverse=True)
    for target in ranked_all:
        key = _gap_target_key(target)
        if key in selected_keys or len(selected) >= limit:
            continue
        selected.append(target)
        selected_keys.add(key)
    selected.sort(key=_gap_priority, reverse=True)
    skipped = [target for target in targets if _gap_target_key(target) not in selected_keys]
    return selected, skipped


def _first_pass_target_limit(total: int) -> int:
    """Every unresolved F1-F5 field receives a first-pass search attempt."""
    return max(0, total)


def _allocate_gap_budgets(targets: list[dict[str, Any]], total_seconds: float = MAX_GAP_BUDGET_SECONDS) -> dict[str, float]:
    if not targets:
        return {}
    total = max(0.0, float(total_seconds))
    reserve = min(MIN_TARGET_BUDGET_SECONDS, total / len(targets))
    weights = { _gap_target_key(target): max(1.0, _gap_priority(target)) for target in targets }
    remaining = max(0.0, total - reserve * len(targets))
    weight_total = sum(weights.values()) or 1.0
    return {
        key: round(reserve + remaining * weight / weight_total, 3)
        for key, weight in weights.items()
    }


def _collect_gap_target(
    code: str,
    name: str,
    context: str,
    target: dict[str, Any],
    provider: str,
    timeout: float,
    deadline: float,
    target_budget: float,
) -> dict[str, Any]:
    factor_key = str(target.get("factor_key") or "")
    subfactor_key = str(target.get("subfactor_key") or "")
    key = f"{factor_key}.{subfactor_key}"
    if key not in RULES or factor_key == "F6":
        return {"key": key, "processed": False, "gap_result": None, "results": [], "providers": [], "errors": []}

    target_started = time.monotonic()
    target_deadline = min(deadline, target_started + target_budget)
    # Run the source-policy query first.  With a bounded query budget, adding
    # it after generic rule queries can silently prevent it from ever running.
    custom_queries = [str(query).strip() for query in target.get("queries", []) if str(query).strip()]
    target_queries = [
        *custom_queries,
        *_gap_policy_queries(key, name, code, context),
        *queries_for(key, name, code, context),
    ]
    target_queries = list(dict.fromkeys(query for query in target_queries if query))[:MAX_GAP_QUERIES_PER_TARGET]
    target_errors: list[str] = []
    used_providers: list[str] = []
    candidate_rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for query_index, query in enumerate(target_queries):
        remaining = min(deadline, target_deadline) - time.monotonic()
        if remaining <= 0:
            target_errors.append(
                "global_budget_exhausted"
                if time.monotonic() >= deadline
                else "target_budget_exhausted"
            )
            break
        queries_left = max(1, len(target_queries) - query_index)
        if remaining < 0.2:
            target_errors.append(
                "global_budget_exhausted"
                if time.monotonic() >= deadline
                else "target_budget_exhausted"
            )
            break
        query_timeout = min(timeout, max(0.2, remaining / queries_left))
        used, rows, query_errors = _search(provider, query, query_timeout, cache_scope=f"{code}|{key}")
        relevant = [row for row in rows if _gap_relevant(row, key, code, name, context)]
        if used == "duckduckgo_lite" and not relevant and provider == "auto":
            fallback_remaining = min(deadline, target_deadline) - time.monotonic()
            fallback_timeout = min(timeout, max(0.2, fallback_remaining / 2)) if fallback_remaining >= 0.2 else 0
            fallback_used, fallback_rows, fallback_errors = (
                _search("model", query, fallback_timeout, cache_scope=f"{code}|{key}")
                if fallback_timeout > 0 else (
                    "none",
                    [],
                    ["global_budget_exhausted" if time.monotonic() >= deadline else "target_budget_exhausted"],
                )
            )
            query_errors.extend(fallback_errors)
            if fallback_rows:
                used = fallback_used
                relevant = [row for row in fallback_rows if _gap_relevant(row, key, code, name, context)]
        target_errors.extend(query_errors)
        if used != "none" and used not in used_providers:
            used_providers.append(used)
        for rank, row in enumerate(relevant[:5], 1):
            url = str(row.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            candidate_rows.append({
                **row,
                "factor_key": factor_key,
                "subfactor_key": subfactor_key,
                "query": query,
                "provider": used,
                "rank": rank,
                "fetch_status": "not_fetched_ranked_candidate",
                "content_excerpt": "",
            })

    target_rows = sorted(candidate_rows, key=lambda row: _gap_candidate_rank(key, row))[:5]
    fetch_attempts = 0
    for row in target_rows:
        remaining = min(deadline, target_deadline) - time.monotonic()
        if fetch_attempts < MAX_GAP_EVIDENCE_PAGES_PER_TARGET and remaining > 0:
            fetch_attempts += 1
            fetch_status, content = _fetch_page(url=str(row.get("url") or ""), timeout=min(timeout, 5, max(0.2, remaining)))
            row["fetch_status"] = fetch_status
            row["content_excerpt"] = content[:6000] if content else ""
        elif remaining <= 0:
            row["fetch_status"] = "global_budget_exhausted" if time.monotonic() >= deadline else "target_budget_exhausted"
        if row["fetch_status"] in {"global_budget_exhausted", "target_budget_exhausted"}:
            target_errors.append(str(row["fetch_status"]))
        row["source_validation"] = _gap_source_validation(key, row)
        row["body_validation"] = _gap_body_validation(key, row, code, name, context)

    body_verified_rows = [
        row for row in target_rows
        if row["source_validation"]["credible"] and row["body_validation"]["status"] == "正文已核验"
    ]
    chain_crosscheck_complete = key != "F1.chokepoint" or _chain_crosscheck_complete(body_verified_rows)
    usable_rows = _usable_gap_rows(key, target_rows)
    if usable_rows:
        assessment = evaluate_gap(key, float(target.get("maximum") or 0), usable_rows)
    elif not target_rows:
        hard_errors = [error for error in target_errors if not error.endswith(":no_results")]
        if any(error == "global_budget_exhausted" for error in hard_errors):
            status = "搜索预算耗尽，需人工确认"
        elif any(error == "target_budget_exhausted" for error in hard_errors):
            status = "单目标预算耗尽，需人工确认"
        else:
            status = "搜索失败，需人工确认" if hard_errors else "已搜索未命中"
        assessment = {
            "status": status,
            "score": 0.0,
            "reason": "；".join(hard_errors[:4]) if hard_errors else f"{_search_backend_label(provider)}未返回相关结果",
            "signals": [],
            "conflict": False,
        }
    else:
        source_matched = sum(bool(row["source_validation"]["credible"]) for row in target_rows)
        body_verified = sum(row["body_validation"]["status"] == "正文已核验" for row in target_rows)
        if key == "F1.chokepoint" and not chain_crosscheck_complete:
            status = "产业链双侧未闭环，需人工确认"
            reason = "缺少公司法定披露与产业权威正文的独立双侧确认"
        elif "global_budget_exhausted" in target_errors:
            status = "搜索预算耗尽，需人工确认"
            reason = "全局搜索预算耗尽，已取得的候选未形成可核验正文"
        elif "target_budget_exhausted" in target_errors:
            status = "单目标预算耗尽，需人工确认"
            reason = "该缺口的搜索预算耗尽，已取得的候选未形成可核验正文"
        else:
            status = "搜索结果待正文核验，需人工确认"
            reason = (
                f"候选 {len(target_rows)} 条，来源类型匹配 {source_matched} 条，"
                f"正文核验通过 {body_verified} 条；不以标题或摘要补分"
            )
        assessment = {
            "status": status,
            "score": 0.0,
            "reason": reason,
            "signals": [],
            "conflict": False,
        }
    all_results = [
        {field: value for field, value in row.items() if field != "content_excerpt"}
        for row in target_rows
    ]
    evidence_rows = [
        {
            **{field: row.get(field) for field in ("title", "url", "snippet", "provider", "rank", "fetch_status", "query")},
            "evidence_type": row.get("source_validation", {}).get("evidence_type"),
            "source_layer": row.get("source_validation", {}).get("source_layer"),
            "source_layer_label": row.get("source_validation", {}).get("source_layer_label"),
            "company_ir_clue": row.get("source_validation", {}).get("company_ir_clue", False),
            "source_status": row.get("source_validation", {}).get("status"),
            "body_status": row.get("body_validation", {}).get("status"),
        }
        for row in target_rows[:5]
    ]
    source_policy = _gap_source_policy(key)
    source_layers = [
        {
            "id": str(layer.get("id") or ""),
            "label": str(layer.get("label") or ""),
            "site_group": str(layer.get("site_group") or ""),
        }
        for layer in _gap_policy_layers(source_policy)
    ]
    source_layer_counts = {
        layer["id"]: sum(row["source_validation"].get("source_layer") == layer["id"] for row in target_rows)
        for layer in source_layers
        if layer["id"]
    }
    gap_result = {
        **target,
        **assessment,
        "queries": target_queries,
        "provider": next((row.get("provider") for row in target_rows if row.get("provider")), "none"),
        "evidence": evidence_rows,
        "errors": target_errors,
        "selection_status": "selected",
        "selection_reason": "所有缺口首轮覆盖；总时限内按风险优先级分配执行顺序",
        "evidence_validation": {
            "evidence_type": source_policy["label"],
            "primary_source": source_policy["site_group"],
            "source_layers": source_layers,
            "source_layer_counts": source_layer_counts,
            "company_ir_clue_count": sum(bool(row["source_validation"].get("company_ir_clue")) for row in target_rows),
            "candidate_count": len(target_rows),
            "source_matched_count": sum(bool(row["source_validation"]["credible"]) for row in target_rows),
            "body_verified_count": sum(row["body_validation"]["status"] == "正文已核验" for row in target_rows),
            "crosscheck_status": (
                "双侧已闭环" if key == "F1.chokepoint" and chain_crosscheck_complete
                else "需公司与产业双侧确认" if key == "F1.chokepoint"
                else "不适用"
            ),
            "budget_status": (
                "全局预算耗尽" if "global_budget_exhausted" in target_errors
                else "单目标预算耗尽" if "target_budget_exhausted" in target_errors
                else "完成"
            ),
            "status": "已取得正文核验" if usable_rows else "需人工确认",
        },
        "target_budget_seconds": target_budget,
        "budget_used_seconds": round(min(target_budget, max(0.0, time.monotonic() - target_started)), 3),
    }
    return {
        "key": key,
        "processed": True,
        "gap_result": gap_result,
        "results": all_results,
        "providers": used_providers,
        "errors": target_errors,
    }


def _collect_gap_targets(code: str, name: str, context: str, targets: list[dict[str, Any]],
                         provider: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + MAX_GAP_BUDGET_SECONDS
    gap_results: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    errors: list[str] = []
    used_providers: list[str] = []
    first_pass_limit = _first_pass_target_limit(len(targets))
    selected_targets, skipped_targets = _select_gap_targets(targets, limit=first_pass_limit)
    target_budgets = _allocate_gap_budgets(selected_targets)
    jobs = [
        (
            code,
            name,
            context,
            target,
            provider,
            timeout,
            deadline,
            target_budgets.get(_gap_target_key(target), MIN_TARGET_BUDGET_SECONDS),
        )
        for target in selected_targets
    ]
    with _search_cache_batch():
        with ThreadPoolExecutor(max_workers=min(MAX_GAP_WORKERS, len(jobs) or 1)) as executor:
            target_outputs = list(executor.map(lambda values: _collect_gap_target(*values), jobs))
    processed_targets = [item["key"] for item in target_outputs if item["processed"]]
    for item in target_outputs:
        if item["gap_result"] is not None:
            gap_results.append(item["gap_result"])
        all_results.extend(item["results"])
        for used in item["providers"]:
            if used not in used_providers:
                used_providers.append(used)
        errors.extend(f"{item['key']}:{error}" for error in item["errors"])
    for target in skipped_targets:
        factor_key = str(target.get("factor_key") or "")
        subfactor_key = str(target.get("subfactor_key") or "")
        key = f"{factor_key}.{subfactor_key}"
        gap_results.append({
            **target,
            "status": "搜索失败，需人工确认",
            "score": 0.0,
            "reason": f"超过首轮缺口安全上限 {first_pass_limit}，本目标未分配搜索预算",
            "signals": [],
            "conflict": False,
            "queries": [],
            "provider": "none",
            "evidence": [],
            "evidence_validation": {
                "evidence_type": _gap_source_policy(key)["label"] if key in RULES else "未分类",
                "primary_source": _gap_source_policy(key)["site_group"] if key in RULES else "",
                "source_layers": [
                    {
                        "id": str(layer.get("id") or ""),
                        "label": str(layer.get("label") or ""),
                        "site_group": str(layer.get("site_group") or ""),
                    }
                    for layer in _gap_policy_layers(_gap_source_policy(key))
                ] if key in RULES else [],
                "source_layer_counts": {},
                "company_ir_clue_count": 0,
                "candidate_count": 0,
                "source_matched_count": 0,
                "body_verified_count": 0,
                "budget_status": "未执行",
                "status": "首轮未执行，需人工确认",
            },
            "errors": ["target_limit_exceeded"],
            "selection_status": "skipped",
            "selection_reason": "超过人工配置的首轮缺口安全上限，需下一轮补检",
            "skip_reason": "target_limit_exceeded",
            "target_budget_seconds": 0.0,
            "budget_used_seconds": 0.0,
        })
        errors.append(f"{key}:target_limit_exceeded")
    used_seconds = round(min(MAX_GAP_BUDGET_SECONDS, max(0.0, time.monotonic() - started)), 3)
    exhausted = used_seconds >= MAX_GAP_BUDGET_SECONDS or any(
        "global_budget_exhausted" in error for error in errors
    )
    return {
        "web_research_status": "completed" if gap_results else "unavailable",
        "web_research_provider": ",".join(used_providers) or "none",
        "web_gap_targets": targets,
        "web_gap_results": gap_results,
        "web_subfactor_results": {f"{item['factor_key']}.{item['subfactor_key']}": item for item in gap_results},
        "results": all_results,
        "errors": errors,
        "search_budget": {
            "budget_total_seconds": MAX_GAP_BUDGET_SECONDS,
            "budget_used_seconds": used_seconds,
            "target_limit": first_pass_limit,
            "first_pass_mode": "all_gaps" if first_pass_limit >= len(targets) else "configured_cap",
            "targets_total": len(targets),
            "targets_selected": len(processed_targets),
            "targets_skipped": len(skipped_targets),
            "selection_policy": (
                "所有 F1-F5 未核验缺口首轮覆盖；总时限内按风险、最大分值、缺口状态和因子优先级执行"
                if first_pass_limit >= len(targets)
                else "人工配置首轮安全上限；各 F1-F5 至少保留一个机会，其余按风险、最大分值、缺口状态和因子优先级排序"
            ),
            "allocation_policy": f"每个入选目标至少 {MIN_TARGET_BUDGET_SECONDS:g} 秒，剩余时间按优先级加权分配",
            "global_time_exhausted": exhausted,
            "skip_reasons": {
                "target_limit_exceeded": len(skipped_targets),
                "global_time_exhausted": sum(
                    "global_budget_exhausted" in item.get("errors", [])
                    for item in gap_results
                ),
                "target_time_exhausted": sum(
                    "target_budget_exhausted" in item.get("errors", [])
                    for item in gap_results
                ),
            },
        },
    }


def _read_pdf_text(
    body: bytes,
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_text_chars: int = MAX_PDF_TEXT_CHARS,
) -> str:
    parts: list[str] = []
    text_length = 0
    reader = PdfReader(BytesIO(body))
    for page in reader.pages[:max(1, int(max_pages))]:
        page_text = page.extract_text() or ""
        if not page_text:
            continue
        parts.append(page_text)
        text_length += len(page_text) + 1
        if text_length >= max(1, int(max_text_chars)):
            break
    # Page boundaries let downstream statutory-report parsers attach an
    # auditable page number without changing ordinary full-text matching.
    return "\f".join(parts)[:max(1, int(max_text_chars))]


def _pdf_text_worker(body: bytes, max_pages: int, max_text_chars: int, connection: Any) -> None:
    try:
        connection.send(("ok", _read_pdf_text(body, max_pages=max_pages, max_text_chars=max_text_chars)))
    except Exception as exc:
        connection.send((f"pdf_{type(exc).__name__}", ""))
    finally:
        connection.close()


def _extract_pdf_text(
    body: bytes,
    timeout: float,
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_text_chars: int = MAX_PDF_TEXT_CHARS,
) -> tuple[str, str]:
    if timeout <= 0:
        return "pdf_timeout", ""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_text_worker,
        args=(body, max(1, int(max_pages)), max(1, int(max_text_chars)), send_connection),
    )
    process.daemon = True
    process.start()
    send_connection.close()
    try:
        if receive_connection.poll(timeout):
            status, text = receive_connection.recv()
            process.join(0.2)
            return status, text
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        return "pdf_timeout", ""
    finally:
        receive_connection.close()
        if process.is_alive():
            process.terminate()
            process.join(1)


def fetch_pdf_document(
    url: str,
    timeout: float,
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_text_chars: int = MAX_PDF_TEXT_CHARS,
    max_fetch_bytes: int = MAX_PDF_FETCH_BYTES,
) -> tuple[str, str]:
    """Fetch one public PDF with bounded extraction for statutory-report users."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    current = url
    try:
        for _ in range(4):
            if not _safe_public_url(current):
                return "unsafe_url", ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", ""
            response = _http_session().get(
                current,
                headers={"User-Agent": USER_AGENT},
                timeout=remaining,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "application/pdf" not in content_type and not urlparse(current).path.lower().endswith(".pdf"):
                return "not_pdf", ""
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(32_768):
                if time.monotonic() >= deadline:
                    return "timeout", ""
                total += len(chunk)
                if total > max_fetch_bytes:
                    return "document_too_large", ""
                chunks.append(chunk)
            remaining = deadline - time.monotonic()
            status, text = _extract_pdf_text(
                b"".join(chunks),
                remaining,
                max_pages=max_pages,
                max_text_chars=max_text_chars,
            )
            if status != "ok":
                return status, ""
            return ("ok", text) if text.strip() else ("pdf_no_text", "")
        return "too_many_redirects", ""
    except Exception as exc:
        return type(exc).__name__, ""


def _fetch_page_uncached(url: str, timeout: float) -> tuple[str, str]:
    deadline = time.monotonic() + max(0.0, timeout)
    current = url
    try:
        for _ in range(4):
            if not _safe_public_url(current):
                return "unsafe_url", ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", ""
            response = _http_session().get(
                current,
                headers={"User-Agent": USER_AGENT},
                timeout=remaining,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            is_pdf = "application/pdf" in content_type or urlparse(current).path.lower().endswith(".pdf")
            byte_limit = MAX_PDF_FETCH_BYTES if is_pdf else MAX_FETCH_BYTES
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(32_768):
                if time.monotonic() >= deadline:
                    return "timeout", ""
                total += len(chunk)
                if total > byte_limit:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
            if total > byte_limit:
                return "document_too_large", ""
            if is_pdf:
                remaining = deadline - time.monotonic()
                status, text = _extract_pdf_text(body, remaining)
                if status != "ok":
                    return status, ""
                return ("ok", text[:120_000]) if text.strip() else ("pdf_no_text", "")
            encoding = response.encoding or "utf-8"
            html = body.decode(encoding, errors="replace")
            parser = _TextExtractor()
            parser.feed(html)
            return "ok", " ".join(parser.parts)[:120_000]
        return "too_many_redirects", ""
    except Exception as exc:
        return type(exc).__name__, ""


def _fetch_page(url: str, timeout: float) -> tuple[str, str]:
    started = time.monotonic()
    with _PAGE_SNAPSHOT_LOCK:
        cached = _PAGE_SNAPSHOT.get(url)
        if cached is not None:
            return cached
        event = _PAGE_INFLIGHT.get(url)
        if event is None:
            event = threading.Event()
            _PAGE_INFLIGHT[url] = event
            owner = True
        else:
            owner = False
    if not owner:
        if event.wait(max(0.0, timeout)):
            with _PAGE_SNAPSHOT_LOCK:
                cached = _PAGE_SNAPSHOT.get(url)
            if cached is not None:
                return cached
            remaining = timeout - (time.monotonic() - started)
            if remaining > 0:
                return _fetch_page(url, remaining)
        return "timeout", ""

    result = _fetch_page_uncached(url, timeout)
    with _PAGE_SNAPSHOT_LOCK:
        if result[0] == "ok" and result[1]:
            _PAGE_SNAPSHOT[url] = result
        _PAGE_INFLIGHT.pop(url, None)
        event.set()
    return result


def _reset_run_snapshot() -> None:
    with _PAGE_SNAPSHOT_LOCK:
        _PAGE_SNAPSHOT.clear()
        for event in _PAGE_INFLIGHT.values():
            event.set()
        _PAGE_INFLIGHT.clear()


def _classify(record: dict[str, Any], name: str, context: str = "") -> dict[str, Any]:
    text = str(record.get("content", ""))
    categories = [category for category, terms in SUPPLY_CATEGORIES.items() if any(term in text for term in terms)]
    tightening = any(term in text for term in TIGHTENING_TERMS)
    loosening = any(term in text for term in LOOSENING_TERMS)
    company_relation = bool(name and name in text and any(term in text for term in COMPANY_RELATION_TERMS) and any(term in text for term in REPLACEMENT_TERMS))
    industry_dependency = any(term in text for term in DEPENDENCY_TERMS) and any(term in text for term in REPLACEMENT_TERMS)
    company_named = bool(name and name in text)
    audit_hits = [match.group(0) for pattern in AUDIT_RISK_PATTERNS for match in re.finditer(pattern, text)]
    goodwill_hits = [match.group(0) for pattern in GOODWILL_RISK_PATTERNS for match in re.finditer(pattern, text)]
    risk_signals = {
        "delisting": [term for term in DELISTING_TERMS if term in text],
        "audit": list(dict.fromkeys(audit_hits)),
        "goodwill": list(dict.fromkeys(goodwill_hits)),
    }
    specialized_labels = [term for term in SPECIALIZED_TERMS if term in text]
    catalyst_categories = [category for category, terms in CATALYST_CATEGORIES.items() if any(term in text for term in terms)]
    evidence_date = _extract_evidence_date(record, text)
    domain = _domain(record.get("url", ""))
    source_role, source_tier = _source_role(domain)
    context_tokens = {
        token for token in re.split(r"[\s、,，/|]+", context)
        if len(token) >= 2 and not token.isdigit()
    }
    capex_up = any(term in text for term in CAPEX_UP_TERMS)
    capex_down = any(term in text for term in CAPEX_DOWN_TERMS)
    capex_categories = [category for category, terms in CAPEX_CATEGORIES.items() if any(term in text for term in terms)]
    return {
        **record,
        "domain": domain,
        "source_tier": source_tier,
        "source_role": source_role,
        "supply_categories": categories,
        "supply_direction": "tightening" if tightening and not loosening else "loosening" if loosening and not tightening else "conflict" if tightening and loosening else "unknown",
        "company_product_relation": company_relation,
        "industry_dependency": industry_dependency,
        "company_named": company_named,
        "risk_signals": risk_signals,
        "specialized_labels": specialized_labels,
        "catalyst_categories": catalyst_categories,
        "evidence_date": evidence_date,
        "evidence_fresh": _is_fresh_date(evidence_date),
        "industry_context_match": any(token in text for token in context_tokens),
        "industry_capex_direction": "up" if capex_up and not capex_down else "down" if capex_down and not capex_up else "conflict" if capex_up and capex_down else "unknown",
        "capex_categories": capex_categories,
    }


def _extract_evidence_date(record: dict[str, Any], text: str) -> str:
    candidates = [str(record.get("date") or ""), text[:6000]]
    for candidate in candidates:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", candidate)
        if not match:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            continue
    return ""


def _is_fresh_date(value: str, days: int = 365) -> bool:
    if not value:
        return False
    try:
        age = (date.today() - datetime.strptime(value, "%Y-%m-%d").date()).days
    except ValueError:
        return False
    return 0 <= age <= days


def _validate_supply(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in records if _confirmable(row) and row.get("supply_categories") and row.get("supply_direction") in {"tightening", "loosening"}]
    domains = {row["domain"] for row in usable}
    categories = {category for row in usable for category in row["supply_categories"]}
    has_authority = any(row["source_tier"] == "A" for row in usable)
    directions = {row["supply_direction"] for row in usable}
    # Require independent domains and independent evidence categories. A
    # single article repeating the same signal cannot confirm a cycle.
    confirmed = len(domains) >= 2 and len(categories) >= 2 and has_authority and len(directions) == 1
    category_directions: dict[str, set[str]] = {}
    for row in usable:
        for category in row.get("supply_categories", []):
            category_directions.setdefault(category, set()).add(row["supply_direction"])
    category_conflict = any(len(values) > 1 for values in category_directions.values())
    if category_conflict:
        confirmed = False
    return {
        "status": "已验证" if confirmed else "证据冲突" if len(directions) > 1 or category_conflict else "需人工确认",
        "evidence_count": len(usable),
        "domain_count": len(domains),
        "categories": sorted(categories),
        "has_authority": has_authority,
        "tightening": next(iter(directions)) == "tightening" if confirmed else None,
        "reason": "两个不同域名、两类证据且含权威来源同向" if confirmed else "未满足双域名、双类别、权威来源和同向要求，或同一类别方向冲突",
    }


def _validate_chokepoint(records: list[dict[str, Any]]) -> dict[str, Any]:
    company_rows = [row for row in records if _confirmable(row) and row.get("company_product_relation")]
    industry_rows = [row for row in records if _confirmable(row) and row.get("industry_dependency")]
    domains = {row["domain"] for row in company_rows + industry_rows}
    has_authority = any(row["source_tier"] == "A" for row in company_rows + industry_rows)
    confirmed = bool(company_rows and industry_rows and len(domains) >= 2 and has_authority)
    return {
        "status": "已验证" if confirmed else "需人工确认",
        "company_evidence_count": len(company_rows),
        "industry_evidence_count": len(industry_rows),
        "domain_count": len(domains),
        "has_authority": has_authority,
        "score": 80 if confirmed else None,
        "reason": "公司产品关系与行业进口依赖由不同来源交叉确认" if confirmed else "缺少公司产品关系、行业依赖、独立域名或权威来源",
    }


def _validate_risk(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A" and row.get("company_named")
        and any(row.get("risk_signals", {}).values())
    ]
    return {
        "status": "已验证" if usable else "需人工确认",
        "evidence_count": len(usable),
        "st_risk": any(row.get("risk_signals", {}).get("delisting") for row in usable) or None,
        "audit_risk": any(row.get("risk_signals", {}).get("audit") for row in usable) or None,
        "goodwill_risk": any(row.get("risk_signals", {}).get("goodwill") for row in usable) or None,
        "reason": "权威正文命中公司退市、审计或商誉风险" if usable else "未取得命中公司名称和风险事项的权威正文；不能以无搜索结果证明无风险",
    }


def _validate_specialized(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A"
        and row.get("company_named") and row.get("specialized_labels")
    ]
    labels = sorted({label for row in usable for label in row.get("specialized_labels", [])})
    strength = 1.0 if any(label in {"专精特新小巨人", "制造业单项冠军", "单项冠军"} for label in labels) else 0.75 if labels else None
    return {
        "status": "已验证" if usable else "需人工确认",
        "evidence_count": len(usable),
        "labels": labels,
        "strength": strength,
        "reason": "政府、协会或交易所权威正文确认公司资质" if usable else "缺少同时包含公司名称和资质名称的权威正文",
    }


def _validate_catalysts(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A"
        and row.get("company_named") and row.get("catalyst_categories") and row.get("evidence_fresh")
    ]
    categories = sorted({category for row in usable for category in row.get("catalyst_categories", [])})
    return {
        "status": "已验证" if usable else "需人工确认",
        "evidence_count": len(usable),
        "verified_count": min(2, len(categories)) if usable else None,
        "categories": categories,
        "reason": "一年内权威正文确认公司具体催化事件" if usable else "缺少公司关系、权威正文、具体事件或有效日期",
    }


def _validate_industry_capex(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A"
        and row.get("industry_context_match") and row.get("evidence_fresh")
        and row.get("industry_capex_direction") in {"up", "down"}
    ]
    domains = {row.get("domain") for row in usable if row.get("domain")}
    directions = {row["industry_capex_direction"] for row in usable}
    # Industry capex is only confirmable when the sources are both fresh and
    # independent. Require two distinct authority domains and two evidence
    # categories (investment/expansion/equipment) where available.
    capex_categories = {category for row in usable for category in row.get("capex_categories", [])}
    confirmed = len(domains) >= 2 and len(directions) == 1 and (len(capex_categories) >= 2 or not capex_categories)
    direction = next(iter(directions)) if confirmed else None
    return {
        "status": "已验证" if confirmed else "证据冲突" if len(directions) > 1 else "需人工确认",
        "evidence_count": len(usable),
        "domain_count": len(domains),
        "signal": "上行" if direction == "up" else "下行" if direction == "down" else None,
        "reason": "两家独立权威来源的一年内正文同向确认行业投资" if confirmed else "未满足行业匹配、有效日期、双权威域名、双类别和同向要求",
    }


def _sector_source_matches_policy(domain: str, source_role: str, policy: str) -> bool:
    if policy == "industry_authority":
        return _matches_domain(domain, INDUSTRY_AUTHORITY_DOMAINS)
    if policy == "overseas_first_party":
        return _matches_domain(domain, OVERSEAS_FIRST_PARTY_DOMAINS)
    if policy == "technology_authority":
        return _matches_domain(domain, PATENT_STANDARD_DOMAINS) or _matches_domain(domain, INDUSTRY_AUTHORITY_DOMAINS)
    if policy == "company_disclosure":
        return source_role == "法定信息披露"
    if policy == "market_data":
        return (
            _matches_domain(domain, MARKET_DATA_DOMAINS)
            or _matches_domain(domain, MARKET_FACT_DOMAINS)
            or source_role == "财经媒体"
        )
    return False


def _sector_source_validation(spec: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    domain = _domain(str(row.get("url") or ""))
    source_role, source_tier = _source_role(domain)
    matches = [
        policy for policy in spec.get("source_policies", ())
        if _sector_source_matches_policy(domain, source_role, str(policy))
    ]
    market_fact = _matches_domain(domain, MARKET_DATA_DOMAINS) or _matches_domain(domain, MARKET_FACT_DOMAINS)
    return {
        "domain": domain,
        "source_role": source_role,
        "source_tier": source_tier,
        "source_match": bool(matches),
        "primary_source": bool(matches) and ("market_data" not in matches or market_fact),
        "matched_policies": matches,
        "status": (
            "来源类型匹配" if matches and ("market_data" not in matches or market_fact)
            else "财经媒体线索，需市场数据交叉确认" if matches
            else "来源类型不匹配"
        ),
    }


def _sector_scope_terms(sector: str, context: str) -> list[str]:
    terms = [sector.strip(), *_context_tokens(context)]
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))


def _sector_text_matches(spec: dict[str, Any], text: str, sector: str, context: str) -> tuple[bool, list[str]]:
    lowered = text.lower()
    scope_match = any(term.lower() in lowered for term in _sector_scope_terms(sector, context))
    matched_terms = [
        str(term) for term in spec.get("signal_terms", ())
        if str(term).lower() in lowered
    ]
    # Search titles are often terse.  Fetch sector-scoped candidates first,
    # then require a section signal from the full page before using the source.
    return scope_match, matched_terms


def _sector_body_validation(spec: dict[str, Any], row: dict[str, Any], sector: str, context: str) -> dict[str, Any]:
    if row.get("fetch_status") != "ok":
        return {"status": "正文未读取", "matched_terms": []}
    content = str(row.get("content_excerpt") or "")
    if not content.strip():
        return {"status": "正文未读取", "matched_terms": []}
    scope_match = any(term.lower() in content.lower() for term in _sector_scope_terms(sector, context))
    matched_terms = [
        str(term) for term in spec.get("signal_terms", ())
        if str(term).lower() in content.lower()
    ]
    if not scope_match:
        return {"status": "正文范围不匹配", "matched_terms": []}
    if not matched_terms:
        return {"status": "正文未命中本节信号", "matched_terms": []}
    return {"status": "正文已核验", "matched_terms": matched_terms}


def _sector_evidence_ref(key: str, index: int) -> str:
    return f"web_research.sector.{key}.{index}"


def _sector_excerpt(row: dict[str, Any], terms: list[str]) -> str:
    content = re.sub(r"\s+", " ", str(row.get("content_excerpt") or "")).strip()
    if not content:
        return ""
    for sentence in re.split(r"(?<=[。！？!?])", content):
        value = sentence.strip()
        if value and any(term.lower() in value.lower() for term in terms):
            return value[:180]
    return content[:180]


def _sector_empty_section(spec: dict[str, Any], reason: str, *, ref: str | None = None) -> dict[str, Any]:
    key = str(spec["key"])
    label = str(spec["label"])
    evidence_refs = [ref] if ref else [f"web_research.sector.{key}"]
    return {
        "status": "需人工确认",
        "summary": f"未取得可正文核验的“{label}”证据，不能用标题、摘要或主题热度代替事实。",
        "evidence_refs": evidence_refs,
        "unknowns": [{"item": label, "reason": reason, "evidence_refs": evidence_refs}],
    }


def _sector_candidate_rank(spec: dict[str, Any], row: dict[str, Any]) -> tuple[int, int, int]:
    validation = _sector_source_validation(spec, row)
    return (0 if validation["source_match"] else 1, *_search_rank(row))


def _collect_sector_section(
    sector: str,
    context: str,
    spec: dict[str, Any],
    provider: str,
    timeout: float,
    deadline: float,
    entity_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = str(spec["key"])
    label = str(spec["label"])
    short_context = " ".join(_context_tokens(context)[:6])
    queries: list[str] = []
    adaptive_specs = sector_search_planner.section_query_specs(
        sector,
        context,
        key,
        entity_context=entity_context,
    )
    for site_group, hint in (*adaptive_specs, *spec.get("query_specs", ())):
        query = f"{sector} {short_context} {site_group} {hint}".strip()
        if query not in queries:
            queries.append(query)
    queries = queries[:MAX_SECTOR_QUERIES_PER_SECTION]

    query_rows: list[dict[str, str]] = []
    candidate_rows: list[dict[str, Any]] = []
    used_providers: list[str] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    for query_index, query in enumerate(queries):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("global_budget_exhausted")
            break
        query_timeout = min(timeout, max(0.2, remaining / max(1, len(queries) - query_index)))
        used, rows, query_errors = _search(provider, query, query_timeout, cache_scope=f"sector|{sector}|{key}")
        query_rows.append({"section": key, "query": query, "provider": used})
        errors.extend(query_errors)
        if used != "none" and used not in used_providers:
            used_providers.append(used)
        for rank, row in enumerate(rows[:5], start=1):
            text = " ".join(str(row.get(field) or "") for field in ("title", "snippet"))
            relevant, _ = _sector_text_matches(spec, text, sector, context)
            url = str(row.get("url") or "")
            if not relevant or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            candidate_rows.append({
                **row,
                "query": query,
                "provider": used,
                "rank": rank,
                "fetch_status": "not_fetched_ranked_candidate",
                "content_excerpt": "",
            })

    candidates = sorted(candidate_rows, key=lambda row: _sector_candidate_rank(spec, row))[:5]
    for index, row in enumerate(candidates, start=1):
        row["evidence_ref"] = _sector_evidence_ref(key, index)
    fetch_attempts = 0
    for row in candidates:
        row["source_validation"] = _sector_source_validation(spec, row)
        remaining = deadline - time.monotonic()
        if row["source_validation"]["source_match"] and fetch_attempts < MAX_SECTOR_EVIDENCE_PAGES_PER_SECTION and remaining > 0:
            fetch_attempts += 1
            fetch_status, content = _fetch_page(str(row.get("url") or ""), min(timeout, 5, max(0.2, remaining)))
            row["fetch_status"] = fetch_status
            row["content_excerpt"] = content[:6000] if content else ""
        elif remaining <= 0:
            row["fetch_status"] = "global_budget_exhausted"
            errors.append("global_budget_exhausted")
        row["body_validation"] = _sector_body_validation(spec, row, sector, context)

    usable = [
        row for row in candidates
        if row["source_validation"]["source_match"]
        and row["body_validation"]["status"] == "正文已核验"
    ]
    domains = {str(row["source_validation"].get("domain") or "") for row in usable}
    domains.discard("")
    directions: set[str] = set()
    positive_terms = tuple(spec.get("positive_terms", ()))
    negative_terms = tuple(spec.get("negative_terms", ()))
    if positive_terms or negative_terms:
        for row in usable:
            content = str(row.get("content_excerpt") or "").lower()
            positive = sum(term.lower() in content for term in positive_terms)
            negative = sum(term.lower() in content for term in negative_terms)
            if positive > negative:
                directions.add("偏紧")
            elif negative > positive:
                directions.add("偏松")
    direction_conflict = len(directions) > 1
    confirmed = (
        len(usable) >= 2
        and len(domains) >= 2
        and any(row["source_validation"]["primary_source"] for row in usable)
        and not direction_conflict
    )
    source_refs = [str(row["evidence_ref"]) for row in usable]
    if confirmed:
        status = "已验证"
        summary = f"“{label}”已有 {len(usable)} 条不同来源的正文交叉覆盖。"
        unknowns: list[dict[str, Any]] = []
    elif usable:
        status = "部分验证"
        reason = (
            "正文信号方向不一致，尚不能判断供需方向。"
            if direction_conflict else
            "财经媒体只能作为市场线索，仍需市场数据或官方市场事实交叉确认。"
            if usable and not any(row["source_validation"]["primary_source"] for row in usable) else
            f"已取得 {len(usable)} 条来源类型匹配的正文，但未满足两个独立来源交叉确认。"
        )
        summary = f"“{label}”已有可核验正文线索，仍需补充独立来源。"
        unknowns = [{"item": label, "reason": reason, "evidence_refs": source_refs}]
    else:
        reason = "搜索未取得来源类型匹配且正文范围匹配的证据，需人工确认。"
        section = _sector_empty_section(spec, reason)
        source_records = []
        for index, row in enumerate(candidates, start=1):
            source_records.append({
                "evidence_ref": row["evidence_ref"],
                "section": key,
                "title": row.get("title", ""),
                "url": row.get("url", ""),
                "snippet": row.get("snippet", ""),
                "query": row.get("query", ""),
                "provider": row.get("provider", "none"),
                "fetch_status": row.get("fetch_status", ""),
                "source_status": row["source_validation"].get("status", "未核验"),
                "body_status": row["body_validation"].get("status", "正文未读取"),
                "matched_terms": row["body_validation"].get("matched_terms", []),
            })
        return {"section": section, "sources": source_records, "queries": query_rows, "providers": used_providers, "errors": errors}

    for index, row in enumerate(usable, start=1):
        excerpt = _sector_excerpt(row, list(row["body_validation"].get("matched_terms") or []))
        if excerpt:
            summary += f" 例如：{excerpt}"
            break
    section = {
        "status": status,
        "summary": summary,
        "evidence_refs": source_refs,
        "unknowns": unknowns,
    }
    source_records = []
    for index, row in enumerate(candidates, start=1):
        body = row["body_validation"]
        source_records.append({
            "evidence_ref": row["evidence_ref"],
            "section": key,
            "title": row.get("title", ""),
            "url": row.get("url", ""),
            "snippet": row.get("snippet", ""),
            "query": row.get("query", ""),
            "provider": row.get("provider", "none"),
            "fetch_status": row.get("fetch_status", ""),
            "source_status": row["source_validation"].get("status", "未核验"),
            "body_status": body.get("status", "正文未读取"),
            "matched_terms": body.get("matched_terms", []),
            "excerpt": _sector_excerpt(row, list(body.get("matched_terms") or [])),
        })
    return {"section": section, "sources": source_records, "queries": query_rows, "providers": used_providers, "errors": errors}


def collect_sector_evidence(
    sector: str,
    *,
    context: str = "",
    provider: str | None = None,
    timeout: float = 12,
    screening: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect traceable industry evidence without producing a sector verdict.

    The returned sections are deliberately limited to verified, partial, or
    manual-confirmation states.  `tools.sector_analysis` owns the later
    industry judgment and candidate ordering.
    """
    sector_name = str(sector or "").strip()
    if not sector_name:
        raise ValueError("sector 不能为空")
    selected = (provider or os.getenv("MODA_SEARCH_PROVIDER", "auto")).strip().lower()
    if selected not in {"auto", "brave", "duckduckgo", "deepseek", "openai", "model", "bridge", "off"}:
        selected = "off"
    if selected == "off":
        return {
            "sector": sector_name,
            "web_research_status": "disabled",
            "web_research_provider": "off",
            "queries": [],
            "sources": [],
            "errors": [],
            "search_budget": {"budget_total_seconds": 0.0, "budget_used_seconds": 0.0, "sections_total": len(SECTOR_EVIDENCE_SPECS), "sections_completed": 0, "global_time_exhausted": False},
            "sections": {key["key"]: _sector_empty_section(key, "行业网页搜索已关闭，需人工确认。") for key in SECTOR_EVIDENCE_SPECS},
        }

    entity_resolution = (
        sector_search_planner.resolve_entity_context(sector_name, screening=screening)
        if screening
        else None
    )
    _reset_run_snapshot()
    started = time.monotonic()
    deadline = started + MAX_SECTOR_BUDGET_SECONDS
    try:
        request_timeout = max(0.2, float(timeout))
    except (TypeError, ValueError):
        request_timeout = 12.0
    jobs = [
        (sector_name, context, spec, selected, request_timeout, deadline, entity_resolution)
        for spec in SECTOR_EVIDENCE_SPECS
    ]
    with _search_cache_batch():
        with ThreadPoolExecutor(max_workers=min(MAX_SECTOR_WORKERS, len(jobs))) as executor:
            outputs = list(executor.map(lambda values: _collect_sector_section(*values), jobs))

    sections: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    queries: list[dict[str, str]] = []
    providers: list[str] = []
    errors: list[str] = []
    for spec, output in zip(SECTOR_EVIDENCE_SPECS, outputs):
        key = str(spec["key"])
        sections[key] = output["section"]
        sources.extend(output["sources"])
        queries.extend(output["queries"])
        for value in output["providers"]:
            if value not in providers:
                providers.append(value)
        errors.extend(f"{key}:{error}" for error in output["errors"])
    used_seconds = round(min(MAX_SECTOR_BUDGET_SECONDS, max(0.0, time.monotonic() - started)), 3)
    return {
        "sector": sector_name,
        "entity_resolution": entity_resolution or {"status": "not_provided"},
        "web_research_status": "completed",
        "web_research_provider": ",".join(providers) or "none",
        "queries": queries,
        "sources": sources,
        "errors": list(dict.fromkeys(errors)),
        "search_budget": {
            "budget_total_seconds": MAX_SECTOR_BUDGET_SECONDS,
            "budget_used_seconds": used_seconds,
            "sections_total": len(SECTOR_EVIDENCE_SPECS),
            "sections_completed": len(outputs),
            "global_time_exhausted": used_seconds >= MAX_SECTOR_BUDGET_SECONDS or any("global_budget_exhausted" in error for error in errors),
        },
        "sections": sections,
    }


def collect(code: str, name: str, context: str, provider: str | None = None, timeout: float = 12,
            targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _reset_run_snapshot()
    selected = (provider or os.getenv("MODA_SEARCH_PROVIDER", "auto")).strip().lower()
    if selected not in {"auto", "brave", "duckduckgo", "deepseek", "openai", "model", "bridge", "off"}:
        selected = "off"
    if selected == "off":
        return {"web_research_status": "disabled", "web_research_provider": "off", "queries": [], "results": [], "errors": []}
    classification_db = lookup(code, name)
    database_specialized = has_category(classification_db, "specialized")
    database_leadership = (
        has_category(classification_db, "leadership")
        or has_category(classification_db, "core_supplier")
    )
    if targets is not None:
        skipped_database_targets = [
            target for target in targets
            if (
                database_specialized
                and target.get("factor_key") == "F3"
                and target.get("subfactor_key") == "specialized"
            )
            or (
                database_leadership
                and target.get("factor_key") == "F3"
                and target.get("subfactor_key") == "leadership"
            )
        ]
        filtered_targets = [
            target for target in targets
            if target not in skipped_database_targets
        ]
        result = _collect_gap_targets(code, name, context, filtered_targets, selected, timeout)
        result["classification_db_found"] = bool(classification_db.get("found"))
        result["classification_db_categories"] = list(classification_db.get("categories") or ())
        result["classification_db_specialized"] = database_specialized
        result["classification_db_leadership"] = database_leadership
        result["database_skipped_targets"] = skipped_database_targets
        return result

    short_context = " ".join(context.split()[:12])
    query_specs = [
        ("supply", f"{name} {short_context} 供不应求 订单 产能 库存"),
        ("chokepoint", f"{name} {short_context} 国产替代 核心供应商 进口依赖"),
        ("chokepoint", f"site:cninfo.com.cn {name} 订单 产能 国产替代"),
        ("risk", f"site:cninfo.com.cn {name} 退市 审计意见 商誉减值"),
        ("risk", f"site:szse.cn {code} {name} 风险警示 审计 商誉"),
        ("specialized", f"site:gov.cn {name} 专精特新 小巨人 单项冠军"),
        ("specialized", f"site:miit.gov.cn {name} 专精特新 单项冠军"),
        ("capex", f"site:stats.gov.cn {short_context} 固定资产投资 投资增长 产能"),
        ("capex", f"site:miit.gov.cn OR site:ndrc.gov.cn {short_context} 投资 扩产 设备更新"),
        ("finance_disclosure", f"{name} {short_context} {CHINA_FINANCE_SITE_GROUPS['disclosure']} 年报 季报 公告 主营业务"),
        ("finance_market", f"{name} {short_context} {CHINA_FINANCE_SITE_GROUPS['market']} 订单 价格 产能 估值"),
    ]
    if database_specialized or database_leadership:
        query_specs = [
            item for item in query_specs
            if not (database_specialized and item[0] == "specialized")
            and not (database_leadership and item[0] == "leadership")
        ]
    queries = [query for _, query in query_specs]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    used_providers: list[str] = []
    seen: set[str] = set()
    purpose_counts: dict[str, int] = {}
    for purpose, query in query_specs:
        used, rows, query_errors = _search(selected, query, timeout)
        errors.extend(f"{query[:24]}:{error}" for error in query_errors)
        if used != "none" and used not in used_providers:
            used_providers.append(used)
        for row in rows:
            url = row.get("url", "")
            if (not url or url in seen or len(results) >= MAX_PAGES
                    or purpose_counts.get(purpose, 0) >= MAX_PAGES_PER_PURPOSE):
                continue
            seen.add(url)
            purpose_counts[purpose] = purpose_counts.get(purpose, 0) + 1
            fetch_status, content = _fetch_page(url, timeout)
            classified = _classify({**row, "purpose": purpose, "query": query, "fetch_status": fetch_status, "content": content}, name, context)
            classified.pop("content", None)
            results.append(classified)

    supply = _validate_supply(results)
    chokepoint = _validate_chokepoint(results)
    risk = _validate_risk(results)
    specialized = _validate_specialized(results)
    industry_capex = _validate_industry_capex(results)
    status = "completed" if results else "unavailable"
    return {
        "web_research_status": status,
        "web_research_provider": ",".join(used_providers) or "none",
        "classification_db_found": bool(classification_db.get("found")),
        "classification_db_categories": list(classification_db.get("categories") or ()),
        "classification_db_specialized": database_specialized,
        "classification_db_leadership": database_leadership,
        "queries": queries,
        "results": results,
        "errors": errors,
        "web_supply_validation": supply,
        "web_chokepoint_validation": chokepoint,
        "web_risk_validation": risk,
        "web_specialized_validation": specialized,
        "web_industry_capex_validation": industry_capex,
    }


def build_report(code: str, name: str, data: dict[str, Any]) -> str:
    if data.get("web_gap_results") is not None:
        lines = [
            f"# 定向搜索补缺：{name or code}（{code}）",
            "",
            f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  后端：{data.get('web_research_provider', 'none')}",
            "",
            f"<!-- moda_web_research: {json.dumps(data, ensure_ascii=False)} -->",
            "",
            "| 因子 | 子因子 | 原状态 | 选择状态 | 搜索结果 | 证据类型/正文核验 | 未核验得分 | 目标预算 | 实际用时 | 后端 | 判断依据 |",
            "|---|---|---|---|---|---|---:|---:|---:|---|---|",
        ]
        for item in data.get("web_gap_results", []):
            reason = str(item.get("reason") or "").replace("|", "/")
            lines.append(
                f"| {item.get('factor_key')} | {item.get('label')} | {item.get('original_status')} | "
                f"{item.get('selection_status', 'selected')} | {item.get('status')} | "
                f"{(item.get('evidence_validation') or {}).get('evidence_type', '未分类')}/"
                f"{(item.get('evidence_validation') or {}).get('status', '需人工确认')} | "
                f"{item.get('score', 0):g}/{item.get('maximum', 0):g} | "
                f"{item.get('target_budget_seconds', 0):g} | {item.get('budget_used_seconds', 0):g} | "
                f"{item.get('provider', 'none')} | {reason} |"
            )
        lines += ["", "## 搜索明细", "", "| 子因子 | 标题 | URL | 查询词 | 后端 | 来源核验 | 正文核验 |", "|---|---|---|---|---|---|---|"]
        for item in data.get("web_gap_results", []):
            for row in item.get("evidence", []):
                title = str(row.get("title") or "").replace("|", "/")
                query = str(row.get("query") or "").replace("|", "/")
                lines.append(
                    f"| {item.get('factor_key')}.{item.get('subfactor_key')} | {title} | {row.get('url', '')} | "
                    f"{query} | {row.get('provider', '')} | {row.get('source_status', '未核验')} | "
                    f"{row.get('body_status', '正文未读取')} |"
                )
        budget = data.get("search_budget") or {}
        lines += [
            "",
            "## 搜索预算",
            "",
            f"- 总预算：{budget.get('budget_total_seconds', MAX_GAP_BUDGET_SECONDS):g} 秒；实际使用：{budget.get('budget_used_seconds', 0):g} 秒。",
            f"- 缺口目标：共 {budget.get('targets_total', 0)} 个；入选 {budget.get('targets_selected', 0)} 个；未入选 {budget.get('targets_skipped', 0)} 个；全局时间耗尽：{'是' if budget.get('global_time_exhausted') else '否'}。",
            f"- 首轮覆盖：{'所有缺口' if budget.get('first_pass_mode') == 'all_gaps' else '按安全上限'}；目标上限 {budget.get('target_limit', MAX_GAP_TARGETS)}。",
            f"- 分配规则：{budget.get('selection_policy', '按优先级选择')}；{budget.get('allocation_policy', '按优先级分配剩余时间')}。",
            f"- 预算原因：目标数量上限 {((budget.get('skip_reasons') or {}).get('target_limit_exceeded', 0))} 个；全局时间耗尽 {((budget.get('skip_reasons') or {}).get('global_time_exhausted', 0))} 个；单目标时间耗尽 {((budget.get('skip_reasons') or {}).get('target_time_exhausted', 0))} 个。",
            "",
            "搜索结果只用于未核验补缺；来源类型与正文均未通过时不得以标题或摘要补分，结构化数据优先，F6 不使用网页补分。",
            "",
        ]
        return "\n".join(lines)
    lines = [
        f"# 搜索交叉验证：{name or code}（{code}）",
        "",
        f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  后端：{data.get('web_research_provider', 'none')}",
        "",
        f"<!-- moda_web_research: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 运行状态：{data.get('web_research_status')}",
        f"- 供需验证：{data.get('web_supply_validation', {}).get('status', '需人工确认')}；{data.get('web_supply_validation', {}).get('reason', '搜索未运行')}",
        f"- 国产替代验证：{data.get('web_chokepoint_validation', {}).get('status', '需人工确认')}；{data.get('web_chokepoint_validation', {}).get('reason', '搜索未运行')}",
        f"- 退市/审计/商誉验证：{data.get('web_risk_validation', {}).get('status', '需人工确认')}；{data.get('web_risk_validation', {}).get('reason', '搜索未运行')}",
        f"- 专精特新/单项冠军验证：{data.get('web_specialized_validation', {}).get('status', '需人工确认')}；{data.get('web_specialized_validation', {}).get('reason', '搜索未运行')}",
        f"- 行业资本开支验证：{data.get('web_industry_capex_validation', {}).get('status', '需人工确认')}；{data.get('web_industry_capex_validation', {}).get('reason', '搜索未运行')}",
        "",
        "| 用途 | 来源角色 | 来源等级 | 标题 | 域名 | 正文 | 证据日期 | 查询词 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in data.get("results", []):
        title = str(row.get("title", "")).replace("|", "/")
        query = str(row.get("query", "")).replace("|", "/")
        lines.append(f"| {row.get('purpose', '')} | {row.get('source_role', '一般来源')} | {row.get('source_tier', 'B')} | [{title}]({row.get('url', '')}) | {row.get('domain', '')} | {row.get('fetch_status', '')} | {row.get('evidence_date', '') or '未识别'} | {query} |")
    if not data.get("results"):
        lines.append("| - | - | - | 无可核验结果 | - | - | - | - |")
    lines += ["", "法定信息披露平台正文可作为高确信度证据；雪球、东方财富、大智慧等金融论坛只收集线索，不参与确认或计分。搜索摘要只用于发现线索；正文未成功读取或未通过交叉验证时不得计分。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate framework evidence with optional web search")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--targets-json", default="")
    parser.add_argument("--refresh", action="store_true", help="Bypass same-day successful search cache")
    args = parser.parse_args()
    if args.refresh:
        os.environ["MODA_SEARCH_CACHE"] = "off"
    code = args.stock.strip()
    targets = json.loads(args.targets_json) if args.targets_json else None
    with _search_cache_batch():
        data = collect(code, args.name or code, args.context, args.provider, targets=targets)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{code}.md"
    path.write_text(build_report(code, args.name or code, data), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
