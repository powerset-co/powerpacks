export interface SourceIdentifier {
  identifier: string;
  total_interactions: number;
  operator_name: string;
  source_account?: string | null;
}

export async function fetchSourceIdentifiers(_setId?: string, _personId?: string, _sourceChannel?: string): Promise<SourceIdentifier[]> {
  return [];
}
