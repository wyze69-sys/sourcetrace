import { useEffect, useMemo, useState } from 'react'
import { ApiError, type ApiClient } from '../services/apiClient'
import type {
  ChangeImpactResponse,
  ImpactItem,
  RiskLevel,
  RiskSeverity,
  TraceConfidence,
} from '../services/types'

export interface ImpactPanelProps {
  client: ApiClient
  repositoryId: string
  repositoryName: string
}

const SAFE_IMPACT_ERROR_MESSAGE = 'Impact preview failed safely. Try again.'

const CONFIDENCE_COLORS: Record<TraceConfidence, { bg: string; fg: string }> = {
  high: { bg: '#0c2b1b', fg: '#4ade80' },
  medium: { bg: '#2b230c', fg: '#facc15' },
  low: { bg: '#2b0f0c', fg: '#f87171' },
}

// For risk, low is good (green) and high is bad (red) — inverse of confidence.
const RISK_COLORS: Record<RiskLevel, { bg: string; fg: string }> = {
  low: { bg: '#0c2b1b', fg: '#4ade80' },
  medium: { bg: '#2b230c', fg: '#facc15' },
  high: { bg: '#2b0f0c', fg: '#f87171' },
  unknown: { bg: '#1f2937', fg: '#9ca3af' },
}

const SEVERITY_COLORS: Record<RiskSeverity, string> = {
  low: '#4ade80',
  medium: '#facc15',
  high: '#f87171',
}

