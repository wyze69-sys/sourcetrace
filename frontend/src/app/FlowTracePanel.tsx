import { useEffect, useMemo, useState } from 'react'
import { ApiError, type ApiClient } from '../services/apiClient'
import type {
  FlowTraceResponse,
  TraceConfidence,
  TraceEdge,
  TraceNode,
} from '../services/types'

export interface FlowTracePanelProps {
  client: ApiClient
  repositoryId: string
  repositoryName: string
  explainAvailable?: boolean
}

const SAFE_TRACE_ERROR_MESSAGE = 'Flow trace failed safely. Try again.'

const CONFIDENCE_COLORS: Record<TraceConfidence, { bg: string; fg: string }> = {
  high: { bg: '#0c2b1b', fg: '#4ade80' },
  medium: { bg: '#2b230c', fg: '#facc15' },
  low: { bg: '#2b0f0c', fg: '#f87171' },
}

const EDGE_KIND_LABEL: Record<TraceEdge['kind'], string> = {
  call: 'call',
  import: 'import',
  http: 'HTTP',
}

interface StepRow {
  node: TraceNode
  depth: number
  incomingEdge: TraceEdge | null
}

function buildStepRows(result: FlowTraceResponse): StepRow[] {
  const nodesById = new Map(result.nodes.map((n) => [n.node_id, n]))
  // First edge reaching a node defines its place in the step tree; later
  // edges to the same node are shown as additional connections.
  const firstIncoming = new Map<string, TraceEdge>()
  for (const edge of result.edges) {
    if (!firstIncoming.has(edge.to_node_id)) {
      firstIncoming.set(edge.to_node_id, edge)
    }
  }

  const depths = new Map<string, number>()
  const rows: StepRow[] = []
  for (const nodeId of result.steps) {
    const node = nodesById.get(nodeId)
    if (!node) continue
    const incoming = firstIncoming.get(nodeId) ?? null
    const parentDepth = incoming ? (depths.get(incoming.from_node_id) ?? 0) : -1
    const depth = parentDepth + 1
    depths.set(nodeId, depth)
    rows.push({ node, depth, incomingEdge: incoming })
  }
  return rows
}

function extraConnections(result: FlowTraceResponse): TraceEdge[] {
  const seen = new Set<string>()
  const extras: TraceEdge[] = []
  for (const edge of result.edges) {
    if (seen.has(edge.to_node_id)) {
      extras.push(edge)
    } else {
      seen.add(edge.to_node_id)
    }
  }
  return extras
}

