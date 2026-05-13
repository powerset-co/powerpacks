export interface GmailAccountDetail {
  email: string;
  interactions: number;
}

export interface OperatorInfo {
  operator_id: string;
  operator_name: string;
  source_channels: string[];
  gmail_accounts?: string[];
  gmail_interactions?: number;
  gmail_account_details?: GmailAccountDetail[];
}
