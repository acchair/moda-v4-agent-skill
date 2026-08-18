from __future__ import annotations

import re


INDUSTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "软件服务": ("软件开发", "计算机应用"),
    "行业应用软件": ("软件开发", "计算机应用"),
    "计算机软件": ("软件开发",),
    "互联网服务": ("互联网传媒", "软件开发"),
    "电子元件": ("元件",),
    "新能源": ("电池", "光伏设备", "风电设备", "其他电源设备"),
    "发电设备": ("电源设备",),
    "汽车整车": ("乘用车", "商用车"),
    "生物医药": ("生物制品",),
    "化工": ("基础化工",),
    "有色金属": ("工业金属", "小金属", "贵金属"),
}


def normalize_industry(value: str) -> str:
    return re.sub(r"[\s行业ⅠⅡⅢIVV]+", "", str(value or "")).replace("其他", "")


def aliases_for(value: str) -> tuple[str, ...]:
    normalized = normalize_industry(value)
    candidates = [normalized, *(normalize_industry(item) for item in INDUSTRY_ALIASES.get(normalized, ()))]
    return tuple(dict.fromkeys(item for item in candidates if item))
