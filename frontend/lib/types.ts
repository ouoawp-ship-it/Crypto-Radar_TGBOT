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
  lifecycle_link_coverage_ratio?: number | null;
  eligible_candidate_count?: number;
  ineligible_candidate_count?: number;
  linked_candidate_count?: number;
  candidate_link_coverage_ratio?: number | null;
  due_candidate_count?: number;
  resolved_due_candidate_count?: number;
  due_resolution_ratio?: number | null;
  successful_due_candidate_count?: number;
  usable_outcome_maturity_ratio?: number | null;
  mature_lifecycle_count?: number;
  maturity_ratio?: number | null;
  lifecycle_maturity_ratio?: number | null;
  generic_unclassified_count?: number;
  real_error_count?: number;
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
  candidate_quality?: LifecycleOutcomeQualitySummaryPayload | null;
  quality?: LifecycleOutcomeQualitySummaryPayload | null;
  not_advice?: string;
};

export type LifecycleOutcomeQualityHorizonItem = {
  horizon?: string;
  label?: string;
  candidate_count?: number;
  eligible?: number;
  eligible_count?: number;
  ineligible?: number;
  ineligible_count?: number;
  linked?: number;
  linked_count?: number;
  not_due?: number;
  ready?: number;
  queued?: number;
  processing?: number;
  success?: number;
  unavailable?: number;
  terminal_unavailable?: number;
  retry_wait?: number;
  error?: number;
  terminal_error?: number;
  maturity_ratio?: number | null;
  resolution_ratio?: number | null;
};

export type LifecycleOutcomeQualityDimensionItem = LifecycleOutcomeQualityHorizonItem & {
  key?: string;
  module?: string;
  first_signal_level?: string;
  signal_type?: string;
  time_range?: string;
  success_count?: number;
  unavailable_count?: number;
  error_count?: number;
  top_gap_reasons?: Record<string, number> | string[] | Array<{ reason?: string; count?: number }>;
  link_coverage_ratio?: number | null;
};

export type LifecycleOutcomeQualitySummaryPayload = LifecycleOutcomeSummaryPayload & {
  candidate_count?: number;
  outcome_candidate_count?: number;
  eligible?: number;
  eligible_count?: number;
  ineligible?: number;
  ineligible_count?: number;
  linked?: number;
  linked_count?: number;
  not_due?: number;
  ready?: number;
  queued?: number;
  processing?: number;
  success?: number;
  unavailable?: number;
  terminal_unavailable?: number;
  retry_wait?: number;
  terminal_error?: number;
  generic_no_outcome_row?: number;
  reasons?: Record<string, number>;
  status_counts?: Record<string, number>;
  top_gap_reasons?: Record<string, number>;
  next_retry_at?: string | null;
  items?: LifecycleOutcomeQualityDimensionItem[];
};

export type LifecycleOutcomeQualityListPayload = {
  items?: LifecycleOutcomeQualityDimensionItem[];
  summary?: LifecycleOutcomeQualitySummaryPayload | Record<string, unknown>;
  reasons?: Record<string, number>;
  generated_at?: string;
  not_advice?: string;
};

export type LifecycleCalibrationReadinessPayload = {
  ready?: boolean;
  label?: string;
  passed?: string[];
  blocked?: string[];
  warnings?: string[];
  current?: Record<string, unknown>;
  required?: Record<string, unknown>;
  generated_at?: string;
  not_advice?: string;
};

export type CalibrationMetricItem = {
  metric_key?: string;
  key?: string;
  label?: string;
  decision_code?: string;
  decision_label?: string;
  first_signal_level?: string;
  timeframe?: string;
  factor?: string;
  factor_label?: string;
  risk_type?: string;
  risk_label?: string;
  sample_count?: number;
  mature_sample_count?: number;
  total_count?: number;
  count?: number;
  success_count?: number;
  success_ratio?: number | null;
  success_rate?: number | null;
  positive_ratio?: number | null;
  avg_return_pct?: number | null;
  avg_final_return_pct?: number | null;
  median_return_pct?: number | null;
  avg_max_gain_pct?: number | null;
  avg_max_drawdown_pct?: number | null;
  drawdown_ratio?: number | null;
  expectancy_pct?: number | null;
  confidence_accuracy?: number | null;
  alert_count?: number;
  event_count?: number;
  avg_lead_time_sec?: number | null;
  avg_lead_time_min?: number | null;
  effectiveness_ratio?: number | null;
  avoided_loss_ratio?: number | null;
  status?: string;
  conclusion?: string;
  [key: string]: unknown;
};

export type CalibrationSummaryPayload = {
  calibration_version?: string;
  model_version?: string;
  generated_at?: string;
  status?: string;
  status_label?: string;
  label?: string;
  sample_count?: number;
  total_samples?: number;
  total_count?: number;
  mature_sample_count?: number;
  mature_samples?: number;
  unavailable_count?: number;
  maturity_ratio?: number | null;
  decision_group_count?: number;
  lifecycle_sample_count?: number;
  not_advice?: string;
  summary?: Record<string, unknown>;
  [key: string]: unknown;
};

