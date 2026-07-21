export type ApiEnvelope<T> = {
  ok?: boolean;
  data?: T;
  items?: unknown[];
  message?: string;
  error?: unknown;
  _meta?: Record<string, unknown>;
};

export type ApiResult<T> = {
  ok: boolean;
  data?: T;
  error?: string;
  status?: number;
  path?: string;
};

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

export type DisplayInfo = {
  title?: string;
  module_label?: string;
  status_label?: string;
  symbol_label?: string;
  time_label?: string;
  score_label?: string;
  stage_label?: string;
  summary?: string;
  card_tone?: Tone;
  badges?: Array<{ label?: string; tone?: Tone }>;
};

export type SignalRank = {
  available?: boolean;
  label?: string;
  value?: number;
  rank?: number;
  sample_size?: number;
  percentile?: number;
  method?: string;
  reason?: string;
  metric?: { key?: string; label?: string; unit?: string; value?: number; quality?: string };
};

export type SignalResonance = {
  label?: string;
  active_count?: number;
  window_count?: number;
  available?: boolean;
  method?: string;
  windows?: Array<{ key?: string; seconds?: number; active?: boolean; module_count?: number; signal_count?: number; modules?: string[] }>;
};

export type SignalLifecycle = {
  state?: string;
  label?: string;
  derived?: boolean;
  observed_at?: string;
  age_sec?: number;
  rule?: "surge" | "ambush" | string | null;
  direction?: "long" | "short" | "neutral" | string | null;
  basis?: string;
  previous_signal_id?: number | null;
};

export type SignalIntelligence = {
  self_rank?: SignalRank;
  market_strength_rank?: SignalRank;
  market_absolute_rank?: SignalRank;
  resonance?: SignalResonance;
  lifecycle?: SignalLifecycle;
};

export type SignalItem = {
  id?: number;
  public_ref?: string;
  time?: string;
  symbol?: string;
  coin?: string;
  module?: string;
  status?: string;
  signal_type?: string;
  score?: number | string | null;
  stage?: string;
  excerpt?: string;
  display?: DisplayInfo;
  intelligence?: SignalIntelligence;
};

export type ListPayload<T> = {
  items?: T[];
  count?: number;
  next_cursor?: number | null;
  filters?: Record<string, unknown>;
};

export type MetricStatus = "fresh" | "stale" | "degraded" | "unavailable";

export type MarketMetric = {
  value?: number | null;
  unit?: "usd" | "percent" | "percent_per_cycle" | "ratio" | string;
  source?: string;
  observed_at?: string;
  age_sec?: number;
  status?: MetricStatus;
  quality?: "direct" | "derived" | "missing" | string;
};

export type FundingExchange = {
  exchange?: string;
  funding_pct?: number | null;
  interval_hours?: number;
  last_funding_time?: string;
  next_funding_time?: string;
  extreme_label?: string;
};

export type MarketSnapshot = {
  schema_version?: string;
  symbol?: string;
  coin?: string;
  status?: MetricStatus;
  updated_at?: string;
  age_sec?: number;
  metrics?: Record<string, MarketMetric>;
  funding_exchanges?: FundingExchange[];
  tiers?: { market_cap?: string; liquidity?: string };
};

export type SignalEvidence = {
  key?: string;
  label?: string;
  description?: string;
  metric?: MarketMetric;
  value?: string;
  tone?: string;
};

export type SignalContext = {
  schema_version?: string;
  signal?: SignalItem;
  market?: MarketSnapshot | null;
  market_error?: string;
  evidence?: SignalEvidence[];
  lifecycle?: SignalLifecycle & { started_at?: string; duration_sec?: number };
  rankings?: { self?: SignalRank; market_strength?: SignalRank; market_absolute?: SignalRank };
  resonance?: SignalResonance;
  related?: { same_symbol?: SignalItem[] };
  actions?: { signal_url?: string; symbol_url?: string; ai_url?: string; alert_url?: string };
};

