import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, clearToken } from '../api/client'
import type { ClaimSummary, ClaimsListResponse, DashboardStats } from '../types'

// ── Status config ───────────────────────────────────────────────────

const STATUS_LABEL: Record<string, string> = {
  AUTO_APPROVED:      'Auto Approved',
  MANUAL_REVIEW:      'Manual Review',
  FRAUD_FLAG:         'Fraud Detected',
  REJECTED:           'Rejected',
  PAID:               'Paid',
  DOCS_REQUESTED:     'Docs Requested',
  RECEIVED:           'Received',
  PREPROCESSING:      'Pre-processing',
  OCR_PROCESSING:     'OCR Processing',
  EXTRACTING:         'Extracting',
  IDENTITY_CHECK:     'Identity Check',
  RAG_SEARCH:         'Searching',
  DECISION_PENDING:   'Decision Pending',
  SUBMITTING_TO_CORE: 'Submitting',
}

type StatusCfg = { bg: string; color: string; dot: string }

const STATUS_CFG: Record<string, StatusCfg> = {
  AUTO_APPROVED:    { bg: '#ecfdf5', color: '#065f46', dot: '#10b981' },
  MANUAL_REVIEW:    { bg: '#fffbeb', color: '#92400e', dot: '#f59e0b' },
  FRAUD_FLAG:       { bg: '#fff1f2', color: '#9f1239', dot: '#f43f5e' },
  REJECTED:         { bg: '#fef2f2', color: '#991b1b', dot: '#dc2626' },
  PAID:             { bg: '#eff6ff', color: '#1d4ed8', dot: '#3b82f6' },
  DOCS_REQUESTED:   { bg: '#faf5ff', color: '#6b21a8', dot: '#a855f7' },
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

// ── Format helpers ──────────────────────────────────────────────────

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) +
    ' · ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

function fmtUSD(v: number, dec = 4) {
  return v === 0 ? '—' : `$${v.toFixed(dec)}`
}

function fmtK(n: number) {
  if (n === 0) return '—'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return n.toLocaleString('en')
}

// ── Table header ────────────────────────────────────────────────────

const COLUMNS = [
  { label: 'Date',       align: 'left'  },
  { label: 'Policy',     align: 'left'  },
  { label: 'Status',     align: 'left'  },
  { label: 'Claimed',    align: 'right' },
  { label: 'Approved',   align: 'right' },
  { label: 'OCR p.',     align: 'right' },
  { label: 'OCR cost',   align: 'right' },
  { label: 'Input',      align: 'right' },
  { label: 'Output',     align: 'right' },
  { label: 'AI cost',    align: 'right' },
  { label: 'Total',      align: 'right' },
] as const

// ── Component ───────────────────────────────────────────────────────

