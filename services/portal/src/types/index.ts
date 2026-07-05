export interface User {
  id: string
  email: string
  full_name: string | null
  role: string
  tenant_id: string
  tenant_name?: string
}

export interface ClaimCost {
  ocr_pages: number
  ocr_cost_usd: number
  ai_input_tokens: number
  ai_output_tokens: number
  ai_cost_usd: number
  infra_cost_usd: number
  total_cost_usd: number
}

export interface DashboardStats {
  total_claims: number
  ocr: { pages: number; cost_usd: number }
  ai_input: { tokens: number; cost_usd: number }
  ai_output: { tokens: number; cost_usd: number }
  infra: { cost_usd: number }
  total_cost_usd: number
}

export interface ClaimSummary {
  id: string
  policy_number: string | null
  status: string
  submission_date: string | null
  event_date: string | null
  total_claimed: number | null
  total_approved: number | null
  final_payout: number | null
  decision_type: string | null
  overall_confidence: number | null
  cost: ClaimCost
}

export interface ClaimsListResponse {
  claims: ClaimSummary[]
  total: number
  page: number
  per_page: number
  pages: number
}

export interface AuditStep {
  step: string
  timestamp: string | null
  duration_ms: number | null
  model_version: string | null
  input_tokens: number | null
  output_tokens: number | null
  ai_cost_usd: number | null
  ocr_pages: number | null
  ocr_cost_usd: number | null
  step_cost_usd: number | null
  confidence: number | null
}

export interface ClaimCostDetail {
  claim_id: string
  policy_number: string | null
  status: string
  submission_date: string | null
  total_claimed: number | null
  total_approved: number | null
  final_payout: number | null
  cost_summary: {
    ocr_total_pages: number
    ocr_cost_usd: number
    ai_input_tokens: number
    ai_output_tokens: number
    ai_cost_usd: number
    infra_cost_usd: number
    total_cost_usd: number
  }
  audit_steps: AuditStep[]
}
