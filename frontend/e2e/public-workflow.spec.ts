import { expect, Page, test } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { mercuRadarFixture } from "./fixtures/mercu-radar";

const signal = {
  id: 7,
  public_ref: "sig_e2e_btc",
  time: "2026-07-16T12:00:00+00:00",
  symbol: "BTCUSDT",
  coin: "BTC",
  module: "launch",
  status: "sent",
  signal_type: "启动雷达",
  score: 88,
  stage: "active",
  excerpt: "价格与 OI 同步增强，进入启动观察。",
  display: { title: "BTC 启动信号", module_label: "启动雷达", status_label: "已发送", summary: "价格与 OI 同步增强，进入启动观察。", card_tone: "good" }
};

const intelligence = {
  self_rank: { available: true, percentile: 96, rank: 2, sample_size: 40, method: "同币历史" },
  market_strength_rank: { available: true, percentile: 92, rank: 3, sample_size: 80, method: "同模块横截面" },
  market_absolute_rank: { available: false, sample_size: 1, reason: "样本积累中" },
  lifecycle: { state: "enhancing", label: "增强", basis: "规则分数较上次提高 8.0" },
  resonance: {
    active_count: 3,
    method: "跨模块同时出现，不推断多空方向",
    windows: ["15m", "30m", "1h", "4h", "1d"].map((key, index) => ({ key, active: index < 3, module_count: index < 3 ? 2 : 1, signal_count: 2 }))
  }
};

