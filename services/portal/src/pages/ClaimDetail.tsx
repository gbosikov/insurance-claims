import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { ClaimCostDetail, AuditStep } from '../types'

// ── Status config (duplicated from ClaimsList for self-containment) ─

const STATUS_LABEL: Record<string, string> = {
  AUTO_APPROVED:    'Auto Approved',
  MANUAL_REVIEW:    'Manual Review',
  FRAUD_FLAG:       'Fraud Detected',
  REJECTED:         'Rejected',
  PAID:             'Paid',
  DOCS_REQUESTED:   'Docs Requested',
  RECEIVED:         'Received',
  DECISION_PENDING: 'Decision Pending',
}

type StatusCfg = { bg: string; color: string; dot: string }

const STATUS_CFG: Record<string, StatusCfg> = {
  AUTO_APPROVED:  { bg: '#ecfdf5', color: '#065f46', dot: '#10b981' },
  MANUAL_REVIEW:  { bg: '#fffbeb', color: '#92400e', dot: '#f59e0b' },
  FRAUD_FLAG:     { bg: '#fff1f2', color: '#9f1239', dot: '#f43f5e' },
  REJECTED:       { bg: '#fef2f2', color: '#991b1b', dot: '#dc2626' },
  PAID:           { bg: '#eff6ff', color: '#1d4ed8', dot: '#3b82f6' },
  DOCS_REQUESTED: { bg: '#faf5ff', color: '#6b21a8', dot: '#a855f7' },
}

function StatusBadge({ status }: { status: string }) {
  const label = STATUS_LABEL[status] || status.replace(/_/g, ' ')
  const cfg: StatusCfg = STATUS_CFG[status] || { bg: '#f1f5f9', color: '#475569', dot: '#94a3b8' }
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      background: cfg.bg, color: cfg.color,
      padding: '3px 10px', borderRadius: 20,
      fontSize: 11.5, fontWeight: 600, whiteSpace: 'nowrap',
    }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: cfg.dot, flexShrink: 0 }} />
      {label}
    </span>
  )
}

// ── Step / confidence labels ────────────────────────────────────────

const STEP_LABELS: Record<string, string> = {
  intake:               'Claim Intake',
  download:             'Document Download',
  preprocessing:        'Pre-processing',
  ocr:                  'OCR Recognition',
  extraction:           'AI Data Extraction',
  rag_search:           'Contract Search (RAG)',
  core_fetch:           'Core System Query',
  core_submit:          'Core System Submit',
  decision:             'AI Decision',
  routing:              'Routing',
  decision_second_pass: 'AI Decision (2nd pass)',
}

const CONFIDENCE_LABELS: Record<string, string> = {
  ocr:                  'OCR quality',
  extraction:           'Extraction',
  decision:             'Decision',
  decision_second_pass: 'Decision',
}

// ── Format helpers ──────────────────────────────────────────────────

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) +
    ' · ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function fmtUSD(v: number | null) {
  if (v == null || v === 0) return '—'
  return `$${v.toFixed(6)}`
}

function fmtTokens(n: number | null) {
  if (n == null) return '0'
  return n.toLocaleString('en')
}

// ── Confidence pill ─────────────────────────────────────────────────

function ConfidencePill({ value }: { value: number }) {
  const pct = value * 100
  const [bg, color] = pct >= 85
    ? ['#ecfdf5', '#065f46']
    : pct >= 70
    ? ['#fffbeb', '#92400e']
    : ['#fef2f2', '#991b1b']
  return (
    <div style={{
      display: 'inline-block',
      background: bg,
      color,
      borderRadius: 5,
      padding: '2px 8px',
      fontWeight: 700,
      fontSize: 12,
      fontVariantNumeric: 'tabular-nums',
    }}>
      {pct.toFixed(1)}%
    </div>
  )
}

// ── Component ───────────────────────────────────────────────────────