export type CalibrationSectionPayload = CalibrationSummaryPayload & {
  items?: CalibrationMetricItem[];
  decision_labels?: CalibrationMetricItem[];
  first_levels?: CalibrationMetricItem[];
  upgrade_paths?: CalibrationMetricItem[];
  intelligence_buckets?: CalibrationMetricItem[];
  factors?: Record<string, CalibrationMetricItem[] | CalibrationMetricItem | unknown>;
  risk_alerts?: CalibrationMetricItem[] | Record<string, CalibrationMetricItem | unknown>;
};

export type CalibrationReadinessPayload = {
  ready?: boolean;
  label?: string;
  status?: string;
  passed?: string[];
  blocked?: string[];
  warnings?: string[];
  current?: Record<string, unknown>;
  required?: Record<string, unknown>;
  generated_at?: string;
  calculated_at?: string;
  note?: string;
  not_advice?: string;
  [key: string]: unknown;
};

export type OptimizationFactorChange = {
  factor?: string;
  factor_key?: string;
  factor_label?: string;
  label?: string;
  old_value?: number | string | boolean | null;
  new_value?: number | string | boolean | null;
  production_value?: number | string | boolean | null;
  candidate_value?: number | string | boolean | null;
  delta?: number | string | null;
  delta_pct?: number | null;
  [key: string]: unknown;
};

export type OptimizationComparisonMetric = {
  metric?: string;
  metric_key?: string;
  label?: string;
  production?: number | string | null;
  production_value?: number | string | null;
  candidate?: number | string | null;
  candidate_value?: number | string | null;
  delta?: number | string | null;
  delta_pct?: number | null;
  [key: string]: unknown;
};

export type OptimizationScenarioItem = {
  scenario?: string;
  scenario_id?: string | number;
  scenario_key?: string;
  name?: string;
  scenario_name?: string;
  label?: string;
  description?: string;
  status?: string;
  recommendation?: string;
  reasons?: string[];
  confidence?: number | string | Record<string, unknown> | null;
  confidence_label?: string;
  manual_review?: boolean | string;
  manual_review_required?: boolean;
  immutable?: boolean;
  auto_apply?: boolean;
  factor_changes?: OptimizationFactorChange[] | Record<string, unknown>;
  parameter_changes?: OptimizationFactorChange[] | Record<string, unknown>;
  factors?: OptimizationFactorChange[] | Record<string, unknown>;
  production_params?: Record<string, unknown>;
  candidate_params?: Record<string, unknown>;
  comparisons?: OptimizationComparisonMetric[] | Record<string, unknown>;
  production?: Record<string, unknown>;
  candidate?: Record<string, unknown>;
  delta?: Record<string, unknown>;
  recommendations?: string[] | Array<Record<string, unknown>>;
  readiness?: OptimizationReadinessPayload | Record<string, unknown>;
  [key: string]: unknown;
};

export type OptimizationSummaryPayload = {
  optimization_version?: string;
  production_model?: string | Record<string, unknown>;
  production_model_version?: string;
  base_model?: string | Record<string, unknown>;
  model_version?: string;
  status?: string;
  status_label?: string;
  generated_at?: string;
  does_not_modify_model?: boolean;
  immutable?: boolean;
  auto_apply?: boolean;
  scenario_count?: number;
  recommended_scenario_count?: number;
  summary?: Record<string, unknown>;
  not_advice?: string;
  [key: string]: unknown;
};

export type OptimizationScenariosPayload = OptimizationSummaryPayload & {
  items?: OptimizationScenarioItem[];
  scenarios?: OptimizationScenarioItem[];
};

export type OptimizationReportPayload = OptimizationScenariosPayload & {
  report?: Record<string, unknown>;
  comparisons?: OptimizationScenarioItem[] | OptimizationComparisonMetric[] | Record<string, unknown>;
  runs?: OptimizationScenarioItem[];
  recommendations?: Array<Record<string, unknown>>;
  readiness?: OptimizationReadinessPayload | Record<string, unknown>;
};

export type OptimizationReadinessPayload = {
  ready?: boolean;
  label?: string;
  status?: string;
  passed?: string[];
  blocked?: string[];
  warnings?: string[];
  current?: Record<string, unknown>;
  required?: Record<string, unknown>;
  generated_at?: string;
  optimization_version?: string;
  production_model?: string | Record<string, unknown>;
  base_model?: string | Record<string, unknown>;
  does_not_modify_model?: boolean;
  immutable?: boolean;
  auto_apply?: boolean;
  not_advice?: string;
  [key: string]: unknown;
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