export type IntelligenceEntry = { signal?: SignalItem; intelligence?: SignalIntelligence };

export type OpportunityBoard = {
  key?: "launch" | "resonance" | "funding" | "risk" | string;
  title?: string;
  description?: string;
  count?: number;
  items?: IntelligenceEntry[];
};

export type RadarIntelligence = {
  schema_version?: string;
  generated_at?: string;
  window_sec?: number;
  data_status?: "ready" | "empty" | string;
  methodology?: Record<string, string>;
  summary?: { signals?: number; symbols?: number; resonance_symbols?: number; enhancing_symbols?: number };
  projection?: { requested?: number; returned?: number; max_items?: number };
  items?: IntelligenceEntry[];
  boards?: OpportunityBoard[];
};

export type CockpitBoardItem = {
  symbol?: string;
  coin?: string;
  asset_type?: string;
  price?: number | null;
  value?: number | null;
  unit?: "usd" | "percent" | "percent_per_cycle" | string;
  magnitude_usd?: number | null;
  strength_percentile?: number | null;
  window_states?: Partial<Record<"15m" | "30m" | "1h" | "4h" | "1d", boolean>>;
  updated_at?: string;
  status?: MetricStatus | string;
  quality?: string;
};

export type CockpitBoardSide = {
  title?: string;
  items?: CockpitBoardItem[];
};

export type CockpitBoard = {
  key?: "price" | "oi" | "futures_flow" | "spot_flow" | "funding" | string;
  title?: string;
  metric?: string;
  unit?: string;
  available?: boolean;
  coverage?: number;
  positive?: CockpitBoardSide;
  negative?: CockpitBoardSide;
  amount_metric?: string;
  amount_unit?: string;
  amount_positive?: CockpitBoardSide;
  amount_negative?: CockpitBoardSide;
  strength_positive?: CockpitBoardSide;
  strength_negative?: CockpitBoardSide;
  reason?: string;
};

export type RadarConfluenceItem = CockpitBoardItem & {
  board_count?: number;
  divergent?: boolean;
  direction?: "positive" | "negative" | "inflow" | "outflow" | string;
};

export type RadarConfluence = {
  amount?: RadarConfluenceItem[];
  strength?: RadarConfluenceItem[];
};

export type MarketCoverage = {
  assets?: number;
  price?: number;
  oi?: number;
  spot_flow?: number;
  futures_flow?: number;
  funding?: number;
};

export type MarketReadiness = {
  status?: "ready" | "warming_up" | "partial" | "stale" | "empty" | string;
  rows?: number;
  symbols_seen?: number;
  oldest_at?: string;
  latest_at?: string;
  history_span_sec?: number;
  history_target_sec?: number;
  requested_window_sec?: number;
  warmup_progress_pct?: number;
  warmup_remaining_sec?: number;
  estimated_full_history_at?: string;
  freshness?: { status?: string; age_sec?: number | null; budget_sec?: number };
  coverage?: MarketCoverage & {
    price_ratio?: number;
    funding_ratio?: number;
    oi_target?: number;
    oi_ratio?: number;
    flow_target?: number;
    spot_flow_ratio?: number;
    futures_flow_ratio?: number;
  };
  source_status?: Array<{ source?: string; rows?: number; latest_at?: string }>;
};

