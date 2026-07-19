type Viewport = "1440x900" | "1920x1080";
type Unit = "percent" | "usd";
type RankedValue = readonly [coin: string, value: number, strength?: number];

function ranked(entries: readonly RankedValue[], unit: Unit) {
  return entries.map(([coin, value, strength], index) => ({
    symbol: `${coin}USDT`,
    coin,
    value,
    unit,
    strength_percentile: strength ?? Math.max(84, 100 - index * 2),
  }));
}

function board(
  key: string,
  unit: Unit,
  amountPositive: readonly RankedValue[],
  amountNegative: readonly RankedValue[],
  strengthPositive: readonly RankedValue[],
  strengthNegative: readonly RankedValue[],
) {
  return {
    key,
    title: key,
    available: true,
    coverage: 100,
    amount_positive: { title: "量级榜", items: ranked(amountPositive, unit) },
    amount_negative: { title: "量级榜", items: ranked(amountNegative, unit) },
    strength_positive: { title: "强度榜", items: ranked(strengthPositive, unit) },
    strength_negative: { title: "强度榜", items: ranked(strengthNegative, unit) },
  };
}

const compactBoards = [
  board(
    "price", "percent",
    [["CASHCAT", 5.99], ["TLM", 4.06], ["ESPORTS", 3.45], ["CYS", 1.33], ["VANRY", 1.32], ["ZBT", 1.01], ["MAGMA", 0.87]],
    [["PHA", -3.58], ["DODO", -1.56], ["LUMIA", -0.98], ["AKE", -0.86], ["BTW", -0.79], ["ZEREBRO", -0.65], ["ALLO", -0.60]],
    [["CASHCAT", 5.99, 100], ["TLM", 4.06, 98], ["CYS", 1.33, 98], ["ESPORTS", 3.45, 97], ["PUMP", 0.76, 96], ["QTUM", 0.46, 95], ["MANTRA", 0.55, 95], ["GAS", 0.56, 95]],
    [["PHA", -3.58, 100], ["LUMIA", -0.98, 91], ["DODO", -1.56, 98], ["AKE", -0.86, 89], ["ALLO", -0.60, 90], ["BTW", -0.79, 88], ["ZEREBRO", -0.65, 84], ["DASH", -0.65, 84]],
  ),
  board(
    "oi", "usd",
    [["ETH", 20_900_000, 70], ["BTC", 19_200_000, 97], ["ZEC", 9_100_000, 100], ["HYPE", 6_000_000, 99], ["SOL", 2_400_000, 98], ["DOGE", 1_900_000, 94], ["ADA", 1_700_000, 95]],
    [["AKE", -2_100_000], ["DYDX", -1_400_000], ["WLD", -1_300_000, 70], ["XRP", -933_000, 96], ["ALLO", -620_000], ["US", -498_000], ["KAITO", -347_000]],
    [["SOL", 381_000, 99], ["QTUM", 47_000, 99], ["BTC", 156_000, 98], ["CASHCAT", 888_000, 97], ["TLM", 565_000, 97], ["LUNC", 182_000, 96], ["ZEC", 9_100_000, 96], ["LSETH", 23_000, 96]],
    [["PHA", -277_000, 100], ["DYDX", -1_400_000, 99], ["AKE", -2_100_000, 99], ["JUP", -162_000, 97], ["US", -498_000, 92], ["BONK", -39_000, 91], ["XEC", -55_000, 91], ["SENT", -71_000, 90]],
  ),
  board(
    "futures_flow", "usd",
    [["ETH", 9_100_000, 70], ["HYPE", 4_600_000, 99], ["SOL", 3_000_000, 98], ["ZEC", 1_700_000, 100], ["XRP", 800_000, 96], ["ADA", 300_000, 95], ["INTC", 300_000]],
    [["DOGE", -1_400_000], ["BTC", -1_100_000], ["AMD", -600_000], ["FARTCOIN", -300_000], ["SKYNYK", -300_000], ["NEAR", -200_000], ["WLD", -200_000]],
    [["QTUM", 0, 100], ["TLM", 100_000, 99], ["BTC", 100_000, 99], ["SENT", 0, 99], ["SOL", 100_000, 99], ["GAS", 0, 98], ["ZEC", 100_000, 98], ["ESPORTS", 0, 97]],
    [["SKYNYK", -300_000, 100], ["PHA", -100_000, 100], ["NEAR", 0, 99], ["AKE", -100_000, 97], ["DEXE", 0, 96], ["JUP", 0, 95], ["ALLO", 0, 94], ["LUMIA", 0, 94]],
  ),
  board(
    "spot_flow", "usd",
    [["BTC", 5_800_000], ["SOL", 800_000], ["HYPE", 300_000], ["XRP", 200_000], ["ZEC", 100_000], ["MANTRA", 100_000], ["XMR", 100_000]],
    [["TRX", -200_000], ["ETH", -100_000], ["DOGE", -100_000], ["LSETH", -100_000], ["XLM", -100_000], ["VVV", -100_000], ["BONK", -50_000]],
    [["POWR", 100_000, 100], ["MANTRA", 100_000, 100], ["BANK", 100_000, 100], ["QTUM", 0, 100], ["XRP", 0, 100], ["ESPORTS", 0, 99], ["SOL", 0, 98], ["ETH", 0, 98]],
    [["VVV", -100_000, 100], ["HIVE", 0, 99], ["BANK", 0, 100], ["PHA", 0, 98], ["XRP", 0, 98], ["ETH", 0, 98], ["SENT", 0, 98], ["BONK", 0, 97]],
  ),
];

