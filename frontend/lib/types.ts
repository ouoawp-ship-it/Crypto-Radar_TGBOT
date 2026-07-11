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
  subtitle?: string;
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

export type SignalItem = {
  id?: number;
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
  decision?: DecisionBlock;
};

export type DecisionBlock = {
  label?: string;
  code?: string;
  tone?: string;
  confidence?: number;
  risk_level?: string;
  summary?: string;
  not_advice?: string;
};

export type DecisionItem = {
  symbol?: string;
  coin?: string;
  model_version?: string;
  decision?: DecisionBlock;
  scores?: Record<string, number>;
  reasons?: string[];
  risks?: string[];
  watch_points?: string[];
  factor_explanations?: Array<{ factor?: string; label?: string; score?: number; explanation?: string }>;
  calibration?: Record<string, unknown>;
  related_signals?: SignalItem[];
};

export type OutcomeItem = {
  symbol?: string;
  horizon?: string;
  signal_time?: string;
  module?: string;
  signal_type?: string;
  decision_label?: string;
  decision_code?: string;
  decision_confidence?: number | null;
  risk_level?: string;
  result_label?: string;
  result_tone?: Tone;
  data_status?: string;
  final_return_pct?: number | null;
  max_gain_pct?: number | null;
  max_drawdown_pct?: number | null;
  entry_price?: number | null;
  future_price?: number | null;
};

export type LifecycleItem = {
  id?: number;
  symbol?: string;
  coin?: string;
  current_state?: string;
  state_label?: string;
  first_signal_id?: number | null;
  first_signal_at?: string;
  first_signal_module?: string;
  first_signal_type?: string;
  first_signal_level?: string;
  highest_level?: string;
  lifecycle_score?: number | null;
  risk_score?: number | null;
  latest_signal_id?: number | null;
  latest_signal_at?: string;
  latest_price?: number | null;
  latest_oi?: number | null;
  latest_funding_rate?: number | null;
  price_change_from_first_pct?: number | null;
  oi_change_from_first_pct?: number | null;
  futures_cvd_status?: string;
  spot_cvd_status?: string;
  funding_status?: string;
  exchange_context?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  reasons?: string[];
  not_advice?: string;
};

export type LifecycleEvent = {
  id?: number;
  symbol?: string;
  event_time?: string;
  event_type?: string;
  event_label?: string;
  event_level?: string;
  state_label?: string;
  previous_state?: string;
  new_state?: string;
  price_change_from_first_pct?: number | null;
  oi_change_pct?: number | null;
  futures_cvd_delta?: number | null;
  spot_cvd_delta?: number | null;
  funding_rate?: number | null;
  event_score?: number | null;
  risk_score?: number | null;
  reasons?: string[];
};

export type LifecycleSummaryPayload = {
  summary?: Record<string, unknown>;
  items?: LifecycleItem[];
  not_advice?: string;
};

export type LifecycleDetailPayload = {
  symbol?: string;
  lifecycle?: LifecycleItem;
  events?: LifecycleEvent[];
  metrics?: Array<Record<string, unknown>>;
  not_advice?: string;
};

export type LifecycleIntelligenceItem = {
  lifecycle_id?: number;
  symbol?: string;
  intelligence_score?: number | null;
  quality_label?: string;
  stage?: string;
  stage_label?: string;
  momentum_label?: string;
  capital_confirmation_label?: string;
  risk_label?: string;
  maturity_label?: string;
  confidence_label?: string;
  summary?: string;
  strengths?: string[];
  risks?: string[];
  watch_points?: string[];
  model_version?: string;
  current_state?: string;
  first_signal_level?: string;
  highest_level?: string;
  lifecycle_score?: number | null;
  risk_score?: number | null;
  price_change_from_first_pct?: number | null;
  oi_change_from_first_pct?: number | null;
  upgrade_path?: string;
  result_label?: string;
  final_return_pct?: number | null;
  outcome_status?: string;
  outcome_link_status?: string;
  outcome_coverage_label?: string;
  outcome_maturity_label?: string;
  outcome_coverage_ratio?: number | null;
  outcome_maturity_ratio?: number | null;
  mature_horizons?: string[];
  pending_horizons?: string[];
  unavailable_horizons?: string[];
  similar_count?: number;
};

