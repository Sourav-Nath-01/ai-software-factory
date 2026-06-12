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

export type PipelineEventType =
  | 'pipeline_start'
  | 'stage_start'
  | 'stage_complete'
  | 'log'
  | 'complete'
  | 'error'
  | 'ping';

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