const wideBoards = [
  board(
    "price", "percent",
    [["TLM", 6.52], ["CASHCAT", 5.12], ["ESPORTS", 3.82], ["VANRY", 1.61], ["EVAA", 1.35], ["CYS", 1.26], ["BR", 1.25]],
    [["PHA", -2.22], ["LUMIA", -0.69], ["IDOL", -0.69], ["BEAT", -0.50], ["BANK", -0.40], ["KIOXIA", -0.39], ["AUDIO", -0.37]],
    [["CASHCAT", 5.12, 100], ["TLM", 6.52, 99], ["CYS", 1.26, 98], ["QTUM", 0.68, 98], ["CKB", 0.76, 97], ["ESPORTS", 3.02, 97], ["BR", 1.25, 96], ["MANTRA", 0.60, 96]],
    [["PHA", -2.22, 100], ["XEC", -0.33, 86], ["LUMIA", -0.69, 86], ["IDOL", -0.69, 85], ["Q", -0.37, 84], ["LUNC", -0.29, 84], ["BANK", -0.40, 82], ["KIOXIA", -0.39, 82]],
  ),
  board(
    "oi", "usd",
    [["BTC", 43_100_000, 93], ["ETH", 20_900_000, 95], ["ZEC", 12_300_000, 100], ["HYPE", 4_000_000, 99], ["ADA", 2_400_000, 98], ["SOL", 2_400_000, 97], ["AMD", 1_700_000]],
    [["DYDX", -1_400_000], ["ALLO", -487_000, 60], ["DEXE", -461_000], ["KAITO", -358_000], ["PUMP", -345_000], ["GRAM", -270_000], ["SPCX", -255_000]],
    [["CASHCAT", 636_000, 100], ["BANANAS31", 351_000, 98], ["QTUM", 62_000, 100], ["ZEC", 12_300_000, 98], ["TLM", 851_000, 98], ["LUNC", 178_000, 96], ["ESPORTS", 965_000, 96], ["AKE", 486_000, 80]],
    [["PHA", -228_000, 99], ["DYDX", -1_400_000, 99], ["XEC", -13_000, 96], ["BEAMX", -35_000, 92], ["BANK", -93_000, 97], ["KAITO", -358_000, 89], ["BONK", -32_000, 88], ["Q", -43_000, 85]],
  ),
  board(
    "futures_flow", "usd",
    [["ETH", 9_000_000, 95], ["ZEC", 4_500_000, 100], ["HYPE", 4_500_000, 99], ["SOL", 4_400_000, 97], ["XRP", 2_000_000, 94], ["ADA", 600_000, 98], ["BNB", 400_000]],
    [["AMD", -600_000], ["SKYNYK", -500_000], ["NEAR", -300_000], ["FARTCOIN", -300_000], ["SPCX", -100_000], ["TRX", -100_000], ["ALLO", -100_000]],
    [["BANANAS31", 100_000, 98], ["QTUM", 0, 100], ["STRK", 100_000, 99], ["TRUTH", 100_000, 99], ["BANK", 0, 97], ["GAS", 0, 98], ["POWR", 0, 100], ["DASH", 0, 97]],
    [["SKYNYK", -500_000, 100], ["PHA", -100_000, 99], ["NEO", 0, 98], ["DUSK", 0, 98], ["SENT", 0, 95], ["LUMIA", -100_000, 70], ["IOST", 0, 96], ["COLLECT", 0, 96]],
  ),
  board(
    "spot_flow", "usd",
    [["BTC", 5_200_000], ["SOL", 900_000], ["XRP", 600_000], ["ETH", 200_000], ["HYPE", 200_000], ["ZEC", 100_000], ["ADA", 100_000]],
    [["LIT", -100_000], ["BNB", -100_000], ["XLM", -50_000], ["BONK", -40_000], ["AVAX", -30_000], ["SENT", -20_000], ["BILL", -10_000]],
    [["POWR", 100_000, 100], ["MANTRA", 100_000, 100], ["QTUM", 0, 100], ["AXL", 0, 100], ["VTHO", 0, 100], ["ESPORTS", 0, 96], ["NEO", 0, 100], ["AKE", 0, 80]],
    [["HIVE", 0, 100], ["STX", 0, 99], ["BANK", 0, 97], ["BILL", 0, 98], ["BONK", 0, 87], ["MOVE", 0, 97], ["SENT", 0, 95], ["WIF", 0, 90]],
  ),
];