export type MarketOverview = {
  schema_version?: string;
  generated_at?: string;
  window_sec?: number;
  data_status?: "ready" | "degraded" | "empty" | string;
  warnings?: string[];
  coverage?: MarketCoverage;
  readiness?: MarketReadiness;
  overview?: {
    bias?: "inflow" | "outflow" | "broad_up" | "broad_down" | "mixed" | string;
    advancing?: number;
    declining?: number;
    flat?: number;
    breadth_pct?: number;
    total_quote_volume?: number;
    spot_net_flow_usd?: number | null;
    futures_net_flow_usd?: number | null;
    oi_net_change_usd?: number | null;
    spot_positive_ratio?: number | null;
    futures_positive_ratio?: number | null;
    oi_positive_ratio?: number | null;
    comparison?: {
      previous?: {
        advancing?: number | null;
        declining?: number | null;
        breadth_pct?: number | null;
        spot_net_flow_usd?: number | null;
        futures_net_flow_usd?: number | null;
        oi_net_change_usd?: number | null;
      };
      delta?: {
        breadth_pct?: number | null;
        spot_net_flow_usd?: number | null;
        futures_net_flow_usd?: number | null;
        oi_net_change_usd?: number | null;
      };
    };
  };
};

export type RadarBoards = {
  schema_version?: string;
  generated_at?: string;
  window_sec?: number;
  data_status?: "ready" | "degraded" | "empty" | string;
  warnings?: string[];
  coverage?: MarketCoverage;
  readiness?: MarketReadiness;
  boards?: CockpitBoard[];
  confluence?: RadarConfluence;
  methodology?: Record<string, string>;
};

export type RealtimeWindow = {
  available?: boolean;
  coverage_ratio?: number;
  gross_trade_usd?: number;
  cvd_usd?: number;
  cvd_ratio_pct?: number;
  price_change_pct?: number;
  long_liquidation_usd?: number;
  short_liquidation_usd?: number;
};

export type RealtimeRuleAnalysis = {
  available?: boolean;
  triggered?: boolean;
  direction?: "long" | "short" | "neutral" | string;
  score?: number;
  flow_acceleration_pp?: number | null;
  volume_acceleration_pct?: number | null;
  liquidation_bias_pct?: number;
  price_compression_pct?: number;
};

export type RealtimeIntelligenceItem = {
  symbol?: string;
  coin?: string;
  asset_type?: string;
  observed_at?: string;
  data_status?: string;
  windows?: Record<string, RealtimeWindow>;
  surge?: RealtimeRuleAnalysis;
  ambush?: RealtimeRuleAnalysis;
  anomaly_24h?: { count?: number; long_count?: number; short_count?: number; latest_at?: string; method?: string };
  resonance?: {
    available?: boolean;
    direction?: string;
    active_count?: number;
    window_count?: number;
    windows?: Array<{ key?: string; active?: boolean; direction?: string; coverage_ratio?: number }>;
  };
  lifecycle?: SignalLifecycle;
  rankings?: { self?: SignalRank; market_strength?: SignalRank; market_absolute?: SignalRank };
};

export type RealtimeAnomalyEvent = {
  id?: string;
  symbol?: string;
  coin?: string;
  observed_at?: string;
  window?: "5m" | "15m" | "1h" | string;
  window_sec?: number;
  event_type?: "price_up" | "price_down" | "volume_spike" | "perp_inflow" | "perp_outflow" | "long_liquidation" | "short_liquidation" | "oi_up" | "oi_down" | string;
  label?: string;
  detail?: string;
  metric?: string;
  direction?: "long" | "short" | "neutral" | string;
  value?: number | null;
  value_usd?: number | null;
  change_pct?: number | null;
  rankings?: { self?: SignalRank; market_strength?: SignalRank; market_absolute?: SignalRank };
};

export type RealtimeIntelligencePayload = {
  schema_version?: string;
  generated_at?: string;
  observed_at?: string;
  data_status?: string;
  coverage?: Record<string, number>;
  methodology?: Record<string, string>;
  items?: RealtimeIntelligenceItem[];
  anomaly_events?: RealtimeAnomalyEvent[];
  boards?: Array<{ key?: string; title?: string; count?: number; description?: string; items?: RealtimeIntelligenceItem[] }>;
};

export type WorkstationRadarAnomaliesPayload = {
  schema_version?: string;
  generated_at?: string;
  observed_at?: string;
  data_status?: string;
  warnings?: string[];
  coverage?: Record<string, number>;
  methodology?: Record<string, string | boolean>;
  items?: RealtimeAnomalyEvent[];
};