const market = {
  symbol: "BTCUSDT",
  coin: "BTC",
  status: "fresh",
  updated_at: "2026-07-16T12:00:00+00:00",
  age_sec: 8,
  metrics: {
    price: { value: 65000, unit: "usd", source: "binance_futures", status: "fresh", age_sec: 8 },
    price_24h_pct: { value: 2.4, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    quote_volume: { value: 1_200_000_000, unit: "usd", source: "binance_futures", status: "fresh", age_sec: 8 },
    oi_value: { value: 820_000_000, unit: "usd", source: "binance_futures", status: "fresh", age_sec: 8 },
    price_15m_pct: { value: 0.6, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    price_1h_pct: { value: 1.1, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    oi_15m_pct: { value: 1.8, unit: "percent", source: "binance_futures", status: "fresh", age_sec: 8 },
    funding_pct: { value: -0.02, unit: "percent_per_cycle", source: "binance_futures", status: "fresh", age_sec: 8 }
  },
  funding_exchanges: [{ exchange: "Binance", funding_pct: -0.02, interval_hours: 8 }],
  tiers: { liquidity: "高流动性" }
};

const newSignal = {
  ...signal,
  id: 8,
  public_ref: "sig_e2e_eth",
  symbol: "ETHUSDT",
  coin: "ETH",
  excerpt: "新增 ETH 资金异动。",
  display: { ...signal.display, title: "ETH 资金异动", summary: "新增 ETH 资金异动。" }
};

const marketOverview = {
  schema_version: "2026-07-17",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  data_status: "ready",
  warnings: [],
  coverage: { assets: 80, price: 80, oi: 24, spot_flow: 12, futures_flow: 12, funding: 80 },
  overview: {
    bias: "inflow",
    advancing: 48,
    declining: 32,
    flat: 0,
    breadth_pct: 20,
    total_quote_volume: 8_000_000_000,
    spot_net_flow_usd: 12_000_000,
    futures_net_flow_usd: 18_000_000,
    oi_net_change_usd: 800_000,
    comparison: {
      previous: {
        advancing: 42,
        declining: 38,
        breadth_pct: 5,
        spot_net_flow_usd: -800_000,
        futures_net_flow_usd: 23_000_000,
        oi_net_change_usd: 1_600_000
      },
      delta: {
        breadth_pct: 15,
        spot_net_flow_usd: 12_800_000,
        futures_net_flow_usd: -5_000_000,
        oi_net_change_usd: -800_000
      }
    }
  }
};

const radarFixtureCoins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "SUI", "LINK", "AVAX", "ARB", "OP", "AAVE", "UNI", "LTC", "HYPE", "BNB"];

function radarFixtureItems(direction: "positive" | "negative", offset: number, unit: "percent" | "usd", magnitudeBase = 0) {
  const sign = direction === "positive" ? 1 : -1;
  return Array.from({ length: 8 }, (_, index) => {
    const coin = radarFixtureCoins[(index + offset) % radarFixtureCoins.length];
    const absoluteValue = unit === "usd" ? 14_000_000 - index * 1_150_000 : 5.8 - index * 0.47;
    return {
      symbol: `${coin}USDT`,
      coin,
      value: sign * absoluteValue,
      unit,
      magnitude_usd: magnitudeBase ? Math.max(250_000, magnitudeBase - index * magnitudeBase * 0.11) : undefined,
      strength_percentile: 100 - index - offset % 3,
    };
  });
}

const radarBoards = {
  schema_version: "2026-07-17",
  generated_at: "2026-07-17T12:00:00Z",
  window_sec: 3600,
  data_status: "ready",
  warnings: [],
  coverage: marketOverview.coverage,
  methodology: { flow: "Binance K 线主动买卖成交差（CVD）估算" },
  boards: [
    {
      key: "price", title: "价格动量", available: true, coverage: 80,
      positive: { title: "涨幅榜", items: radarFixtureItems("positive", 0, "percent") },
      negative: { title: "跌幅榜", items: radarFixtureItems("negative", 8, "percent") }
    },
    {
      key: "oi", title: "持仓变化", available: true, coverage: 24,
      amount_metric: "oi_change_usd", amount_unit: "usd",
      positive: { title: "OI 增长", items: radarFixtureItems("positive", 1, "percent", 20_000_000) },
      negative: { title: "OI 下降", items: radarFixtureItems("negative", 9, "percent", 5_000_000) }
    },
    {
      key: "futures_flow", title: "合约主动资金", available: true, coverage: 12,
      positive: { title: "合约流入", items: radarFixtureItems("positive", 2, "usd") },
      negative: { title: "合约流出", items: radarFixtureItems("negative", 10, "usd") }
    },
    {
      key: "spot_flow", title: "现货主动资金", available: true, coverage: 12,
      positive: { title: "现货流入", items: radarFixtureItems("positive", 3, "usd") },
      negative: { title: "现货流出", items: radarFixtureItems("negative", 11, "usd") }
    },
    {
      key: "realtime_surge", title: "Surge 加速", available: true, coverage: 12,
      positive: { title: "多头加速", items: [{ symbol: "BTCUSDT", coin: "BTC", value: 82, unit: "score", strength_percentile: 98 }] },
      negative: { title: "空头加速", items: [{ symbol: "ETHUSDT", coin: "ETH", value: -76, unit: "score", strength_percentile: 94 }] }
    }
  ]
};

const realtimeItems = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "SUI", "LINK", "AVAX", "ARB", "OP", "AAVE", "UNI", "LTC"].map((coin, index) => {
  const direction = index % 2 === 0 ? "long" : "short";
  const signed = direction === "long" ? 1 : -1;
  return {
    symbol: `${coin}USDT`, coin, observed_at: "2026-07-18T08:30:00Z", data_status: "ready",
    windows: {
      "5m": { available: true, coverage_ratio: 1, gross_trade_usd: 90_000_000 - index * 2_000_000, cvd_usd: signed * (12_000_000 - index * 300_000), cvd_ratio_pct: signed * (13 - index * 0.3), price_change_pct: signed * (1.2 - index * 0.03), long_liquidation_usd: direction === "short" ? 2_000_000 + index * 10_000 : 150_000, short_liquidation_usd: direction === "long" ? 2_500_000 + index * 10_000 : 120_000 },
      "15m": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 8, price_change_pct: signed * 1.4 },
      "30m": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 7, price_change_pct: signed * 1.1 },
      "1h": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 6, price_change_pct: signed * 1.8 },
      "4h": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 5, price_change_pct: signed * 2.1 },
      "1d": { available: true, coverage_ratio: 1, cvd_ratio_pct: signed * 4, price_change_pct: signed * 3.2 }
    },
    surge: { available: true, triggered: index < 5, direction, score: 92 - index * 3, flow_acceleration_pp: signed * (18 - index), volume_acceleration_pct: 42 - index },
    ambush: { available: true, triggered: index >= 5 && index < 9, direction, score: 78 - index, price_compression_pct: 0.6 + index * 0.04 },
    anomaly_24h: { count: 24 - index, long_count: direction === "long" ? 16 - Math.floor(index / 2) : 6, short_count: direction === "short" ? 15 - Math.floor(index / 2) : 5, latest_at: "2026-07-18T08:30:00Z" },
    resonance: { available: true, direction, active_count: 4, window_count: 5, windows: ["15m", "30m", "1h", "4h", "1d"].map((key, windowIndex) => ({ key, active: windowIndex < 4, direction, coverage_ratio: 1 })) },
    lifecycle: { state: index < 2 ? "enhancing" : "continuing", label: index < 2 ? "增强" : "持续", basis: "封闭窗口规则状态", age_sec: (index + 1) * 300, rule: index < 5 ? "surge" : "ambush", direction }
  };
});