type EventRanks = readonly [self: number, marketStrength: number, marketAbsolute: number];

function event(
  coin: string,
  label: string,
  direction: "long" | "short",
  observedAt: string,
  detail: string,
  index: number,
  ranks: EventRanks,
) {
  return {
    id: `${observedAt}:${coin}:${label}:${index}`,
    symbol: `${coin}USDT`,
    coin,
    observed_at: observedAt,
    window: "5m",
    event_type: label,
    label,
    detail,
    metric: "state",
    direction,
    value: null,
    rankings: {
      self: { available: true, rank: ranks[0], sample_size: 288, percentile: 96, method: "同币历史窗口" },
      market_strength: { available: true, rank: ranks[1], sample_size: 100, percentile: 95, method: "全场强度" },
      market_absolute: { available: true, rank: ranks[2], sample_size: 100, percentile: 94, method: "全场量级" },
    },
  };
}

const compactEvents = [
  event("DYDX", "OI 暴跌", "short", "2030-07-18T22:53:00Z", "5 分钟内 oi -142万 (-5.4%)", 0, [43, 6, 6]),
  event("AKE", "OI 暴跌", "short", "2030-07-18T22:52:00Z", "5 分钟内 oi -262万 (-1.3%)", 1, [43, 5, 5]),
  event("BANK", "OI 暴跌", "short", "2030-07-18T22:35:00Z", "5 分钟内 oi -102万 (-1.3%)", 2, [43, 7, 11]),
  event("AKE", "OI 暴跌", "short", "2030-07-18T22:34:00Z", "5 分钟内 oi -294万 (-1.5%)", 3, [43, 6, 5]),
  event("JUP", "OI 暴涨", "long", "2030-07-18T22:31:00Z", "5 分钟内 oi +70万 (+1.4%)", 4, [43, 5, 8]),
  event("STRK", "Vol 爆发", "long", "2030-07-18T22:28:00Z", "1 小时内 成交量 233万 (+11.2%)", 5, [4, 17, 63]),
  event("STRK", "Vol 爆发", "long", "2030-07-18T22:28:00Z", "1 小时内 成交量 233万 (+11.2%)", 6, [4, 17, 63]),
  event("LIT", "Vol 爆发", "long", "2030-07-18T22:25:00Z", "5 分钟内 成交量 177万 (+2.4%)", 7, [43, 3, 8]),
  event("AKE", "OI 暴跌", "short", "2030-07-18T22:17:00Z", "1 小时内 oi -1429万 (-7.1%)", 8, [4, 4, 3]),
  event("SENT", "Vol 爆发", "long", "2030-07-18T22:12:00Z", "15 分钟内 成交量 165万 (+2.1%)", 9, [14, 13, 10]),
  event("BANK", "OI 暴涨", "long", "2030-07-18T22:08:00Z", "5 分钟内 oi +58万 (+0.8%)", 10, [43, 7, 8]),
  event("PHA", "价格暴跌", "short", "2030-07-18T22:03:00Z", "5 分钟内 价格 -3.58%", 11, [14, 2, 4]),
];

