export type RunState = Record<string, any>;
export type SetupJobStage = { label: string; index: number; total: number };
export type SetupJob = {
  id: string;
  action: string;
  actionKey?: string;
  source?: string;
  stages?: SetupJobStage[];
  status: "running" | "completed" | "failed" | "blocked";
  startedAt: string;
  completedAt?: string | null;
  command: string[];
  code?: number | null;
  stdout?: string;
  stderr?: string;
  log?: string;
  output?: Record<string, any> | null;
};
export type SetupOperator = { id: string; email?: string; label: string };