const anomalyEvents = realtimeItems.flatMap((item, index) => {
  const positive = item.surge.direction === "long";
  const rankings = {
    self: { available: true, rank: (index % 9) + 1, sample_size: 288, percentile: 99 - index, method: "近 24h 同币历史窗口" },
    market_strength: { available: true, rank: index + 1, sample_size: realtimeItems.length, percentile: 98 - index, method: "全场历史极端分位" },
    market_absolute: { available: true, rank: realtimeItems.length - index, sample_size: realtimeItems.length, percentile: 80 + index, method: "全场绝对金额" }
  };
  return [
    { id: `${item.symbol}:price`, symbol: item.symbol, coin: item.coin, observed_at: item.observed_at, window: "5m", event_type: positive ? "price_up" : "price_down", label: positive ? "价格暴涨" : "价格暴跌", metric: "price", direction: item.surge.direction, value: item.windows["5m"].price_change_pct, change_pct: item.windows["5m"].price_change_pct, rankings },
    { id: `${item.symbol}:flow`, symbol: item.symbol, coin: item.coin, observed_at: item.observed_at, window: "15m", event_type: positive ? "perp_inflow" : "perp_outflow", label: positive ? "合约净流入" : "合约净流出", metric: "perp_flow", direction: item.surge.direction, value: item.windows["5m"].cvd_usd, value_usd: item.windows["5m"].cvd_usd, change_pct: item.windows["5m"].cvd_ratio_pct, rankings }
  ];
});

const realtimeIntelligence = {
  schema_version: "2026-07-18.1", generated_at: "2026-07-18T08:30:00Z", observed_at: "2026-07-18T08:30:00Z", data_status: "ready",
  coverage: { symbols: realtimeItems.length, surge: 5, ambush: 4, total: realtimeItems.length, anomaly_events: anomalyEvents.length }, items: realtimeItems, anomaly_events: anomalyEvents,
  boards: []
};

const crossExchangeOi = {
  schema_version: "workstation.funds.open-interest.v1", symbol: "BTCUSDT", data_status: "ready",
  coverage: { exchanges: 3, target: 3 }, mark_price: 65000, total_oi_usd: 1_450_000_000, top_exchange_share_pct: 48.2759,
  exchanges: [
    { exchange: "binance", oi_usd: 700_000_000, share_pct: 48.2759, status: "ready" },
    { exchange: "bybit", oi_usd: 450_000_000, share_pct: 31.0345, status: "ready" },
    { exchange: "okx", oi_usd: 300_000_000, share_pct: 20.6897, status: "ready" }
  ]
};

const fundsSectorFixtures = [
  ["layer1", "L1", 1_651_900], ["privacy", "隐私", 425_700], ["gaming", "GameFi", 327_900],
  ["modular", "模块化", 86_200], ["data", "数据", 74_100], ["stocks", "股票", 68_700],
  ["cross_chain", "跨链", 61_900], ["layer2", "L2", 53_800], ["btc", "BTC生态", 35_300],
  ["metals", "贵金属", -1_366_000], ["payments", "支付", -678_900], ["exchange", "平台币", -447_800],
  ["rwa", "RWA", -417_500], ["meme", "Meme", -382_000], ["oracle", "预言机", -312_600],
  ["staking", "质押", -265_400], ["depin", "DePIN", -244_100], ["defi_outflow", "DeFi", -230_000], ["nft", "NFT", -218_700],
  ["identity", "身份", -191_500], ["desci", "DeSci", -176_800], ["ai", "AI", -154_300], ["social", "社交", -132_900]
] as const;

const fundsSectors = {
  schema_version: "2026-07-18",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-19T22:55:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 686, flow: 686, gross_flow: 686, oi: 412, market_cap: 634 },
  warnings: [],
  summary: { net_flow_usd: -1_074_700, inflow_usd: 2_701_900, outflow_usd: 3_776_600, asset_count: 686, covered_assets: 686, leading_inflow_sector: "L1", leading_outflow_sector: "贵金属" },
  catalog: fundsSectorFixtures.map(([id, label]) => ({ id, label, description: `${label} 板块` })),
  sectors: fundsSectorFixtures.map(([sector_id, label, net_flow_usd], index) => ({ sector_id, label, net_flow_usd, magnitude_usd: Math.abs(net_flow_usd), inflow_usd: net_flow_usd > 0 ? Math.abs(net_flow_usd) + 150_000 : 80_000 + index * 5_000, outflow_usd: net_flow_usd < 0 ? Math.abs(net_flow_usd) + 80_000 : 150_000, asset_count: 8 + index, covered_assets: 8 + index, coverage_ratio: 1, data_status: "ready", leaders: [{ symbol: "BTCUSDT", net_flow_usd }] }))
};