const wideEvents = [
  event("BANK", "OI 暴涨", "long", "2030-07-18T23:03:00Z", "5 分钟内 oi +55万 (+0.7%)", 0, [43, 7, 8]),
  event("BANK", "OI 暴涨", "long", "2030-07-18T23:03:00Z", "5 分钟内 oi +65万 (+0.7%)", 1, [43, 7, 8]),
  event("AKE", "OI 暴涨", "long", "2030-07-18T23:02:00Z", "5 分钟内 oi +531万 (+2.6%)", 2, [43, 4, 1]),
  event("AKE", "OI 暴涨", "long", "2030-07-18T23:02:00Z", "5 分钟内 oi +531万 (+2.6%)", 3, [43, 4, 1]),
  event("DYDX", "OI 暴跌", "short", "2030-07-18T22:53:00Z", "15 分钟内 oi -146万 (-5.5%)", 4, [14, 6, 9]),
  event("DYDX", "OI 暴跌", "short", "2030-07-18T22:53:00Z", "15 分钟内 oi -146万 (-5.5%)", 5, [14, 6, 9]),
  event("AKE", "OI 暴跌", "short", "2030-07-18T22:52:00Z", "5 分钟内 oi -262万 (-1.3%)", 6, [43, 5, 5]),
  event("BANK", "OI 暴跌", "short", "2030-07-18T22:35:00Z", "5 分钟内 oi -102万 (-1.3%)", 7, [43, 7, 11]),
  event("AKE", "OI 暴跌", "short", "2030-07-18T22:34:00Z", "5 分钟内 oi -294万 (-1.5%)", 8, [43, 5, 5]),
  event("JUP", "OI 暴涨", "long", "2030-07-18T22:31:00Z", "5 分钟内 oi +70万 (+1.4%)", 9, [43, 5, 8]),
  event("STRK", "Vol 爆发", "long", "2030-07-18T22:28:00Z", "1 小时内 成交量 213万 (+10.1%)", 10, [4, 19, 65]),
  event("STRK", "Vol 爆发", "long", "2030-07-18T22:28:00Z", "1 小时内 成交量 213万 (+10.1%)", 11, [4, 19, 65]),
  event("LIT", "Vol 爆发", "long", "2030-07-18T22:25:00Z", "5 分钟内 成交量 177万 (+2.4%)", 12, [43, 3, 8]),
  event("AKE", "OI 暴跌", "short", "2030-07-18T22:17:00Z", "1 小时内 oi -1429万 (-7.1%)", 13, [4, 4, 3]),
];

function realtime(boards: ReturnType<typeof board>[], events: ReturnType<typeof event>[], observedAt: string) {
  const allItems = boards.flatMap((entry) => [
    ...entry.amount_positive.items,
    ...entry.amount_negative.items,
    ...entry.strength_positive.items,
    ...entry.strength_negative.items,
  ]);
  const unique = new Map(allItems.map((item) => [item.symbol, item]));
  const items = [...unique.values()].map((item, index) => ({
    symbol: item.symbol,
    coin: item.coin,
    observed_at: observedAt,
    data_status: "ready",
    windows: { "5m": { available: true, coverage_ratio: 1, price_change_pct: item.unit === "percent" ? item.value : 0, cvd_usd: item.unit === "usd" ? item.value : 0 } },
    resonance: { available: true, direction: item.value >= 0 ? "long" : "short", active_count: Math.max(1, Math.min(5, Math.round(item.strength_percentile / 20))), window_count: 5, windows: [] },
    surge: { available: true, triggered: index < 5, direction: item.value >= 0 ? "long" : "short", score: 100 - index },
    ambush: { available: true, triggered: index >= 5 && index < 10, direction: item.value >= 0 ? "long" : "short", score: 95 - index },
    anomaly_24h: { count: Math.max(1, 30 - index), long_count: item.value >= 0 ? 20 : 6, short_count: item.value < 0 ? 20 : 6 },
  }));
  return { schema_version: "mercu-visual-v1", generated_at: observedAt, observed_at: observedAt, data_status: "ready", coverage: { symbols: items.length }, items, anomaly_events: events, boards: [] };
}