export type WorkstationRadarSurgePayload = {
  schema_version?: string;
  generated_at?: string;
  observed_at?: string;
  data_status?: string;
  warnings?: string[];
  coverage?: Record<string, number>;
  methodology?: Record<string, string | boolean>;
  items?: RealtimeIntelligenceItem[];
};

export type WorkstationRadarRankPayload = {
  schema_version?: string;
  generated_at?: string;
  observed_at?: string;
  data_status?: string;
  warnings?: string[];
  coverage?: Record<string, number>;
  methodology?: Record<string, string | boolean>;
  universe?: RealtimeIntelligenceItem[];
  total?: RealtimeIntelligenceItem[];
  ambush?: RealtimeIntelligenceItem[];
};

export type WorkstationRadarBrief = {
  id?: string;
  symbol?: string;
  coin?: string;
  observed_at?: string;
  direction?: string;
  title?: string;
  summary?: string;
  rankings?: { self?: SignalRank; market_strength?: SignalRank; market_absolute?: SignalRank };
};

export type WorkstationRadarBriefsPayload = {
  schema_version?: string;
  generated_at?: string;
  observed_at?: string;
  data_status?: string;
  warnings?: string[];
  coverage?: Record<string, number>;
  methodology?: Record<string, string | boolean>;
  items?: WorkstationRadarBrief[];
};

export type SectorDefinition = { id?: string; label?: string; description?: string };

export type AssetSector = {
  catalog_version?: string;
  primary_sector_id?: string;
  primary_sector_label?: string;
  sector_ids?: string[];
  sector_labels?: string[];
};

export type SectorFlow = {
  sector_id?: string;
  label?: string;
  description?: string;
  market_type?: "spot" | "futures" | string;
  inflow_usd?: number | null;
  outflow_usd?: number | null;
  net_flow_usd?: number | null;
  magnitude_usd?: number | null;
  asset_count?: number;
  covered_assets?: number;
  coverage_ratio?: number;
  data_status?: MetricStatus | "ready" | "empty" | string;
  leaders?: Array<{ symbol?: string; net_flow_usd?: number | null; data_status?: string }>;
};

export type FundsSectorsPayload = {
  schema_version?: string;
  catalog_version?: string;
  generated_at?: string;
  window_sec?: number;
  market_type?: "spot" | "futures" | string;
  data_status?: string;
  coverage?: { assets?: number; flow?: number; gross_flow?: number; oi?: number; market_cap?: number };
  warnings?: string[];
  summary?: {
    net_flow_usd?: number | null;
    inflow_usd?: number | null;
    outflow_usd?: number | null;
    asset_count?: number;
    covered_assets?: number;
    leading_inflow_sector?: string;
    leading_outflow_sector?: string;
  };
  catalog?: SectorDefinition[];
  sectors?: SectorFlow[];
  methodology?: Record<string, string>;
};

export type FundsAsset = {
  symbol?: string;
  coin?: string;
  price?: number | null;
  price_change_pct?: number | null;
  price_change_window_sec?: number;
  net_flow_usd?: number | null;
  net_flow_change_pct?: number | null;
  inflow_usd?: number | null;
  outflow_usd?: number | null;
  volume_usd?: number | null;
  volume_change_pct?: number | null;
  oi_usd?: number | null;
  oi_change_usd?: number | null;
  oi_change_pct?: number | null;
  funding_pct?: number | null;
  market_cap?: number | null;
  sector?: AssetSector;
  updated_at?: string;
  age_sec?: number;
  data_status?: string;
  quality?: Record<string, string>;
  sources?: Record<string, string>;
};