const fundsCoins = ["ACE", "XRP", "SUI", "TRX", "BNB", "DOGE", "TAO", "ALICE", "APT", "STRK", "PUMP", "XVG", "GIGGLE", "ESPORTS", "BTC", "ETH", "SOL", "ADA", "LINK", "INJ"];
const fundsNetFixtures = [163_600, 122_900, 119_600, 88_700, 66_300, 59_500, 59_300, 47_100, 42_100, 27_200, 26_800, 25_900, 24_800, 23_500, 22_100, 21_000, 19_600, 18_400, 17_200, 16_000];
const mercuFundRows = [
  { volume_usd: 1_278_000, volume_change_pct: -49.20, inflow_usd: 720_800, outflow_usd: 557_200, market_cap: 9_595_300, price: 0.0954, price_change_pct: 48.14 },
  { volume_usd: 1_250_200, volume_change_pct: -88.81, inflow_usd: 686_600, outflow_usd: 563_700, market_cap: 68_575_000_000, price: 1.10, price_change_pct: 0.51 },
  { volume_usd: 223_500, volume_change_pct: -81.79, inflow_usd: 171_600, outflow_usd: 52_000, market_cap: 3_035_000_000, price: 0.7491, price_change_pct: 0.70 },
  { volume_usd: 189_200, volume_change_pct: -84.67, inflow_usd: 138_900, outflow_usd: 50_300, market_cap: 31_002_000_000, price: 0.3272, price_change_pct: 0.40 },
  { volume_usd: 438_200, volume_change_pct: -83.86, inflow_usd: 252_300, outflow_usd: 185_900, market_cap: 75_974_000_000, price: 671, price_change_pct: 0.17 },
  { volume_usd: 229_200, volume_change_pct: -93.82, inflow_usd: 144_400, outflow_usd: 84_800, market_cap: 11_223_000_000, price: 0.0723, price_change_pct: -0.11 },
  { volume_usd: 162_900, volume_change_pct: -77.71, inflow_usd: 111_100, outflow_usd: 51_800, market_cap: 1_901_000_000, price: 198.20, price_change_pct: 1.28 },
  { volume_usd: 147_800, volume_change_pct: -3.58, inflow_usd: 97_400, outflow_usd: 50_300, market_cap: 12_107_700, price: 0.1213, price_change_pct: 2.80 },
  { volume_usd: 72_000, volume_change_pct: -69.03, inflow_usd: 57_100, outflow_usd: 15_000, market_cap: 504_000_000, price: 0.5960, price_change_pct: -2.13 },
] as const;
const fundsAssetFixtures = Array.from({ length: 686 }, (_, index) => {
  const coin = fundsCoins[index] || `T${String(index + 1).padStart(3, "0")}`;
  const net = fundsNetFixtures[index] ?? Math.max(100, (22 - Math.min(index, 21)) * 10_000 + (index % 3) * 700);
  const target = mercuFundRows[index];
  const inflow = target?.inflow_usd ?? net * (2.1 + (index % 20) * 0.03);
  const outflow = target?.outflow_usd ?? inflow - net;
  return { symbol: `${coin}USDT`, coin, price: target?.price ?? (index === 0 ? 0.4356 : 0.08 + index * 0.127), price_change_pct: target?.price_change_pct ?? (index % 3 === 0 ? 8.17 - index * 0.01 : -0.99 - index * 0.02), net_flow_usd: net, net_flow_change_pct: null, inflow_usd: inflow, outflow_usd: outflow, volume_usd: target?.volume_usd ?? Math.max(10_000, 745_000 - index * 800), volume_change_pct: target?.volume_change_pct ?? (index % 2 ? -14.03 - index * 0.01 : 38.19 - index * 0.01), oi_usd: Math.max(100_000, 820_000_000 - index * 1_000_000), oi_change_pct: 1.8 - index * 0.01, funding_pct: -0.02 + index * 0.0001, market_cap: target?.market_cap ?? Math.max(500_000, 102_000_000 - index * 100_000), updated_at: "2026-07-18T17:44:00Z", data_status: "ready", sector: { primary_sector_id: fundsSectorFixtures[index % fundsSectorFixtures.length][0], primary_sector_label: fundsSectorFixtures[index % fundsSectorFixtures.length][1], sector_ids: [fundsSectorFixtures[index % fundsSectorFixtures.length][0]] } };
});
const fundsAssets = {
  schema_version: "2026-07-18",
  catalog_version: "2026.07.1",
  generated_at: "2026-07-19T22:55:00Z",
  window_sec: 3600,
  market_type: "spot",
  data_status: "ready",
  coverage: { assets: 686, flow: 686 },
  warnings: [],
  distribution: { oi_total_usd: 900_000_000, oi_covered_assets: 62, top_10_oi_share_pct: 74.5, top_50_oi_share_pct: 98.2 },
  pagination: { page: 1, page_size: 20, page_count: 35, total: 686 },
  items: fundsAssetFixtures.slice(0, 20)
};