function overview(viewport: Viewport) {
  const wide = viewport === "1920x1080";
  const current = wide
    ? { advancing: 276, declining: 113, breadth_pct: 42, futures_net_flow_usd: 26_000_000, spot_net_flow_usd: 8_000_000, oi_net_change_usd: 98_000_000, futures_positive_ratio: .86, spot_positive_ratio: .89, oi_positive_ratio: .92 }
    : { advancing: 258, declining: 124, breadth_pct: 35, futures_net_flow_usd: 17_000_000, spot_net_flow_usd: 7_000_000, oi_net_change_usd: 64_000_000, futures_positive_ratio: .76, spot_positive_ratio: .86, oi_positive_ratio: .85 };
  const previous = wide
    ? { advancing: 265, declining: 126, breadth_pct: 36, futures_net_flow_usd: 36_000_000, spot_net_flow_usd: 1_000_000, oi_net_change_usd: 93_000_000 }
    : { advancing: 244, declining: 139, breadth_pct: 27, futures_net_flow_usd: 23_000_000, spot_net_flow_usd: -229_000, oi_net_change_usd: 80_000_000 };
  return {
    schema_version: "mercu-visual-v1",
    generated_at: wide ? "2030-07-18T23:04:00Z" : "2030-07-18T22:59:00Z",
    window_sec: 900,
    data_status: "ready",
    warnings: [],
    coverage: { assets: current.advancing + current.declining },
    overview: {
      ...current,
      bias: "inflow",
      comparison: {
        previous,
        delta: {
          breadth_pct: current.breadth_pct - previous.breadth_pct,
          futures_net_flow_usd: current.futures_net_flow_usd - previous.futures_net_flow_usd,
          spot_net_flow_usd: current.spot_net_flow_usd - previous.spot_net_flow_usd,
          oi_net_change_usd: current.oi_net_change_usd - previous.oi_net_change_usd,
        },
      },
    },
  };
}

function confluenceItem(
  coin: string,
  boardCount: number,
  direction: "positive" | "negative",
  divergent = false,
) {
  return { symbol: `${coin}USDT`, coin, board_count: boardCount, direction, divergent };
}

const compactConfluence = {
  amount: [
    confluenceItem("ZEC", 3, "positive"),
    confluenceItem("HYPE", 3, "positive"),
    confluenceItem("SOL", 3, "positive"),
    confluenceItem("BTC", 2, "positive", true),
    confluenceItem("XRP", 2, "positive", true),
    confluenceItem("ADA", 2, "positive", true),
    confluenceItem("DOGE", 2, "negative", true),
  ],
  strength: [
    confluenceItem("QTUM", 3, "positive"),
    confluenceItem("PHA", 3, "negative"),
    confluenceItem("SLP", 2, "positive"),
    confluenceItem("AKE", 2, "negative", true),
    confluenceItem("BONK", 2, "negative"),
  ],
};

const wideConfluence = {
  amount: [
    confluenceItem("ZEC", 3, "positive"),
    confluenceItem("HYPE", 3, "positive"),
    confluenceItem("ADA", 3, "positive"),
    confluenceItem("SOL", 3, "positive"),
    confluenceItem("ETH", 3, "positive"),
    confluenceItem("XRP", 2, "positive"),
    confluenceItem("BTC", 2, "positive"),
  ],
  strength: [
    confluenceItem("QTUM", 3, "positive"),
    confluenceItem("POWR", 2, "positive"),
    confluenceItem("PHA", 2, "negative"),
    confluenceItem("BANANAS31", 2, "positive"),
    confluenceItem("BANK", 2, "negative", true),
    confluenceItem("ESPORTS", 2, "positive"),
    confluenceItem("SENT", 2, "negative"),
  ],
};

export function mercuRadarFixture(viewport: Viewport) {
  const wide = viewport === "1920x1080";
  const boards = wide ? wideBoards : compactBoards;
  const observedAt = wide ? "2030-07-18T23:04:00Z" : "2030-07-18T22:59:00Z";
  return {
    overview: overview(viewport),
    boards: {
      schema_version: "mercu-visual-v1",
      generated_at: observedAt,
      window_sec: 900,
      data_status: "ready",
      warnings: [],
      coverage: { assets: 100 },
      methodology: { flow: "固定视觉回归数据；生产环境使用实时数据" },
      boards,
      confluence: wide ? wideConfluence : compactConfluence,
    },
    realtime: realtime(boards, wide ? wideEvents : compactEvents, observedAt),
  };
}