export default function ClaimDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [data, setData]     = useState<ClaimCostDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]   = useState('')

  useEffect(() => {
    if (!id) return
    api.claimCost(id)
      .then(setData)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [id])

  const cs = data?.cost_summary

  const title = data?.policy_number
    ? `Policy ${data.policy_number}`
    : id ? `Claim ${id.slice(0, 8).toUpperCase()}` : 'Loading…'

  return (
    <>
      {/* ── Navigation ────────────────────────────────────────────── */}
      <nav style={{
        position: 'sticky', top: 0, zIndex: 100,
        background: '#0f172a',
        height: 56,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 28px',
        boxShadow: '0 1px 3px rgba(0,0,0,0.4)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <button
            onClick={() => navigate('/claims')}
            style={{
              background: '#1e293b',
              border: '1px solid #334155',
              color: '#94a3b8',
              padding: '5px 14px',
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >← Back</button>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 30, height: 30,
              background: 'linear-gradient(135deg, #2563eb, #4f46e5)',
              borderRadius: 7,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: '#fff', fontWeight: 800, fontSize: 12,
              flexShrink: 0,
            }}>IC</div>
            <span style={{ color: '#f1f5f9', fontWeight: 600, fontSize: 15 }}>{title}</span>
          </div>
        </div>

        {data && <StatusBadge status={data.status} />}
      </nav>

      <div style={{ maxWidth: 1000, margin: '0 auto', padding: '28px 24px' }}>

        {error && (
          <div style={{
            background: '#fef2f2', border: '1px solid #fecaca',
            borderRadius: 8, padding: '12px 16px', color: '#b91c1c', marginBottom: 20, fontSize: 13,
          }}>{error}</div>
        )}
        {loading && <p style={{ color: '#94a3b8', fontSize: 14 }}>Loading…</p>}

        {/* ── Overview ──────────────────────────────────────────────── */}
        {data && (
          <div style={{
            background: '#fff', borderRadius: 12, marginBottom: 20,
            boxShadow: '0 1px 3px rgba(0,0,0,0.07)', overflow: 'hidden',
          }}>
            <div style={{ padding: '16px 24px', borderBottom: '1px solid #f1f5f9' }}>
              <span style={{ fontWeight: 700, fontSize: 15, color: '#0f172a' }}>Overview</span>
            </div>
            <div style={{ padding: '20px 24px' }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
                {[
                  { label: 'Submitted',    value: fmtDate(data.submission_date), mono: false },
                  { label: 'Claimed (GEL)',  value: data.total_claimed  != null ? data.total_claimed.toFixed(2)  : '—', mono: true },
                  { label: 'Approved (GEL)', value: data.total_approved != null ? data.total_approved.toFixed(2) : '—', mono: true },
                  { label: 'Payout (GEL)',   value: data.final_payout  != null ? data.final_payout.toFixed(2)   : '—', mono: true, accent: '#059669' },
                ].map(({ label, value, mono, accent }) => (
                  <div key={label} style={{
                    background: '#f8fafc', borderRadius: 8, padding: '14px 18px',
                  }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
                      {label}
                    </div>
                    <div style={{
                      fontSize: 16, fontWeight: 700,
                      color: accent || '#0f172a',
                      fontVariantNumeric: mono ? 'tabular-nums' : undefined,
                    }}>
                      {value}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ── Cost summary ──────────────────────────────────────────── */}
        {cs && (
          <div style={{
            background: '#fff', borderRadius: 12, marginBottom: 20,
            boxShadow: '0 1px 3px rgba(0,0,0,0.07)', overflow: 'hidden',
          }}>
            <div style={{ padding: '16px 24px', borderBottom: '1px solid #f1f5f9' }}>
              <span style={{ fontWeight: 700, fontSize: 15, color: '#0f172a' }}>Cost Summary</span>
            </div>
            <div style={{ padding: '20px 24px' }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginBottom: 14 }}>
                {[
                  { label: 'OCR Pages',       value: String(cs.ocr_total_pages),                   accent: '#0ea5e9' },
                  { label: 'OCR Cost',         value: cs.ocr_cost_usd  ? `$${cs.ocr_cost_usd.toFixed(4)}`  : '—', accent: '#0ea5e9' },
                  { label: 'Infrastructure',   value: `$${cs.infra_cost_usd.toFixed(4)}`,           accent: '#f59e0b' },
                  { label: 'Input Tokens',     value: cs.ai_input_tokens.toLocaleString('en'),      accent: '#8b5cf6' },
                  { label: 'Output Tokens',    value: cs.ai_output_tokens.toLocaleString('en'),     accent: '#db2777' },
                  { label: 'AI Cost',          value: `$${cs.ai_cost_usd.toFixed(4)}`,              accent: '#4f46e5' },
                ].map(({ label, value, accent }) => (
                  <div key={label} style={{
                    background: '#f8fafc', borderRadius: 8, padding: '14px 18px',
                    borderLeft: `3px solid ${accent}`,
                  }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
                      {label}
                    </div>
                    <div style={{ fontSize: 18, fontWeight: 800, color: '#0f172a', fontVariantNumeric: 'tabular-nums' }}>
                      {value}
                    </div>
                  </div>
                ))}
              </div>

              {/* Total */}
              <div style={{
                background: 'linear-gradient(150deg, #1e293b, #0f172a)',
                borderRadius: 8, padding: '16px 22px',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              }}>
                <span style={{ color: '#64748b', fontWeight: 600, fontSize: 13 }}>Total processing cost</span>
                <span style={{ color: '#f8fafc', fontWeight: 800, fontSize: 22, fontVariantNumeric: 'tabular-nums' }}>
                  ${cs.total_cost_usd.toFixed(4)}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* ── Audit steps ───────────────────────────────────────────── */}
        {data && data.audit_steps.length > 0 && (
          <div style={{
            background: '#fff', borderRadius: 12,
            boxShadow: '0 1px 3px rgba(0,0,0,0.07)', overflow: 'hidden',
          }}>
            <div style={{ padding: '16px 24px', borderBottom: '1px solid #f1f5f9' }}>
              <span style={{ fontWeight: 700, fontSize: 15, color: '#0f172a' }}>Processing Steps</span>
            </div>

            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr>
                    {[
                      { label: 'Step',        align: 'left'  },
                      { label: 'Time',        align: 'left'  },
                      { label: 'ms',          align: 'right' },
                      { label: 'Model',       align: 'left'  },
                      { label: 'Input',       align: 'right' },
                      { label: 'Output',      align: 'right' },
                      { label: 'OCR p.',      align: 'right' },
                      { label: 'Cost',        align: 'right' },
                      { label: 'Confidence',  align: 'right' },
                    ].map(col => (
                      <th key={col.label} style={{
                        padding: '10px 14px',
                        background: '#f8fafc',
                        borderBottom: '2px solid #e2e8f0',
                        textAlign: col.align as 'left' | 'right',
                        fontWeight: 600, color: '#64748b',
                        fontSize: 11, whiteSpace: 'nowrap',
                        textTransform: 'uppercase', letterSpacing: 0.5,
                      }}>{col.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.audit_steps.map((step: AuditStep, i: number) => {
                    const hasModel      = !!step.model_version
                    const hasConfidence = step.confidence != null && CONFIDENCE_LABELS[step.step]
                    return (
                      <tr
                        key={i}
                        style={{ transition: 'background 0.1s' }}
                        onMouseEnter={e => (e.currentTarget.style.background = '#f8fafc')}
                        onMouseLeave={e => (e.currentTarget.style.background = '')}
                      >
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', fontWeight: 600, color: '#0f172a', whiteSpace: 'nowrap' }}>
                          {STEP_LABELS[step.step] || step.step}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', color: '#64748b', whiteSpace: 'nowrap', fontSize: 11.5 }}>
                          {fmtDate(step.timestamp)}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#94a3b8', fontVariantNumeric: 'tabular-nums' }}>
                          {step.duration_ms != null ? step.duration_ms.toLocaleString('en') : '—'}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9' }}>
                          {hasModel ? (
                            <span style={{
                              background: '#eff6ff', color: '#1d4ed8',
                              padding: '2px 7px', borderRadius: 4,
                              fontSize: 11, fontWeight: 600,
                            }}>{step.model_version}</span>
                          ) : (
                            <span style={{
                              background: '#f1f5f9', color: '#94a3b8',
                              padding: '2px 7px', borderRadius: 4,
                              fontSize: 11, fontWeight: 500, fontStyle: 'italic',
                            }}>L_computing</span>
                          )}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: hasModel ? '#8b5cf6' : '#cbd5e1', fontVariantNumeric: 'tabular-nums' }}>
                          {fmtTokens(step.input_tokens)}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: hasModel ? '#db2777' : '#cbd5e1', fontVariantNumeric: 'tabular-nums' }}>
                          {fmtTokens(step.output_tokens)}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#64748b' }}>
                          {step.ocr_pages != null ? step.ocr_pages : '—'}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#4f46e5', fontWeight: step.step_cost_usd ? 600 : 400, fontVariantNumeric: 'tabular-nums' }}>
                          {fmtUSD(step.step_cost_usd)}
                        </td>
                        <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right' }}>
                          {hasConfidence ? (
                            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
                              <ConfidencePill value={step.confidence!} />
                              <span style={{ fontSize: 10, color: '#94a3b8' }}>
                                {CONFIDENCE_LABELS[step.step]}
                              </span>
                            </div>
                          ) : (
                            <span style={{ color: '#e2e8f0' }}>—</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