const wideFundRows = [
  ["ACE", 99_300, 1_198_800, -52.35, 649_000, 549_700, 9_850_800, 0.0976, 51.08],
  ["TRX", 90_000, 200_800, -83.73, 145_400, 55_400, 31_002_000_000, 0.3272, 0.37],
  ["DOGE", 78_000, 239_300, -93.35, 158_600, 80_600, 11_223_000_000, 0.0724, -0.21],
  ["SUI", 72_800, 173_500, -85.86, 123_200, 50_400, 3_035_000_000, 0.7490, 0.52],
  ["OPG", 48_000, 98_700, 10.47, 73_300, 25_300, 20_146_900, 0.1068, 2.50],
  ["XRP", 44_800, 1_266_200, -88.67, 655_500, 610_700, 68_575_000_000, 1.10, 0.37],
  ["PUMP", 38_700, 2_289_900, -43.70, 1_164_300, 1_125_600, 769_000_000, 0.001958, 17.74],
  ["GIGGLE", 33_200, 125_300, 59.22, 79_300, 46_000, 26_360_700, 26.67, 1.99],
  ["APT", 31_800, 60_600, -73.93, 46_200, 14_400, 504_000_000, 0.5960, -2.30],
  ["XVG", 29_100, 51_800, -18.56, 40_400, 11_400, 35_957_800, 0.002177, 7.40],
  ["ESPORTS", 28_500, 44_400, -59.50, 36_400, 7_967.64, 13_377_800, 0.0218, -37.18],
  ["STRK", 27_200, 106_500, 4.89, 66_800, 39_600, 1_930_000_000, 0.0288, -0.35],
] as const;

const wideFundsAssetFixtures = Array.from({ length: 686 }, (_, index) => {
  const target = wideFundRows[index];
  if (target) {
    const [coin, net_flow_usd, volume_usd, volume_change_pct, inflow_usd, outflow_usd, market_cap, price, price_change_pct] = target;
    return {
      ...fundsAssetFixtures[index], coin, symbol: `${coin}USDT`, net_flow_usd, net_flow_change_pct: null,
      volume_usd, volume_change_pct, inflow_usd, outflow_usd, market_cap, price, price_change_pct,
    };
  }
  const source = fundsAssetFixtures[index] || fundsAssetFixtures[fundsAssetFixtures.length - 1];
  const coin = `T${String(index + 1).padStart(3, "0")}`;
  return { ...source, coin, symbol: `${coin}USDT`, net_flow_usd: Math.max(100, 20_000 - index * 10) };
});

const wideSectorRows = [
  ["privacy", "隐私", 2_059_200], ["layer1", "L1", 1_837_500], ["gaming", "GameFi", 445_800],
  ["meme", "Meme", 192_100], ["defi_inflow", "DeFi", 102_900], ["stocks", "股票", 87_300],
  ["exchange_inflow", "平台币", 83_500], ["depin", "DePIN", 81_400], ["layer2", "L2", 55_900],
  ["modular", "模块化", 43_300], ["data", "数据", 41_500], ["nft", "NFT", 30_000],
  ["cross_chain", "跨链", 25_000], ["btc", "BTC生态", 20_000], ["identity", "身份", 15_000],
  ["metals", "贵金属", -1_346_000], ["payments", "支付", -534_500], ["exchange", "平台币", -476_700],
  ["rwa", "RWA", -398_400], ["social", "社交", -67_700], ["staking", "质押", -62_900],
  ["ai", "AI", -50_000], ["desci", "DeSci", -40_000],
] as const;

const wideFundsSectors = {
  ...fundsSectors,
  generated_at: "2026-07-19T22:57:00Z",
  coverage: { ...fundsSectors.coverage, assets: 686, flow: 686, gross_flow: 686 },
  summary: { ...fundsSectors.summary, net_flow_usd: 2_190_800, inflow_usd: 5_087_900, outflow_usd: 2_897_000, asset_count: 686, covered_assets: 686, leading_inflow_sector: "隐私", leading_outflow_sector: "贵金属" },
  catalog: wideSectorRows.map(([id, label]) => ({ id, label, description: `${label} 板块` })),
  sectors: wideSectorRows.map(([sector_id, label, net_flow_usd], index) => ({ sector_id, label, net_flow_usd, magnitude_usd: Math.abs(Number(net_flow_usd)), inflow_usd: Number(net_flow_usd) > 0 ? Math.abs(Number(net_flow_usd)) + 150_000 : 80_000 + index * 5_000, outflow_usd: Number(net_flow_usd) < 0 ? Math.abs(Numb…17279 tokens truncated… };
      await page.clock.setFixedTime(new Date(fixedTimes[route]));
      await page.goto(`/${route}`);
      await expect(page.getByTestId(`${route}-workstation`)).toBeVisible();
      await expect(page.getByTestId(`${route}-workstation`)).toHaveAttribute("aria-busy", "false");
      await page.evaluate(async () => {
        await document.fonts.ready;
      });
      if (route === "radar") {
        await expect(page.getByLabel("五窗口共振 2/5").first()).toBeVisible();
        await expect(page.getByTestId("radar-hot-money").getByText(viewport.width === 1440 ? "+$23.9M" : "+$9.6M", { exact: true }).first()).toBeVisible();
        if (viewport.width === 1920) {
          await expect(page.getByTestId("radar-hot-money").getByText("美股", { exact: true }).first()).toBeVisible();
          await expect(page.getByTestId("radar-hot-money").getByText("黄金", { exact: true }).first()).toBeVisible();
        }
      }
      await page.locator("img").evaluateAll(async (images) => {
        await Promise.all(images.map((image) => (image as HTMLImageElement).decode().catch(() => undefined)));
      });
      await page.addStyleTag({ content: "nextjs-portal { display: none !important; }" });
      await expect(page).toHaveScreenshot(`${route}-${viewport.width}x${viewport.height}.png`, { animations: "disabled", maxDiffPixelRatio: 0.035, scale: "device" });
      if (process.env.MERCU_ACTUAL_DIR) {
        const actualPath = resolve(process.env.MERCU_ACTUAL_DIR, `${route}-${viewport.width}x${viewport.height}-chromium.png`);
        await mkdir(dirname(actualPath), { recursive: true });
        await page.screenshot({ animations: "disabled", path: actualPath, scale: "device" });
      }
    }
  });
}