export type FundsAssetsPayload = {
  schema_version?: string;
  catalog_version?: string;
  generated_at?: string;
  window_sec?: number;
  market_type?: string;
  data_status?: string;
  coverage?: Record<string, number>;
  warnings?: string[];
  filters?: Record<string, string>;
  sort?: { key?: string; direction?: "asc" | "desc" | string };
  distribution?: {
    oi_total_usd?: number | null;
    oi_covered_assets?: number;
    top_10_oi_share_pct?: number | null;
    top_50_oi_share_pct?: number | null;
  };
  pagination?: { page?: number; page_size?: number; page_count?: number; total?: number };
  items?: FundsAsset[];
  methodology?: Record<string, string>;
};

export type CrossExchangeOpenInterest = {
  schema_version?: string;
  symbol?: string;
  data_status?: string;
  coverage?: { exchanges?: number; target?: number };
  mark_price?: number | null;
  total_oi_usd?: number | null;
  top_exchange_share_pct?: number | null;
  exchanges?: Array<{
    exchange?: string;
    oi_usd?: number | null;
    oi_native?: number | null;
    share_pct?: number | null;
    status?: string;
    source?: string;
  }>;
  methodology?: Record<string, string>;
};

export type FundsOverviewPayload = {
  schema_version?: string;
  generated_at?: string;
  window_sec?: number;
  sector_window_sec?: number;
  asset_window_sec?: number;
  market_type?: "spot" | "futures" | string;
  data_status?: string;
  coverage?: Record<string, number>;
  warnings?: string[];
  summary?: FundsSectorsPayload["summary"];
  distribution?: FundsAssetsPayload["distribution"];
  catalog?: SectorDefinition[];
  sectors?: SectorFlow[];
  assets?: FundsAsset[];
  filters?: Record<string, string>;
  sort?: FundsAssetsPayload["sort"];
  pagination?: FundsAssetsPayload["pagination"];
  methodology?: Record<string, string>;
};

export type KlinePoint = {
  open_time?: string;
  open_time_ms?: number;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  base_volume?: number | null;
  close_time_ms?: number;
  quote_volume?: number | null;
  taker_buy_quote_volume?: number | null;
};

export type CoinChart = {
  market_type?: string;
  interval?: string;
  interval_sec?: number;
  source?: string;
  data_status?: string;
  coverage?: { requested?: number; returned?: number };
  points?: KlinePoint[];
  warnings?: string[];
};

export type CoinSeriesPoint = {
  observed_at?: number;
  updated_at?: string;
  price?: number | null;
  quote_volume?: number | null;
  market_cap?: number | null;
  oi_usd?: number | null;
  oi_change_usd?: number | null;
  oi_change_pct?: number | null;
  spot_inflow_usd?: number | null;
  spot_outflow_usd?: number | null;
  spot_flow_usd?: number | null;
  futures_inflow_usd?: number | null;
  futures_outflow_usd?: number | null;
  futures_flow_usd?: number | null;
  funding_pct?: number | null;
  sources?: string[];
};

export type CoinSeries = {
  data_status?: string;
  interval?: string;
  interval_sec?: number;
  requested_buckets?: number;
  coverage?: Record<string, number>;
  points?: CoinSeriesPoint[];
  analytics?: FundsSeriesAnalytics;
  warnings?: string[];
  methodology?: Record<string, string>;
};

export type CoinContext = {
  schema_version?: string;
  symbol?: string;
  coin?: string;
  market?: MarketSnapshot | null;
  market_error?: string;
  data_status?: string;
  warnings?: string[];
  summary?: { signal_count?: number; sent_count?: number; module_counts?: Record<string, number>; latest_at?: string };
  chart?: CoinChart;
  series?: CoinSeries;
  funds_profile?: FundsProfile;
  related_info?: { data_status?: string; items?: CoinRelatedInfoItem[]; methodology?: string };
  evidence_coverage?: { market?: number; chart_points?: number; snapshot_points?: number; signals?: number; related_info?: number; announcements?: number };
  timeline?: SignalItem[];
  actions?: { radar_url?: string; share_url?: string; ai_url?: string; alert_url?: string };
};

