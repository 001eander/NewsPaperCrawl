export interface Paths {
  data_dir: string;
  log_dir: string;
  secrets_dir: string;
}

export interface CoreState {
  version: string;
  operation: string | null;
  stage?: string | null;
}

export type Stage = 'import' | 'issues' | 'boards' | 'articles';

export interface RunParams {
  csv?: string;
  start_year?: number;
  end_year?: number;
  start_month?: number;
  end_month?: number;
  paperid?: string;
  limit?: number;
  concurrency?: number;
}

export type ConfigKind = 'credentials' | 'bark';
export type PathKind = 'data' | 'logs' | 'secrets';

export interface LoadedConfig {
  username: string;
  password: string;
  deviceKey: string;
  barkServer: string;
}

export interface OverviewRow {
  stage: string;
  exists: boolean;
  total: number;
  done: number;
  pending: number;
  failed: number;
}

export interface PerPaperRow {
  paperid: string;
  name: string;
  issues: string;
  boards: string;
  articles: string;
}

export interface FailedRow {
  table: string;
  paperid: string;
  date: string;
  error: string;
}
