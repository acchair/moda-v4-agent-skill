from __future__ import annotations

from typing import Any, Iterable

from tools.scoring.model import Scorecard


def _known(evidence: dict[str, Any], keys: Iterable[str]) -> bool:
    return all(evidence.get(key) not in (None, "", []) for key in keys)


def _row(name: str, usefulness: str, status: str, reason: str) -> dict[str, str]:
    return {"method": name, "usefulness": usefulness, "status": status, "reason": reason}


def evaluate(evidence: dict[str, Any], card: Scorecard) -> list[dict[str, str]]:
    subfactors = [item for factor in card.factors for item in factor.subfactors]
    unresolved = {"需人工确认", "已搜索未命中", "搜索失败，需人工确认"}
    covered = sum(item.status not in unresolved for item in subfactors)
    coverage = covered / len(subfactors) if subfactors else 0
    comps_ready = _known(evidence, ("pe_ttm", "peer_pe_ttm_median"))
    earnings_ready = any(_known(evidence, pair) for pair in (("revenue_yoy", "profit_yoy"), ("revenue_yoy_delta", "profit_yoy_delta")))
    catalyst_checked = evidence.get("verified_catalyst_count") is not None
    merger_ready = evidence.get("merger_event_verified") is True
    valuation_ready = comps_ready or _known(evidence, ("pe_ttm", "pb"))
    dcf_data_ready = _known(evidence, ("free_cash_flow", "net_debt", "total_shares"))
    factor_ready = coverage >= 0.60
    alpha_check = evidence.get("alpha_crosscheck") or {}
    alpha_methods = {item.get("method"): item for item in alpha_check.get("methods", [])}
    quant = alpha_methods.get("量化选股筛选", {})
    thesis = alpha_methods.get("投资逻辑追踪", {})

    return [
        _row(
            "DCF 估值",
            "条件适用",
            "待假设" if dcf_data_ready else "待数据",
            "自由现金流、净债务和总股本已取得；仍需审查增长率、终值和WACC假设"
            if dcf_data_ready else "需要自由现金流、净债务、总股本和经审查的增长/WACC假设；禁止用市值反推FCF",
        ),
        _row("Comps 同行估值", "高", "已启用" if comps_ready else "待数据", "PE/PB 与同行中位数交叉验证，不改变六层得分" if comps_ready else "缺少目标或同行可比估值"),
        _row("三表预测", "条件适用", "待数据", "需要完整利润表、资产负债表、现金流量表和资本开支假设"),
        _row("Quick LBO", "低/条件适用", "待场景", "普通 A 股少数股权研究不默认使用；仅在私有化、并购或控股交易场景启用"),
        _row("并购增厚/摊薄", "条件适用", "可启用" if merger_ready else "待事件", "仅在存在明确交易对价、融资结构和标的财务时计算"),
        _row("首次覆盖报告", "中", "可启用" if factor_ready and valuation_ready else "待数据", "可复用当前六层诊断作为事实底稿，不另造评级体系"),
        _row("财报 beat/miss 解读", "高", "已启用" if earnings_ready else "待数据", "基于营收、利润及其趋势判断" if earnings_ready else "缺少可比财报趋势或一致预期"),
        _row("催化剂日历", "高", "已启用" if catalyst_checked else "待数据", "公告已核验；只有明确日期和事件才进入日历" if catalyst_checked else "公告模块未完成"),
        _row("投资逻辑追踪", "高", alpha_check.get("status", "待数据"), thesis.get("reason", "缺少 Alpha 趋势、信号和位置证据")),
        _row("晨报", "低", "按需", "属于组合/市场工作流，不是单票默认报告"),
        _row("量化筛选", "中", alpha_check.get("status", "待数据"), quant.get("reason", "缺少均线、动量和量价证据")),
        _row("行业综述", "中高", "可启用" if evidence.get("industry") and comps_ready else "待数据", "需要行业分类、同行与产业链数据"),
        _row("IC 投委会备忘录", "高", "可启用" if factor_ready else "待数据", "将现有证据重排成 Bull/Base/Bear，不产生第二套分数"),
        _row("Porter 五力 + BCG", "中", "可启用" if _known(evidence, ("business_chain_match", "leadership_strength")) else "待数据", "适合竞争结构复核；BCG 需市场份额和行业增速"),
        _row("DD 尽调清单", "高", "已启用", f"当前 24 个子因子覆盖 {covered}/{len(subfactors)}；缺口继续标记需人工确认"),
        _row("单位经济学", "行业限定", "待数据", "仅适合有客户/门店/用户/产能单位指标的业务"),
        _row("价值创造计划", "低/条件适用", "待场景", "更适合控股型 PE；公开市场少数股东无法直接执行"),
        _row("组合再平衡", "高但需持仓", "待持仓", "必须使用用户真实持仓、成本和风险约束，禁止虚构示例仓位"),
    ]