export type LifecycleIntelligenceSummaryPayload = {
  summary?: Record<string, unknown>;
  quality_distribution?: Array<{ label?: string; count?: number }>;
  stage_distribution?: Array<{ label?: string; count?: number }>;
  items?: LifecycleIntelligenceItem[];
  model_version?: string;
  not_advice?: string;
};

export type LifecycleIntelligenceDetailPayload = {
  symbol?: string;
  intelligence?: LifecycleIntelligenceItem | null;
  replay?: LifecycleReplaySummary | null;
  status?: string;
  message?: string;
  model_version?: string;
  not_advice?: string;
};

export type LifecycleReplaySummary = {
  lifecycle_id?: number;
  symbol?: string;
  replay_version?: string;
  frame_count?: number;
  duration_sec?: number | null;
  first_signal_level?: string;
  upgrade_path?: string;
  highest_level?: string;
  time_to_1h_sec?: number | null;
  time_to_4h_sec?: number | null;
  time_to_24h_sec?: number | null;
  max_price_gain_pct?: number | null;
  max_drawdown_pct?: number | null;
  final_return_pct?: number | null;
  observed_max_price_gain_pct?: number | null;
  observed_max_drawdown_pct?: number | null;
  observed_final_return_pct?: number | null;
  final_state?: string;
  result_label?: string;
  outcome_status?: string;
  outcome_count?: number;
  outcome_link_method?: string;
  outcome_link_status?: string;
  primary_outcome_signal_id?: number | null;
  primary_outcome_link_method?: string;
  mature_horizons?: string[];
  pending_horizons?: string[];
  unavailable_horizons?: string[];
  outcome_coverage_ratio?: number | null;
  outcome_maturity_ratio?: number | null;
  outcome_maturity_label?: string;
  outcome_horizons?: Record<string, string>;
  primary_outcome?: LifecycleOutcomeLinkItem | null;
  summary?: Record<string, unknown>;
};

export type LifecycleReplayFrame = {
  frame_index?: number;
  event_time?: string;
  event_type?: string;
  event_label?: string;
  state_before?: string;
  state_after?: string;
  signal_level?: string;
  price?: number | null;
  price_change_from_first_pct?: number | null;
  oi_change_from_first_pct?: number | null;
  spot_cvd_delta?: number | null;
  futures_cvd_delta?: number | null;
  funding_rate?: number | null;
  lifecycle_score?: number | null;
  risk_score?: number | null;
  intelligence_score?: number | null;
  summary?: string;
};

export type LifecycleReplayPayload = {
  symbol?: string;
  replay?: LifecycleReplaySummary | null;
  status?: string;
  message?: string;
  model_version?: string;
  not_advice?: string;
};

export type LifecycleSimilarityPayload = {
  symbol?: string;
  status?: string;
  message?: string;
  similar_count?: number;
  avg_final_return_pct?: number | null;
  positive_ratio?: number | null;
  avg_max_drawdown_pct?: number | null;
  strong_success_ratio?: number | null;
  samples?: Array<Record<string, unknown>>;
  model_version?: string;
  disclaimer?: string;
  not_advice?: string;
};

export type LifecycleOutcomeHorizonCounts = {
  success?: number;
  pending?: number;
  ready?: number;
  not_due?: number;
  unavailable?: number;
  error?: number;
  missing?: number;
};

export type LifecycleOutcomeLinkItem = {
  lifecycle_id?: number;
  symbol?: string;
  signal_id?: number | null;
  lifecycle_event_id?: number | null;
  horizon?: string;
  outcome_status?: string;
  status?: string;
  link_role?: string;
  link_method?: string;
  link_confidence?: number | null;
  signal_time?: string;
  outcome_time?: string;
  is_primary?: boolean | number;
  final_return_pct?: number | null;
  max_gain_pct?: number | null;
  max_drawdown_pct?: number | null;
  result_label?: string;
  outcome?: {
    signal_id?: number | null;
    horizon?: string;
    data_status?: string;
    status?: string;
    signal_time?: string;
    due_time?: string;
    final_return_pct?: number | null;
    max_gain_pct?: number | null;
    max_drawdown_pct?: number | null;
    result_label?: string;
    updated_at?: string;
  } | null;
};