test("exports deterministic workstation API corpus for native-browser visual audit", async ({ page }) => {
  const outputValue = process.env.MERCU_FIXTURE_EXPORT_PATH;
  test.skip(!outputValue, "Set MERCU_FIXTURE_EXPORT_PATH to export the native-browser fixture corpus.");
  if (!outputValue) return;

  const viewportValue = process.env.MERCU_FIXTURE_VIEWPORT === "1920x1080" ? "1920x1080" : "1440x900";
  const [width, height] = viewportValue.split("x").map(Number);
  const responseTasks: Promise<void>[] = [];
  const responses: Record<string, { body: string; contentType: string; status: number }> = {};
  const responseKey = (value: string) => {
    const url = new URL(value);
    const entries = Array.from(url.searchParams.entries()).sort(([leftKey, leftValue], [rightKey, rightValue]) => (
      leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue)
    ));
    const query = new URLSearchParams(entries).toString();
    return `${url.pathname}${query ? `?${query}` : ""}`;
  };

  page.on("response", (response) => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith("/public-api/")) return;
    responseTasks.push((async () => {
      try {
        const headers = await response.allHeaders();
        responses[responseKey(response.url())] = {
          body: (await response.body()).toString("utf8"),
          contentType: headers["content-type"] || "application/json; charset=utf-8",
          status: response.status(),
        };
      } catch {
        // A superseded navigation may release a response body; later identical requests still populate the corpus.
      }
    })());
  });

  await page.setViewportSize({ width, height });
  await mockPublicApi(page, { radarVisual: viewportValue });
  const fixedTimes = width === 1440
    ? { radar: "2030-07-18T22:53:04Z", info: "2030-07-18T22:54:52Z", funds: "2030-07-18T22:55:53Z" }
    : { radar: "2030-07-18T22:59:49Z", info: "2030-07-18T22:59:39Z", funds: "2030-07-18T22:58:04Z" };
  for (const route of ["radar", "info", "funds"] as const) {
    await page.clock.setFixedTime(new Date(fixedTimes[route]));
    await page.goto(`/${route}`);
    await expect(page.getByTestId(`${route}-workstation`)).toHaveAttribute("aria-busy", "false");
    await page.waitForTimeout(100);
  }
  await Promise.allSettled(responseTasks);

  const outputPath = resolve(outputValue);
  await mkdir(dirname(outputPath), { recursive: true });
  await writeFile(outputPath, `${JSON.stringify({ responses: Object.fromEntries(Object.entries(responses).sort()), viewport: viewportValue }, null, 2)}\n`, "utf8");
});

test("home dashboard refreshes its own signal data", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByText("BTCUSDT", { exact: true }).first()).toBeVisible();
  expect(state.signalRequests()).toBe(1);
  state.failSignals();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByText(/刷新失败，正在继续显示上次成功数据/)).toBeVisible();
  await expect(page.getByText("BTCUSDT", { exact: true }).first()).toBeVisible();
});

test("Paoxx AI reservation page does not call the former agent endpoint", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/agents");

  await expect(page.getByTestId("paoxx-ai-reserved")).toBeVisible();
  await expect(page.getByRole("heading", { name: "泡泡智选" })).toBeVisible();
  await expect(page.getByText("当前不提供第三方 AI 智选、荐币或自动交易功能。", { exact: false })).toBeVisible();
  expect(state.agentRequests()).toBe(0);
});

test("320px radar keeps its primary workstation controls usable", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 780 });
  await mockPublicApi(page);
  await page.goto("/radar?view=extended");

  await expect(page.getByPlaceholder("搜索币种...")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  for (const control of [
    page.getByPlaceholder("搜索币种..."),
    page.getByRole("button", { name: "1h" }),
    page.getByRole("button", { name: "暂停" }),
    page.getByRole("button", { name: "立即更新" }),
  ]) {
    const controlBox = await control.boundingBox();
    expect(controlBox?.height).toBeGreaterThanOrEqual(44);
  }
  const undersizedControls = await page.locator("a[href], button, input, select, summary").evaluateAll((elements) => elements
    .map((element) => {
      const rect = element.getBoundingClientRect();
      return { label: element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName, width: rect.width, height: rect.height };
    })
    .filter((item) => item.label !== "Open Next.js Dev Tools" && item.width > 0 && item.height > 0 && (item.width < 44 || item.height < 44)));
  expect(undersizedControls).toEqual([]);
  await expect(page.getByRole("heading", { name: "24h 异动总榜" })).toBeVisible();
});