function ImpactItemList({
  title,
  hint,
  items,
  symbolByNodeId,
  openCitations,
  onToggleCitation,
}: {
  title: string
  hint: string
  items: ImpactItem[]
  symbolByNodeId: Map<string, string>
  openCitations: Record<string, boolean>
  onToggleCitation: (key: string) => void
}) {
  if (items.length === 0) return null
  return (
    <div style={{ marginBottom: '16px' }}>
      <h3 style={{ fontSize: '0.9rem', color: '#9ca3af', marginBottom: '4px' }}>
        {title} ({items.length}):
      </h3>
      <p style={{ fontSize: '0.75rem', color: '#64748b', margin: '0 0 10px 0' }}>{hint}</p>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {items.map((item) => {
          const colors = CONFIDENCE_COLORS[item.confidence]
          const citationKey = `${title}-${item.node_id}`
          return (
            <li
              key={item.node_id}
              style={{
                marginBottom: '10px',
                marginLeft: `${Math.min(item.distance - 1, 6) * 20}px`,
                background: '#0f172a',
                border: '1px solid #1e293b',
                borderRadius: '8px',
                padding: '10px 12px',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: '8px',
                  alignItems: 'center',
                  fontSize: '0.85rem',
                }}
              >
                <span
                  className="mono"
                  style={{
                    background: '#111827',
                    border: '1px solid #374151',
                    color: '#9ca3af',
                    padding: '1px 8px',
                    borderRadius: '10px',
                    fontSize: '0.72rem',
                  }}
                >
                  distance {item.distance}
                </span>
                <span style={{ color: '#94a3b8' }}>{item.symbol_type}</span>
                <strong>{item.symbol_name}</strong>
                <span
                  style={{
                    background: colors.bg,
                    color: colors.fg,
                    padding: '1px 8px',
                    borderRadius: '10px',
                    fontSize: '0.72rem',
                    fontWeight: 600,
                  }}
                >
                  {item.confidence} confidence
                </span>
                <span style={{ color: '#64748b', fontSize: '0.72rem' }}>
                  via {item.edge_kind === 'http' ? 'HTTP' : 'call'}
                </span>
              </div>
              <div style={{ marginTop: '6px' }}>
                <button
                  type="button"
                  className="mono"
                  onClick={() => onToggleCitation(citationKey)}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    cursor: 'pointer',
                    padding: 0,
                    fontSize: '0.82rem',
                    textDecoration: 'underline',
                  }}
                  aria-expanded={!!openCitations[citationKey]}
                >
                  {item.relative_path}:{item.start_line}-{item.end_line}
                </button>
              </div>
              {openCitations[citationKey] && (
                <div
                  className="evidence-block"
                  style={{
                    margin: '8px 0 0 0',
                    padding: '8px',
                    background: '#020617',
                    borderRadius: '4px',
                    fontSize: '0.8rem',
                    color: '#94a3b8',
                  }}
                >
                  Cited evidence: <span className="mono">{item.evidence_label}</span> at line{' '}
                  {item.evidence_line_start}
                  {item.evidence_line_end !== item.evidence_line_start
                    ? `-${item.evidence_line_end}`
                    : ''}{' '}
                  in{' '}
                  <span className="mono">
                    {symbolByNodeId.get(item.evidence_node_id) ?? item.evidence_node_id}
                  </span>
                  {' — '}connected through{' '}
                  <span className="mono">
                    {symbolByNodeId.get(item.via_node_id) ?? item.via_node_id}
                  </span>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export function ImpactPanel({ client, repositoryId, repositoryName }: ImpactPanelProps) {
  const [symbol, setSymbol] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ChangeImpactResponse | null>(null)
  const [openCitations, setOpenCitations] = useState<Record<string, boolean>>({})

  // A different repository means any previous preview is stale evidence.
  useEffect(() => {
    setResult(null)
    setError(null)
    setOpenCitations({})
  }, [repositoryId])

  // Symbol lookup for via/evidence node ids: items in either direction plus
  // the resolved target itself (labeled with the query text).
  const symbolByNodeId = useMemo(() => {
    const map = new Map<string, string>()
    if (!result) return map
    for (const item of [...result.upstream, ...result.downstream]) {
      map.set(item.node_id, item.symbol_name)
    }
    if (result.target.resolved_node_id) {
      map.set(result.target.resolved_node_id, `${result.target.query} (target)`)
    }
    return map
  }, [result])

  const handlePreview = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!symbol.trim()) return

    setLoading(true)
    setError(null)
    try {
      const res = await client.previewImpact(repositoryId, symbol.trim())
      setResult(res)
      setOpenCitations({})
    } catch (err) {
      setResult(null)
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(SAFE_IMPACT_ERROR_MESSAGE)
      }
    } finally {
      setLoading(false)
    }
  }

  const toggleCitation = (key: string) => {
    setOpenCitations((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  const resolved = result !== null && result.target.resolved_node_id !== null
  const hasImpact =
    resolved && (result.upstream.length > 0 || result.downstream.length > 0)
  const riskColors = result ? RISK_COLORS[result.risk_level] : null

  return (
    <section className="card-panel impact-panel">
      <h2 className="panel-header">
        Change Impact Preview: <span className="mono">{repositoryName}</span>
      </h2>
      <p className="panel-text" style={{ marginBottom: '16px' }}>
        Preview what may break before changing a symbol: upstream dependents, downstream
        dependencies, affected endpoints and tests. Every item is cited from indexed
        source; unknowns are reported as gaps, never guessed.
      </p>

      <form
        onSubmit={handlePreview}
        className="impact-form"
        style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}
      >
        <input
          type="text"
          className="input-text chat-input"
          placeholder="Symbol to analyze (e.g. validate_owner_permissions)..."
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          disabled={loading}
          aria-label="Impact target symbol"
        />
        <button type="submit" disabled={loading || !symbol.trim()} className="btn-action">
          {loading ? 'Previewing...' : 'Preview Impact'}
        </button>
      </form>

      {error && (
        <div className="form-error" style={{ marginBottom: '16px' }}>
          {error}
        </div>
      )}

      {result !== null && !resolved && (
        <p className="panel-text" style={{ fontStyle: 'italic' }}>
          No indexed symbol matched "{result.target.query}". Nothing was analyzed.
        </p>
      )}

      {resolved && riskColors && (
        <div
          className="impact-risk"
          style={{
            marginBottom: '16px',
            border: '1px solid #1e293b',
            background: '#0f172a',
            borderRadius: '8px',
            padding: '12px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
            <h3 style={{ fontSize: '0.9rem', color: '#9ca3af', margin: 0 }}>Change risk:</h3>
            <span
              style={{
                background: riskColors.bg,
                color: riskColors.fg,
                padding: '2px 10px',
                borderRadius: '10px',
                fontSize: '0.8rem',
                fontWeight: 700,
                textTransform: 'uppercase',
              }}
            >
              {result.risk_level}
            </span>
          </div>
          {result.risk_factors.length > 0 ? (
            <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '0.82rem', color: '#d6d3d1' }}>
              {result.risk_factors.map((factor, i) => (
                <li key={`${factor.kind}-${i}`} style={{ marginBottom: '4px' }}>
                  <span className="mono" style={{ color: SEVERITY_COLORS[factor.severity] }}>
                    {factor.kind}
                  </span>
                  : {factor.detail}
                </li>
              ))}
            </ul>
          ) : (
            <p style={{ margin: 0, fontSize: '0.82rem', color: '#94a3b8' }}>
              No risk factors detected from indexed evidence.
            </p>
          )}
        </div>
      )}

      {resolved && result.affected_endpoints.length > 0 && (
        <div style={{ marginBottom: '16px' }}>
          <h3 style={{ fontSize: '0.9rem', color: '#9ca3af', marginBottom: '8px' }}>
            Affected HTTP endpoints ({result.affected_endpoints.length}):
          </h3>
          <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '0.82rem', color: '#d6d3d1' }}>
            {result.affected_endpoints.map((ep, i) => (
              <li key={`${ep.http_method}-${ep.normalized_path}-${i}`}>
                <span className="mono">
                  {ep.http_method} {ep.normalized_path}
                </span>{' '}
                <span style={{ color: '#64748b' }}>
                  (declared by {symbolByNodeId.get(ep.node_id) ?? ep.node_id})
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {resolved && (
        <>
          <ImpactItemList
            title="Upstream dependents"
            hint="Code that references this symbol — it may break if the symbol's contract changes."
            items={result.upstream}
            symbolByNodeId={symbolByNodeId}
            openCitations={openCitations}
            onToggleCitation={toggleCitation}
          />
          <ImpactItemList
            title="Downstream dependencies"
            hint="Code this symbol relies on — changes here shape what the symbol can safely assume."
            items={result.downstream}
            symbolByNodeId={symbolByNodeId}
            openCitations={openCitations}
            onToggleCitation={toggleCitation}
          />
        </>
      )}

      {resolved && result.affected_tests.length > 0 && (
        <p className="panel-text" style={{ fontSize: '0.82rem', marginBottom: '12px' }}>
          {result.affected_tests.length} indexed test file symbol(s) reference this target —
          run them after changing it.
        </p>
      )}

      {resolved && !hasImpact && (
        <p className="panel-text" style={{ fontStyle: 'italic', marginBottom: '12px' }}>
          No impact connections found in indexed evidence: nothing indexed references this
          symbol, and it references nothing indexed.
        </p>
      )}

      {result !== null && result.gaps.length > 0 && (
        <div
          className="impact-gaps"
          style={{
            marginTop: '16px',
            border: '1px solid #78350f',
            background: '#1c1207',
            borderRadius: '8px',
            padding: '12px',
          }}
        >
          <h3 style={{ fontSize: '0.9rem', color: '#fbbf24', margin: '0 0 8px 0' }}>
            Gaps ({result.gaps.length}) — what this preview could not prove:
          </h3>
          <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '0.82rem', color: '#d6d3d1' }}>
            {result.gaps.map((gap, i) => (
              <li key={`${gap.kind}-${i}`} style={{ marginBottom: '4px' }}>
                <span className="mono" style={{ color: '#fbbf24' }}>
                  {gap.kind}
                </span>
                : {gap.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      {resolved && result.gaps.length === 0 && hasImpact && (
        <p className="panel-text" style={{ marginTop: '12px', fontSize: '0.82rem' }}>
          No gaps — every examined connection resolved to indexed evidence.
        </p>
      )}
    </section>
  )
}

export default ImpactPanel
