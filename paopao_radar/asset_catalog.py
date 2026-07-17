from __future__ import annotations

from typing import Any


ASSET_CATALOG_VERSION = "2026.07.1"

SECTORS: tuple[dict[str, str], ...] = (
    {"id": "layer1", "label": "L1", "description": "一层公链与原生结算网络"},
    {"id": "layer2", "label": "L2", "description": "二层扩容与 Rollup 生态"},
    {"id": "defi", "label": "DeFi", "description": "交易、借贷、衍生品与流动性协议"},
    {"id": "meme", "label": "Meme", "description": "社区文化与注意力资产"},
    {"id": "ai", "label": "AI", "description": "AI 网络、数据、算力与智能体"},
    {"id": "rwa", "label": "RWA", "description": "现实世界资产与链上证券化"},
    {"id": "depin", "label": "DePIN", "description": "去中心化物理基础设施网络"},
    {"id": "gaming", "label": "GameFi", "description": "游戏、元宇宙与互动娱乐"},
    {"id": "btc_ecosystem", "label": "BTC 生态", "description": "Bitcoin 扩展、铭文与原生生态"},
    {"id": "payments", "label": "支付", "description": "支付、汇款与价值传输网络"},
    {"id": "privacy", "label": "隐私", "description": "隐私计算与隐私转账协议"},
    {"id": "oracle", "label": "预言机", "description": "链上数据与跨系统消息网络"},
    {"id": "bridge", "label": "跨链", "description": "跨链通信、桥与全链协议"},
    {"id": "staking", "label": "质押", "description": "流动性质押、再质押与验证服务"},
    {"id": "exchange", "label": "交易平台币", "description": "交易平台与交易生态权益资产"},
    {"id": "stablecoin", "label": "稳定币", "description": "法币、加密资产或算法锚定资产"},
    {"id": "other", "label": "其他", "description": "尚未纳入当前版本主分类的资产"},
)

SECTOR_BY_ID = {item["id"]: item for item in SECTORS}


# The first sector is the aggregation sector. Additional sectors are tags only,
# which prevents multi-sector assets from being double-counted in fund totals.
ASSET_SECTORS: dict[str, tuple[str, ...]] = {
    "BTC": ("layer1", "btc_ecosystem"),
    "ETH": ("layer1", "staking"),
    "SOL": ("layer1",), "ADA": ("layer1",), "AVAX": ("layer1",),
    "DOT": ("layer1",), "ATOM": ("layer1",), "NEAR": ("layer1", "ai"),
    "TON": ("layer1", "payments"), "SUI": ("layer1",), "APT": ("layer1",),
    "SEI": ("layer1",), "INJ": ("layer1", "defi"), "TIA": ("layer1",),
    "ALGO": ("layer1",), "ICP": ("layer1", "ai"), "HBAR": ("layer1",),
    "KAS": ("layer1",), "TRX": ("layer1", "payments"), "ETC": ("layer1",),
    "BNB": ("layer1", "exchange"),
    "ARB": ("layer2",), "OP": ("layer2",), "STRK": ("layer2",),
    "MNT": ("layer2",), "ZK": ("layer2",), "METIS": ("layer2",),
    "POL": ("layer2",), "MATIC": ("layer2",), "LRC": ("layer2", "defi"),
    "UNI": ("defi",), "AAVE": ("defi",), "CRV": ("defi",),
    "MKR": ("defi", "rwa"), "SKY": ("defi", "rwa"), "COMP": ("defi",),
    "SUSHI": ("defi",), "DYDX": ("defi",), "GMX": ("defi",),
    "SNX": ("defi",), "ENA": ("defi", "stablecoin"), "1INCH": ("defi",),
    "JUP": ("defi",), "RAY": ("defi",), "CAKE": ("defi",),
    "PENDLE": ("defi",), "YFI": ("defi",), "BAL": ("defi",),
    "DOGE": ("meme", "payments"), "SHIB": ("meme",), "PEPE": ("meme",),
    "BONK": ("meme",), "FLOKI": ("meme",), "WIF": ("meme",),
    "BRETT": ("meme",), "POPCAT": ("meme",), "TRUMP": ("meme",),
    "FET": ("ai",), "ASI": ("ai",), "TAO": ("ai",),
    "WLD": ("ai",), "ARKM": ("ai",), "GRT": ("ai",),
    "AGIX": ("ai",), "OCEAN": ("ai",),
    "ONDO": ("rwa",), "OM": ("rwa",), "CFG": ("rwa",), "POLYX": ("rwa",),
    "FIL": ("depin",), "AR": ("depin",), "RENDER": ("depin", "ai"),
    "RNDR": ("depin", "ai"), "HNT": ("depin",), "IOTX": ("depin",),
    "AKT": ("depin", "ai"), "THETA": ("depin",),
    "IMX": ("gaming", "layer2"), "AXS": ("gaming",), "SAND": ("gaming",),
    "MANA": ("gaming",), "GALA": ("gaming",), "ENJ": ("gaming",),
    "ILV": ("gaming",), "BEAM": ("gaming",), "PIXEL": ("gaming",),
    "STX": ("btc_ecosystem", "layer2"), "ORDI": ("btc_ecosystem",),
    "SATS": ("btc_ecosystem",), "CKB": ("btc_ecosystem",),
    "RUNE": ("btc_ecosystem", "bridge"),
    "XRP": ("payments",), "XLM": ("payments",), "LTC": ("payments",),
    "BCH": ("payments", "btc_ecosystem"), "DASH": ("payments", "privacy"),
    "XMR": ("privacy",), "ZEC": ("privacy",),
    "LINK": ("oracle", "defi"), "PYTH": ("oracle",), "API3": ("oracle",),
    "BAND": ("oracle",),
    "W": ("bridge",), "AXL": ("bridge",), "ZRO": ("bridge",), "WAN": ("bridge",),
    "LDO": ("staking", "defi"), "RPL": ("staking",), "SSV": ("staking",),
    "EIGEN": ("staking",), "ETHFI": ("staking",),
    "OKB": ("exchange",), "GT": ("exchange",), "LEO": ("exchange",),
    "CRO": ("exchange",),
    "USDT": ("stablecoin",), "USDC": ("stablecoin",), "DAI": ("stablecoin",),
    "FDUSD": ("stablecoin",), "TUSD": ("stablecoin",), "USDE": ("stablecoin",),
}


def base_asset(symbol: Any) -> str:
    value = str(symbol or "").strip().upper()
    return value[:-4] if value.endswith("USDT") else value


def asset_sector_ids(symbol: Any) -> tuple[str, ...]:
    return ASSET_SECTORS.get(base_asset(symbol), ("other",))


def asset_sector_view(symbol: Any) -> dict[str, Any]:
    ids = asset_sector_ids(symbol)
    primary = ids[0]
    return {
        "catalog_version": ASSET_CATALOG_VERSION,
        "primary_sector_id": primary,
        "primary_sector_label": SECTOR_BY_ID[primary]["label"],
        "sector_ids": list(ids),
        "sector_labels": [SECTOR_BY_ID[item]["label"] for item in ids],
    }


def public_sector_catalog() -> list[dict[str, str]]:
    return [dict(item) for item in SECTORS]


__all__ = [
    "ASSET_CATALOG_VERSION",
    "ASSET_SECTORS",
    "SECTORS",
    "asset_sector_ids",
    "asset_sector_view",
    "base_asset",
    "public_sector_catalog",
]