export type WatchlistMarketItem = {
  symbol?: string;
  ok?: boolean;
  market?: MarketSnapshot | null;
  error?: string;
  coin_url?: string;
  flow?: {
    window_sec?: number;
    spot_net_flow_usd?: number | null;
    futures_net_flow_usd?: number | null;
    oi_change_pct?: number | null;
    funding_pct?: number | null;
    updated_at?: string;
    data_status?: string;
    source?: string;
  } | null;
};

export type WatchlistMarketPayload = { items?: WatchlistMarketItem[]; count?: number; invalid?: string[] };

export type NewsAnalysis = {
  status?: "ready" | "not_generated" | string;
  fact_summary?: string;
  possible_impact?: string;
  related_assets?: string[];
  verification_needed?: string[];
  fact_inference_boundary?: string;
  generated_by?: string;
  version?: string;
  reason?: string;
  author_handle?: string;
  author_display_name?: string;
  engagement?: { likes?: number; reposts?: number; replies?: number; score?: number };
};

export type NewsEvent = {
  event_id?: string;
  published_at?: string;
  collected_at?: string;
  source?: string;
  source_type?: string;
  title?: string;
  summary?: string;
  url?: string;
  symbols?: string[];
  importance?: "high" | "medium" | "low" | string;
  language?: "zh" | "en" | string;
  cluster_id?: string;
  cluster_size?: number;
  event_kind?: "risk" | "opportunity" | "neutral" | string;
  ai_analysis?: NewsAnalysis;
  rights_status?: string;
  source_links?: Array<{ source?: string; url?: string; rights_status?: string }>;
  timestamp_quality?: string;
  data_status?: string;
};

export type CoinRelatedInfoItem = SignalItem & NewsEvent;

export type InfoChannel = {
  key?: string;
  label?: string;
  status?: string;
  count?: number;
  rights_status?: string;
  reason?: string;
};

export type InfoPlazaRank = {
  symbol?: string;
  coin?: string;
  asset_type?: string;
  posts?: number;
  recent_1h_posts?: number;
  previous_1h_posts?: number;
  recent_ratio?: number | null;
  is_new?: boolean;
  positive?: number;
  negative?: number;
  neutral?: number;
  positive_pct?: number;
  negative_pct?: number;
  sentiment?: "bullish" | "bearish" | "neutral" | string;
  sentiment_confidence_pct?: number;
  likes?: number;
  reposts?: number;
  replies?: number;
  engagement?: number;
  engagement_per_post?: number;
  latest_at?: string;
  summary?: string;
  price_change_pct?: number | null;
  futures_flow_usd?: number | null;
  futures_flow_strength?: number | null;
  futures_long_pct?: number | null;
  futures_short_pct?: number | null;
  market_updated_at?: string;
  market_status?: string;
};

export type InfoPlazaRankings = {
  schema_version?: string;
  generated_at?: string;
  data_status?: string;
  provider?: { id?: string; label?: string; kind?: string; rights_status?: string };
  active_4h?: InfoPlazaRank[];
  total_24h?: InfoPlazaRank[];
  coverage?: Record<string, number>;
  methodology?: Record<string, string>;
};

export type FundsSeriesPayload = CoinSeries & {
  schema_version?: string;
  generated_at?: string;
  symbol?: string;
  kind?: "spot_flow" | "futures_flow" | "oi" | "funding" | string;
  metric?: string;
  analytics?: FundsSeriesAnalytics;
};

export type FundsSeriesAnalytics = {
  data_status?: string;
  metric?: string;
  net_flow_usd?: number | null;
  direction?: "inflow" | "outflow" | "neutral" | string;
  latest_direction?: "inflow" | "outflow" | "neutral" | string;
  duration_sec?: number;
  hit_rate_pct?: number | null;
  hit_samples?: number;
  price?: { first?: number | null; current?: number | null; change_pct?: number | null; high?: number | null; low?: number | null };
  coverage?: Record<string, number>;
  methodology?: Record<string, string>;
};

