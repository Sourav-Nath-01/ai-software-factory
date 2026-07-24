export interface RunMetrics {
  files_generated: number;
  lines_of_code: number;
  review_iterations: number;
  test_fix_iterations: number;
  issues_found: number;
  issues_fixed: number;
  tests_passed: boolean;
  duration_seconds: number;
}

export interface CodeFile {
  file_path: string;
  content: string;
  language: string;
}

export type RunStatus = 'pending' | 'running' | 'complete' | 'failed';

export interface RunSummary {
  run_id: string;
  prompt: string;
  status: RunStatus;
  model: string;
  created_at: string;
  metrics?: RunMetrics;
}

export interface RunResult extends RunSummary {
  files: CodeFile[];
  error?: string;
}

// ── HITL Types ────────────────────────────────────────────────
// Defined before PipelineEvent so it can be referenced inline.

export interface HITLPlan {
  project_name: string;
  description: string;
  tech_stack: string[];
  file_structure: string[];
  modules: string[];
  endpoints: { method: string; path: string; description: string }[];
}

// ── Pipeline Event Types ──────────────────────────────────────

export type PipelineEventType =
  | 'pipeline_start'
  | 'stage_start'
  | 'stage_complete'
  | 'log'
  | 'complete'
  | 'error'
  | 'ping'
  | 'hitl_checkpoint'
  | 'hitl_approved'
  | 'cancelled';

export interface PipelineEvent {
  type: PipelineEventType;
  stage?: string;
  icon?: string;
  meta?: string;
  duration?: number;
  data?: Record<string, unknown>;
  message?: string;
  metrics?: RunMetrics;
  prompt?: string;
  plan?: HITLPlan;  // present on hitl_checkpoint events
}

export interface StageState {
  name: string;
  icon: string;
  status: 'pending' | 'running' | 'complete' | 'failed';
  duration?: number;
  message?: string;
  data?: Record<string, unknown>;
}

export interface AppStats {
  total_runs: number;
  successful_runs: number;
  success_rate: number;
  avg_files_generated: number;
}

// ── Diff Viewer Types ─────────────────────────────────────────

export interface FileDiff {
  file_path: string;
  original_content: string;
  improved_content: string;
  is_new: boolean;
  is_unchanged: boolean;
}

export interface DiffResponse {
  run_id: string;
  files_changed: number;
  files_added: number;
  files_unchanged: number;
  diffs: FileDiff[];
}