export function FlowTracePanel({
  client,
  repositoryId,
  repositoryName,
  explainAvailable = false,
}: FlowTracePanelProps) {
  const [entry, setEntry] = useState('')
  const [loading, setLoading] = useState(false)
  const [explaining, setExplaining] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<FlowTraceResponse | null>(null)
  const [openSnippets, setOpenSnippets] = useState<Record<string, boolean>>({})

  // A different repository means any previous trace is stale evidence.
  useEffect(() => {
    setResult(null)
    setError(null)
    setOpenSnippets({})
  }, [repositoryId])

  const rows = useMemo(() => (result ? buildStepRows(result) : []), [result])
  const extras = useMemo(() => (result ? extraConnections(result) : []), [result])
  const symbolByNodeId = useMemo(
    () => new Map((result?.nodes ?? []).map((n) => [n.node_id, n.symbol_name])),
    [result],
  )

  const handleTrace = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!entry.trim()) return

    setLoading(true)
    setError(null)
    try {
      const res = await client.traceFlow(repositoryId, entry.trim())
      setResult(res)
      setOpenSnippets({})
    } catch (err) {
      setResult(null)
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(SAFE_TRACE_ERROR_MESSAGE)
      }
    } finally {
      setLoading(false)
    }
  }

  const handleExplain = async () => {
    if (!result || result.steps.length === 0) return
    setExplaining(true)
    setError(null)
    try {
      const res = await client.traceFlow(
        repositoryId,
        result.entry.query,
        undefined,
        'explain',
      )
      setResult(res)
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(SAFE_TRACE_ERROR_MESSAGE)
      }
    } finally {
      setExplaining(false)
    }
  }

  const toggleSnippet = (nodeId: string) => {
    setOpenSnippets((prev) => ({ ...prev, [nodeId]: !prev[nodeId] }))
  }

  return (
    <section className="card-panel trace-panel">
      <h2 className="panel-header">
        Feature Flow Trace: <span className="mono">{repositoryName}</span>
      </h2>
      <p className="panel-text" style={{ marginBottom: '16px' }}>
        Trace an execution flow from a symbol through calls, imports, and HTTP boundaries.
        Every step is cited from indexed source; unresolved paths are reported as gaps.
      </p>

      <form
        onSubmit={handleTrace}
        className="trace-form"
        style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}
      >
        <input
          type="text"
          className="input-text chat-input"
          placeholder="Entry symbol to trace (e.g. Dashboard, create_repository)..."
          value={entry}
          onChange={(e) => setEntry(e.target.value)}
          disabled={loading}
          aria-label="Trace entry symbol"
        />
        <button type="submit" disabled={loading || !entry.trim()} className="btn-action">
          {loading ? 'Tracing...' : 'Trace Flow'}
        </button>
      </form>

      {error && (
        <div className="form-error" style={{ marginBottom: '16px' }}>
          {error}
        </div>
      )}

      {result !== null && result.entry.resolved_node_id === null && (
        <p className="panel-text" style={{ fontStyle: 'italic' }}>
          No indexed symbol matched "{result.entry.query}". Nothing was traced.
        </p>
      )}

      {result !== null && rows.length > 0 && (
        <div className="trace-steps">
          <h3 style={{ fontSize: '0.9rem', color: '#9ca3af', marginBottom: '12px' }}>
            Flow steps ({rows.length}):
          </h3>
          <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {rows.map(({ node, depth, incomingEdge }, index) => {
              const colors = incomingEdge ? CONFIDENCE_COLORS[incomingEdge.confidence] : null
              return (
                <li
                  key={node.node_id}
                  style={{
                    marginBottom: '10px',
                    marginLeft: `${Math.min(depth, 6) * 20}px`,
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
                    <span style={{ color: '#64748b' }} className="mono">
                      {index + 1}.
                    </span>
                    <span style={{ color: '#94a3b8' }}>{node.symbol_type}</span>
                    <strong>{node.symbol_name}</strong>
                    {incomingEdge && (
                      <>
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
                          via {EDGE_KIND_LABEL[incomingEdge.kind]}: {incomingEdge.evidence_label}{' '}
                          @L{incomingEdge.evidence_line_start}
                        </span>
                        {colors && (
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
                            {incomingEdge.confidence} confidence
                          </span>
                        )}
                        {incomingEdge.alternatives.length > 0 && (
                          <span style={{ color: '#f59e0b', fontSize: '0.72rem' }}>
                            {incomingEdge.alternatives.length} alternative candidate(s)
                          </span>
                        )}
                      </>
                    )}
                  </div>
                  <div style={{ marginTop: '6px' }}>
                    <button
                      type="button"
                      className="mono"
                      onClick={() => toggleSnippet(node.node_id)}
                      style={{
                        background: 'none',
                        border: 'none',
                        color: '#38bdf8',
                        cursor: 'pointer',
                        padding: 0,
                        fontSize: '0.82rem',
                        textDecoration: 'underline',
                      }}
                      aria-expanded={!!openSnippets[node.node_id]}
                    >
                      {node.relative_path}:{node.start_line}-{node.end_line}
                    </button>
                  </div>
                  {openSnippets[node.node_id] && (
                    <pre
                      className="evidence-block"
                      style={{
                        margin: '8px 0 0 0',
                        padding: '8px',
                        background: '#020617',
                        borderRadius: '4px',
                        overflowX: 'auto',
                        fontSize: '0.8rem',
                      }}
                    >
                      <code>{node.snippet}</code>
                    </pre>
                  )}
                </li>
              )
            })}
          </ol>

          {extras.length > 0 && (
            <div style={{ marginTop: '8px' }}>
              <h4 style={{ fontSize: '0.82rem', color: '#9ca3af', marginBottom: '6px' }}>
                Additional connections:
              </h4>
              <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '0.82rem', color: '#94a3b8' }}>
                {extras.map((edge, i) => (
                  <li key={`${edge.from_node_id}-${edge.to_node_id}-${i}`}>
                    <span className="mono">
                      {symbolByNodeId.get(edge.from_node_id) ?? edge.from_node_id} →{' '}
                      {symbolByNodeId.get(edge.to_node_id) ?? edge.to_node_id}
                    </span>{' '}
                    ({EDGE_KIND_LABEL[edge.kind]}, {edge.confidence})
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {result !== null && rows.length > 0 && explainAvailable && (
        <div style={{ marginTop: '14px' }}>
          {result.explanation === null && (
            <button
              type="button"
              className="btn-action"
              disabled={explaining}
              onClick={handleExplain}
            >
              {explaining ? 'Explaining...' : 'Explain this flow'}
            </button>
          )}
        </div>
      )}

      {result !== null && result.explanation !== null && (
        <div
          className="trace-explanation"
          style={{
            marginTop: '14px',
            border: '1px solid #1e3a5f',
            background: '#0b1a2b',
            borderRadius: '8px',
            padding: '12px',
          }}
        >
          <h3 style={{ fontSize: '0.9rem', color: '#38bdf8', margin: '0 0 8px 0' }}>
            Grounded explanation (cites steps{' '}
            {result.explanation.cited_steps.map((s) => `S${s}`).join(', ')}):
          </h3>
          <p
            className="panel-text"
            style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: '0.88rem' }}
          >
            {result.explanation.text}
          </p>
          <p style={{ margin: '8px 0 0 0', fontSize: '0.72rem', color: '#64748b' }}>
            This narration is restricted to the traced steps above; [S#] markers map to
            step numbers.
          </p>
        </div>
      )}

      {result !== null && result.gaps.length > 0 && (
        <div
          className="trace-gaps"
          style={{
            marginTop: '16px',
            border: '1px solid #78350f',
            background: '#1c1207',
            borderRadius: '8px',
            padding: '12px',
          }}
        >
          <h3 style={{ fontSize: '0.9rem', color: '#fbbf24', margin: '0 0 8px 0' }}>
            Gaps ({result.gaps.length}) — what this trace could not prove:
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

      {result !== null && result.gaps.length === 0 && rows.length > 0 && (
        <p className="panel-text" style={{ marginTop: '12px', fontSize: '0.82rem' }}>
          No gaps — every examined step resolved to indexed evidence.
        </p>
      )}
    </section>
  )
}

export default FlowTracePanel