export default function ClaimsList() {
  const navigate = useNavigate()
  const [data, setData]       = useState<ClaimsListResponse | null>(null)
  const [stats, setStats]     = useState<DashboardStats | null>(null)
  const [page, setPage]       = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')

  useEffect(() => {
    api.stats().then(setStats).catch(() => null)
  }, [])

  useEffect(() => {
    setLoading(true)
    setError('')
    api.claims(page, 20)
      .then(setData)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [page])

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
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{
            width: 30, height: 30,
            background: 'linear-gradient(135deg, #2563eb, #4f46e5)',
            borderRadius: 7,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#fff', fontWeight: 800, fontSize: 12, letterSpacing: 0.5,
            flexShrink: 0,
          }}>IC</div>
          <span style={{ color: '#f1f5f9', fontWeight: 600, fontSize: 15, letterSpacing: -0.2 }}>
            Claims Portal
          </span>
          <span style={{
            background: '#1e293b',
            color: '#64748b',
            padding: '2px 9px',
            borderRadius: 4,
            fontSize: 11,
            fontWeight: 500,
          }}>Unison Insurance</span>
        </div>

        <button
          onClick={() => { clearToken(); navigate('/login', { replace: true }) }}
          style={{
            background: 'transparent',
            border: '1px solid #334155',
            color: '#94a3b8',
            padding: '5px 14px',
            borderRadius: 6,
            fontSize: 12,
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >Sign out</button>
      </nav>

      <div style={{ maxWidth: 1400, margin: '0 auto', padding: '28px 24px' }}>

        {error && (
          <div style={{
            background: '#fef2f2', border: '1px solid #fecaca',
            borderRadius: 8, padding: '12px 16px',
            color: '#b91c1c', fontSize: 13, marginBottom: 20,
          }}>{error}</div>
        )}

        {/* ── Stats row ──────────────────────────────────────────── */}
        {stats && (
          <div style={{ display: 'flex', gap: 14, marginBottom: 24, flexWrap: 'wrap' }}>
            {([
              {
                label: 'OCR PAGES',
                value: stats.ocr.pages === 0 ? '—' : stats.ocr.pages.toLocaleString('en'),
                cost: fmtUSD(stats.ocr.cost_usd, 4),
                rate: '$0.006 / page',
                accent: '#0ea5e9',
              },
              {
                label: 'INPUT TOKENS',
                value: fmtK(stats.ai_input.tokens),
                cost: fmtUSD(stats.ai_input.cost_usd, 4),
                rate: '$6.00 / 1M',
                accent: '#8b5cf6',
              },
              {
                label: 'OUTPUT TOKENS',
                value: fmtK(stats.ai_output.tokens),
                cost: fmtUSD(stats.ai_output.cost_usd, 4),
                rate: '$36.00 / 1M',
                accent: '#db2777',
              },
              {
                label: 'INFRASTRUCTURE',
                value: `${stats.total_claims} claims`,
                cost: fmtUSD(stats.infra.cost_usd, 4),
                rate: '$0.005 / claim',
                accent: '#f59e0b',
              },
            ] as const).map(card => (
              <div key={card.label} style={{
                flex: '1 1 180px',
                background: '#fff',
                borderRadius: 10,
                padding: '18px 20px',
                boxShadow: '0 1px 3px rgba(0,0,0,0.07)',
                borderLeft: `3px solid ${card.accent}`,
              }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: '#94a3b8', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 8 }}>
                  {card.label}
                </div>
                <div style={{ fontSize: 26, fontWeight: 800, color: '#0f172a', lineHeight: 1, marginBottom: 6 }}>
                  {card.value}
                </div>
                <div style={{ fontSize: 12, fontWeight: 600, color: card.accent }}>
                  {card.cost}
                </div>
                <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>
                  {card.rate}
                </div>
              </div>
            ))}

            {/* Total card */}
            <div style={{
              flex: '0 0 auto', minWidth: 190,
              background: 'linear-gradient(150deg, #1e293b, #0f172a)',
              borderRadius: 10,
              padding: '18px 24px',
              boxShadow: '0 4px 20px rgba(15,23,42,0.3)',
              display: 'flex', flexDirection: 'column', justifyContent: 'center',
            }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: '#475569', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 8 }}>
                TOTAL COST
              </div>
              <div style={{ fontSize: 30, fontWeight: 800, color: '#f8fafc', lineHeight: 1, marginBottom: 4 }}>
                ${stats.total_cost_usd.toFixed(4)}
              </div>
              <div style={{ fontSize: 12, color: '#475569' }}>
                across {stats.total_claims} {stats.total_claims === 1 ? 'claim' : 'claims'}
              </div>
            </div>
          </div>
        )}

        {/* ── Claims table ───────────────────────────────────────── */}
        <div style={{
          background: '#fff',
          borderRadius: 12,
          boxShadow: '0 1px 3px rgba(0,0,0,0.07)',
          overflow: 'hidden',
        }}>
          <div style={{
            padding: '18px 24px',
            borderBottom: '1px solid #f1f5f9',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <div>
              <span style={{ fontWeight: 700, fontSize: 16, color: '#0f172a' }}>Claims</span>
              {data && (
                <span style={{ marginLeft: 8, fontSize: 13, color: '#94a3b8', fontWeight: 500 }}>
                  {data.total} total
                </span>
              )}
            </div>
            {loading && (
              <span style={{ fontSize: 12, color: '#94a3b8' }}>Loading…</span>
            )}
          </div>

          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr>
                  {COLUMNS.map(col => (
                    <th key={col.label} style={{
                      padding: '10px 14px',
                      background: '#f8fafc',
                      borderBottom: '2px solid #e2e8f0',
                      textAlign: col.align,
                      fontWeight: 600,
                      color: '#64748b',
                      fontSize: 11,
                      whiteSpace: 'nowrap',
                      textTransform: 'uppercase',
                      letterSpacing: 0.5,
                    }}>{col.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data?.claims.map((c: ClaimSummary) => (
                  <tr
                    key={c.id}
                    style={{ cursor: 'pointer', transition: 'background 0.1s' }}
                    onClick={() => navigate(`/claims/${c.id}`)}
                    onMouseEnter={e => (e.currentTarget.style.background = '#f8fafc')}
                    onMouseLeave={e => (e.currentTarget.style.background = '')}
                  >
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', color: '#64748b', whiteSpace: 'nowrap', fontSize: 12 }}>
                      {fmtDate(c.submission_date)}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', fontWeight: 600, color: '#0f172a' }}>
                      {c.policy_number || <span style={{ color: '#cbd5e1' }}>—</span>}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9' }}>
                      <StatusBadge status={c.status} />
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: '#374151' }}>
                      {c.total_claimed != null ? c.total_claimed.toFixed(2) : '—'}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 600, color: '#059669' }}>
                      {c.final_payout != null ? c.final_payout.toFixed(2) : '—'}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#64748b' }}>
                      {c.cost.ocr_pages === 0 ? '—' : c.cost.ocr_pages}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#0ea5e9', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtUSD(c.cost.ocr_cost_usd)}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#8b5cf6', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtK(c.cost.ai_input_tokens)}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#db2777', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtK(c.cost.ai_output_tokens)}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', color: '#64748b', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtUSD(c.cost.ai_cost_usd)}
                    </td>
                    <td style={{ padding: '11px 14px', borderBottom: '1px solid #f1f5f9', textAlign: 'right', fontWeight: 700, color: '#2563eb', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtUSD(c.cost.total_cost_usd)}
                    </td>
                  </tr>
                ))}

                {!loading && data?.claims.length === 0 && (
                  <tr>
                    <td colSpan={COLUMNS.length} style={{ padding: '56px 24px', textAlign: 'center', color: '#94a3b8', fontSize: 14 }}>
                      No claims found
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {data && data.pages > 1 && (
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '14px 24px',
              borderTop: '1px solid #f1f5f9',
              fontSize: 13, color: '#64748b',
            }}>
              <span>Page {page} of {data.pages}</span>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  disabled={page <= 1}
                  onClick={() => setPage(p => p - 1)}
                  style={{
                    padding: '6px 16px',
                    border: '1px solid #e2e8f0',
                    borderRadius: 6,
                    background: page <= 1 ? '#f8fafc' : '#fff',
                    cursor: page <= 1 ? 'not-allowed' : 'pointer',
                    fontSize: 13,
                    color: page <= 1 ? '#cbd5e1' : '#374151',
                    fontWeight: 500,
                  }}
                >← Previous</button>
                <button
                  disabled={page >= data.pages}
                  onClick={() => setPage(p => p + 1)}
                  style={{
                    padding: '6px 16px',
                    border: '1px solid #e2e8f0',
                    borderRadius: 6,
                    background: page >= data.pages ? '#f8fafc' : '#fff',
                    cursor: page >= data.pages ? 'not-allowed' : 'pointer',
                    fontSize: 13,
                    color: page >= data.pages ? '#cbd5e1' : '#374151',
                    fontWeight: 500,
                  }}
                >Next →</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