export type LifecycleOutcomeCoverageItem = {
  lifecycle_id?: number;
  symbol?: string;
  candidate_signal_count?: number;
  linked_signal_count?: number;
  linked_outcome_count?: number;
  horizon_1h_status?: string;
  horizon_4h_status?: string;
  horizon_24h_status?: string;
  horizon_72h_status?: string;
  linked_horizon_count?: number;
  mature_horizon_count?: number;
  link_coverage_ratio?: number | null;
  maturity_ratio?: number | null;
  coverage_label?: string;
  maturity_label?: string;
  unlinked_reason?: string;
  reasons?: string[] | Record<string, unknown>;
  calculated_at?: string;
  updated_at?: string;
};

export type LifecycleOutcomeSummaryPayload = {
  lifecycle_count?: number;
  candidate_signal_count?: number;
  linked_lifecycle_count?: number;
  linked_outcome_count?: number;
  link_coverage_ratio?: number | null;
  mature_lifecycle_count?: number;
  maturity_ratio?: number | null;
  horizons?: Record<string, LifecycleOutcomeHorizonCounts>;
  unlinked_reasons?: Record<string, number>;
  summary?: Record<string, unknown>;
  generated_at?: string;
  not_advice?: string;
};

export type LifecycleOutcomeDetailPayload = {
  available?: boolean;
  symbol?: string;
  coverage?: LifecycleOutcomeCoverageItem | null;
  primary_outcome?: LifecycleOutcomeLinkItem | null;
  primary?: LifecycleOutcomeLinkItem | null;
  links?: LifecycleOutcomeLinkItem[];
  horizons?: Record<string, LifecycleOutcomeHorizonCounts | string>;
  mature_horizons?: string[];
  pending_horizons?: string[];
  unavailable_horizons?: string[];
  outcome_link_status?: string;
  link_method?: string;
  confidence_label?: string;
  not_advice?: string;
};

export type LifecycleOutcomeReasonsPayload = {
  reasons?: Record<string, number>;
  unlinked_reasons?: Record<string, number>;
  total?: number;
  not_advice?: string;
};

export type LifecycleOutcomeMaturityPayload = {
  lifecycle_count?: number;
  mature_lifecycle_count?: number;
  maturity_ratio?: number | null;
  horizons?: Record<string, LifecycleOutcomeHorizonCounts>;
  labels?: Record<string, number>;
  not_advice?: string;
};

export type BacktestGroup = {
  key?: string;
  label?: string;
  decision_code?: string;
  decision_label?: string;
  total_count?: number;
  success_count?: number;
  pending_count?: number;
  unavailable_count?: number;
  error_count?: number;
  coverage_ratio?: number;
  avg_final_return_pct?: number | null;
  median_final_return_pct?: number | null;
  avg_max_gain_pct?: number | null;
  avg_max_drawdown_pct?: number | null;
  positive_ratio?: number;
  strong_ratio?: number;
  drawdown_ratio?: number;
  avg_gain_drawdown_ratio?: number | null;
  expectancy_score?: number | null;
  sample_quality?: string;
  diagnosis?: string;
};

export type BacktestPayload = {
  summary?: BacktestGroup & { headline?: string };
  decision_groups?: BacktestGroup[];
  module_groups?: BacktestGroup[];
  risk_groups?: BacktestGroup[];
  confidence_groups?: BacktestGroup[];
  model_diagnosis?: {
    overall_label?: string;
    overall_summary?: string;
    strengths?: string[];
    weaknesses?: string[];
    calibration_hints?: string[];
    data_warnings?: string[];
  };
  coverage?: Record<string, number>;
  filters?: Record<string, unknown>;
};

export type BacktestMatrixPayload = {
  items?: Array<{
    decision_code?: string;
    decision_label?: string;
    horizons?: Record<string, BacktestGroup>;
  }>;
  horizons?: string[];
};

export type CoinItem = {
  coin?: string;
  symbol?: string;
  count?: number;
  latest_at?: string;
  module_count?: number;
  failed_count?: number;
  label?: string;
  subtitle?: string;
};

export type ListPayload<T> = {
  items?: T[];
  count?: number;
  next_cursor?: string | number | null;
  summary?: Record<string, unknown>;
  filters?: Record<string, unknown>;
  pagination?: Record<string, unknown>;
};
