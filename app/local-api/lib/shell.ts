import type { SetupJobStage } from "./types";

export function shellQuote(value: string): string {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

export function shellJoin(command: string[]): string {
  return command.map(shellQuote).join(" ");
}

export function shellStage(label: string, command: string[]): string {
  return `printf '%s\\n' ${shellQuote(`setup: ${label}`)} && ${shellJoin(command)}`;
}

export function setupStageSummaries(stages: Array<{ label: string; command: string[] }>): SetupJobStage[] {
  const total = stages.length;
  return stages.map((stage, index) => ({
    label: stage.label,
    index: index + 1,
    total,
  }));
}

export function stagedCommand(stages: Array<{ label: string; command: string[] }>): string[] {
  if (stages.length === 0) return [];
  if (stages.length === 1) return stages[0].command;
  const totalStages = stages.length;
  const stageSummaries = setupStageSummaries(stages);
  const script = [
    "set -o pipefail",
    ...stages.map((stage, index) => {
      const stageNumber = index + 1;
      const failurePayload = {
        status: "failed",
        failed_stage: stage.label,
        stage_index: stageNumber,
        total_stages: totalStages,
      };
      return [
        `printf '%s\\n' ${shellQuote(`setup: ${stage.label} (${stageNumber}/${totalStages})`)}`,
        shellJoin(stage.command),
        "code=$?",
        `if [ "$code" -ne 0 ]; then printf '%s\\n' ${shellQuote(JSON.stringify(failurePayload))}; exit "$code"; fi`,
      ].join("; ");
    }),
    `printf '%s\\n' ${shellQuote(JSON.stringify({ status: "completed", stages: stageSummaries }))}`,
  ].join("; ");
  return [
    "/bin/zsh",
    "-lc",
    script,
  ];
}