export type FundsVolumeProfile = {
  data_status?: string;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  range_high?: number | null;
  range_low?: number | null;
  value_area_ratio?: number;
  coverage?: Record<string, number>;
  methodology?: string;
};

export type FundsProfile = {
  schema_version?: string;
  market_type?: string;
  interval?: string;
  source?: string;
  volume_profile?: FundsVolumeProfile;
};

export type InfoFeedPayload = {
  schema_version?: string;
  generated_at?: string;
  data_status?: string;
  coverage?: Record<string, number>;
  warnings?: string[];
  filters?: Record<string, string | number>;
  pagination?: { page?: number; page_size?: number; page_count?: number; total?: number; bounded_at?: number };
  summary?: { high_importance?: number; risk?: number; opportunity?: number; official?: number };
  channels?: InfoChannel[];
  items?: NewsEvent[];
  plaza_rankings?: InfoPlazaRankings | null;
  methodology?: Record<string, string>;
};

export type InfoDashboardPayload = {
  schema_version?: string;
  generated_at?: string;
  data_status?: string;
  coverage?: Record<string, number>;
  warnings?: string[];
  summary?: InfoFeedPayload["summary"];
  channels?: InfoChannel[];
  ingestion?: { status?: string; error?: string };
  methodology?: Record<string, string>;
};

export type InfoBrief = {
  channel?: "news" | "en" | "kol" | "plaza" | string;
  data_status?: string;
  title?: string;
  summary?: string;
  event_id?: string;
  published_at?: string;
  symbols?: string[];
  source?: string;
  source_url?: string;
  generated_by?: string;
  model_generated?: boolean;
};

export type InfoBriefsPayload = {
  schema_version?: string;
  generated_at?: string;
  window_sec?: number;
  data_status?: string;
  coverage?: Record<string, number>;
  warnings?: string[];
  items?: InfoBrief[];
  methodology?: Record<string, string>;
};

export type EvidenceFact = {
  ref?: string;
  kind?: "market_metric" | "signal_event" | "news_event" | string;
  scope?: string;
  key?: string;
  label?: string;
  value?: number | string | null;
  unit?: string;
  source?: string;
  observed_at?: string;
  data_status?: string;
  url?: string;
  note?: string;
};

export type AgentInsight = {
  insight_id?: string;
  agent_type?: "global" | "major" | "anomaly" | "message" | string;
  scope?: string;
  generated_at?: string;
  expires_at?: string;
  state?: string;
  confidence?: number | null;
  summary?: string;
  evidence_refs?: string[];
  counter_evidence_refs?: string[];
  model_info?: Record<string, string | boolean>;
  data_status?: string;
  disclaimer?: string;
  label?: string;
  state_label?: string;
  bucket?: "strong" | "weak" | "risk" | string;
  missing_facts?: string[];
  event_id?: string;
  symbols?: string[];
  actions?: { coin_url?: string; radar_url?: string; signal_ref?: string; info_url?: string; source_url?: string; ai_url?: string };
};

export type AgentsOverviewPayload = {
  schema_version?: string;
  engine_version?: string;
  generated_at?: string;
  expires_at?: string;
  window_sec?: number;
  data_status?: string;
  coverage?: Record<string, number>;
  warnings?: string[];
  agents?: {
    global?: AgentInsight;
    majors?: AgentInsight[];
    anomalies?: AgentInsight[];
    messages?: AgentInsight[];
  };
  evidence?: EvidenceFact[];
  model_info?: Record<string, string | boolean>;
  safety?: {
    rule_first?: boolean;
    ready_only_for_direction?: boolean;
    numbers_formatted_by_code?: boolean;
    evidence_required?: boolean;
    disclaimer?: string;
  };
};