test("public cockpit defaults to the Mercu-style light system and persists theme choice", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("header distinguishes degraded data from an offline API", async ({ page }) => {
  await mockPublicApi(page, { healthStatus: "degraded" });
  await page.goto("/radar");

  await expect(page.getByText("DEGRADED", { exact: true })).toBeVisible();
});

test("767px radar keeps stacked workstation modules usable", async ({ page }) => {
  await page.setViewportSize({ width: 767, height: 900 });
  await mockPublicApi(page);
  await page.goto("/radar");

  await expect(page.getByPlaceholder("搜索币种...")).toBeVisible();
  await expect(page.getByRole("heading", { name: "热钱观察榜单" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "全场态势" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(767);
});

test("coin context and browser-local watchlist form a reusable loop", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/coin/BTCUSDT");
  await expect(page.getByRole("heading", { name: "BTC 单币上下文" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "K 线与成交量" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "快照证据曲线" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "关联资讯" })).toBeVisible();
  await expect(page.getByRole("link", { name: /BTC 合约保证金规则更新/ })).toHaveAttribute("href", "https://example.com/btc-margin");
  await page.getByRole("button", { name: /加入自选/ }).click();
  await page.goto("/watchlist");
  await expect(page.getByText("BTCUSDT", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看上下文" })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("paoxx.public.watchlist.v1"))).toContain("BTCUSDT");
});

test("radar signal deep link opens and closes the exact context drawer", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar?signal=sig_e2e_btc");

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("BTCUSDT", { exact: true })).toBeVisible();
  await dialog.locator("button").first().click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => new URL(page.url()).searchParams.has("signal")).toBe(false);
});

test("radar symbol deep link opens and closes the Mercu coin drawer", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/radar?symbol=BTCUSDT");

  const dialog = page.getByRole("dialog", { name: "BTC 单币详情" });
  await expect(dialog).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.get("symbol")).toBe("BTCUSDT");
  const closeButton = dialog.getByRole("button", { name: "关闭" });
  await expect(closeButton).toHaveCount(1);
  await closeButton.click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => new URL(page.url()).searchParams.has("symbol")).toBe(false);
});

test("funds workstation links overview, time series and cross-exchange OI", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByRole("heading", { name: "板块资金流" })).toBeVisible();
  await expect(page.getByLabel("搜索全体代币")).toBeVisible();
  await page.getByRole("button", { name: /BTC/ }).first().click();
  await expect(page.getByRole("heading", { name: "BTCUSDT 现货" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "现货资金流" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "跨所持仓对比" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "OI & 资金费率" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "集中度 / 关键价位" })).toBeVisible();
  await expect(page.getByText("下一桶方向命中", { exact: false })).toBeVisible();
  await expect(page.getByText("62.5%", { exact: true })).toBeVisible();
  await expect(page.getByText("POC", { exact: true })).toBeVisible();
  await expect(page.getByText("价格表现", { exact: true })).toBeVisible();
  await expect(page.getByText("+1.09%", { exact: true })).toBeVisible();
  await expect(page.getByText("关联资讯", { exact: true })).toBeVisible();
  await expect(page.getByText("BTC 合约保证金规则更新", { exact: true })).toBeVisible();
  await expect(page.getByText("$1.45B", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "合约" }).click();
  await expect(page.getByRole("heading", { name: "BTCUSDT 合约（永续）" })).toBeVisible();
});

test("funds overview uses server-backed pagination and search semantics", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByText("共 686 个代币 · 每页 20 条 · 第 1/35 页")).toBeVisible();
  const secondPageResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/public-api/workstation/funds/overview" && url.searchParams.get("market_type") === "spot" && url.searchParams.get("page") === "2";
  });
  await page.getByRole("button", { name: "下一页" }).click();
  await secondPageResponse;
  await expect(page.getByText("共 686 个代币 · 每页 20 条 · 第 2/35 页")).toBeVisible();
  await expect(page.getByText("21", { exact: true }).first()).toBeVisible();

  const searchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/public-api/workstation/funds/overview" && url.searchParams.get("market_type") === "spot" && url.searchParams.get("search") === "BTC";
  });
  await page.getByLabel("搜索全体代币").fill("BTC");
  await searchResponse;
  await expect(page.getByText("共 1 个代币 · 每页 20 条 · 第 1/1 页")).toBeVisible();
});

test("funds workstation preserves explicit cross-venue coverage", async ({ page }) => {
  await mockPublicApi(page, { assetWarnings: ["资产资金数据已降级"] });
  await page.goto("/funds");
  await page.getByRole("button", { name: /BTC/ }).first().click();

  await expect(page.getByText("3/3 场所")).toBeVisible();
  await expect(page.getByText("缺失交易所不按 0 计入分母", { exact: false })).toBeVisible();
});

test("390px funds workstation stacks without page-level horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/funds");

  await expect(page.getByLabel("搜索全体代币")).toBeVisible();
  await page.getByRole("button", { name: /BTC/ }).first().click();
  await expect(page.getByRole("heading", { name: "跨所持仓对比" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("information workstation keeps four fixed authorized streams traceable", async ({ page }) => {
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "AI信息蒸馏" })).toBeVisible();
  for (const heading of ["聚合资讯", "英文流资讯", "KOL聚合资讯", "币安广场情绪"]) await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
  for (const mode of ["news", "english", "kol", "plaza"]) await expect(page.getByTestId(`info-channel-icon-${mode}`)).toBeVisible();
  for (const label of ["搜索聚合资讯", "搜索英文流资讯"]) {
    const searchBox = await page.getByLabel(label).boundingBox();
    expect(searchBox?.width).toBeCloseTo(145, 0);
    expect(searchBox?.height).toBeCloseTo(27, 0);
  }
  const englishRowHeights = await page.locator('[data-info-row="english"]').evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height));
  expect(englishRowHeights.length).toBeGreaterThan(1);
  expect(Math.min(...englishRowHeights)).toBeGreaterThanOrEqual(55);
  const kolRowHeights = await page.locator('[data-info-row="kol"]').evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height));
  expect(kolRowHeights.length).toBeGreaterThan(1);
  expect(Math.min(...kolRowHeights)).toBeGreaterThanOrEqual(40);
  expect(Math.max(...kolRowHeights)).toBeGreaterThan(Math.min(...kolRowHeights));
  const bodyWeights = await page.locator("[data-info-row] h3").evaluateAll((rows) => [...new Set(rows.map((row) => getComputedStyle(row).fontWeight))]);
  expect(bodyWeights).toEqual(["400"]);
  const plazaDigest = page.getByTestId("info-plaza-digest");
  await expect(plazaDigest).toHaveAttribute("aria-expanded", "false");
  await plazaDigest.click();
  await expect(plazaDigest).toHaveAttribute("aria-expanded", "true");
  await expect(plazaDigest).toContainText("收起");
  await expect(page.getByLabel("搜索KOL聚合资讯")).toHaveCount(0);
  await expect(page.getByText(/广场 多/).first()).toBeVisible();
  await expect(page.getByText("466 帖", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: /据伊朗学生通讯社/ }).first()).toBeVisible();
  await expect(page.getByRole("link").filter({ hasText: "据伊朗学生通讯社" }).first()).toHaveAttribute("rel", "noreferrer");
});

test("information workstation loads each source column independently", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: /据伊朗学生通讯社/ }).first()).toBeVisible();
  await expect.poll(state.infoRequests).toBe(4);
  await page.getByRole("button", { name: /4h AI 综合分析/ }).click();
  await expect.poll(state.infoRequests).toBe(8);
});

test("390px information workstation stacks its four columns", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/info");

  await expect(page.getByRole("heading", { name: "聚合资讯" }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /据伊朗学生通讯社/ }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("Paoxx AI page remains an explicit self-owned reservation", async ({ page }) => {
  const state = await mockPublicApi(page);
  await page.goto("/agents");

  await expect(page.getByRole("heading", { name: "泡泡智选" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "先把证据做对，再让模型开口。" })).toBeVisible();
  await expect(page.getByText("PAOXX NATIVE")).toBeVisible();
  await expect(page.getByText("公开版本")).toBeVisible();
  await expect(page.getByText("未开放")).toBeVisible();
  await expect(page.getByText("全局 Agent")).toHaveCount(0);
  expect(state.agentRequests()).toBe(0);
});

test("390px Paoxx AI reservation stays usable without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/agents");

  await expect(page.getByRole("heading", { name: "先把证据做对，再让模型开口。" })).toBeVisible();
  await expect(page.getByRole("link", { name: "先看实时雷达" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("radar polling can be paused and manually refreshed", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockPublicApi(page);
  await page.goto("/radar?view=extended");

  await expect(page.getByText("30s 增量", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByText("已暂停", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "立即更新" }).click();
  await expect(page.getByRole("heading", { name: "异动总榜" })).toBeVisible();
  await page.getByRole("button", { name: "继续" }).click();
  await expect(page.getByText("30s 增量", { exact: false })).toBeVisible();
});

test("reserved AI surface never exposes copied directional conclusions", async ({ page }) => {
  const state = await mockPublicApi(page, { agents: agentsOverview });
  await page.goto("/agents");

  await expect(page.getByText("当前页面不请求 AI 决策接口", { exact: false })).toBeVisible();
  await expect(page.getByText("同步增强", { exact: true })).toHaveCount(0);
  expect(state.agentRequests()).toBe(0);
});

