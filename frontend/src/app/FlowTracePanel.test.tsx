import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type ApiClient } from '../services/apiClient'
import type { FlowTraceResponse } from '../services/types'
import { FlowTracePanel } from './FlowTracePanel'

function makeClient(traceFlow: ReturnType<typeof vi.fn>): ApiClient {
  return { traceFlow } as unknown as ApiClient
}

const FULL_TRACE: FlowTraceResponse = {
  repository_id: 'repo_1',
  entry: { query: 'Dashboard', resolved_node_id: 'c_component', candidates: ['c_component'] },
  nodes: [
    {
      node_id: 'c_component',
      relative_path: 'client/src/Dashboard.jsx',
      symbol_name: 'Dashboard',
      symbol_type: 'react_component',
      start_line: 5,
      end_line: 40,
      snippet: 'export function Dashboard() { ... }',
    },
    {
      node_id: 'c_client',
      relative_path: 'client/src/api.js',
      symbol_name: 'fetchStats',
      symbol_type: 'async_function',
      start_line: 9,
      end_line: 14,
      snippet: 'export async function fetchStats() { ... }',
    },
    {
      node_id: 'c_handler',
      relative_path: 'backend/routes/stats.py',
      symbol_name: 'read_stats',
      symbol_type: 'function',
      start_line: 12,
      end_line: 30,
      snippet: 'def read_stats(): ...',
    },
  ],
  edges: [
    {
      from_node_id: 'c_component',
      to_node_id: 'c_client',
      kind: 'call',
      confidence: 'high',
      evidence_label: 'fetchStats',
      evidence_line_start: 8,
      evidence_line_end: 8,
      alternatives: [],
    },
    {
      from_node_id: 'c_client',
      to_node_id: 'c_handler',
      kind: 'http',
      confidence: 'low',
      evidence_label: 'GET /api/v1/stats',
      evidence_line_start: 11,
      evidence_line_end: 11,
      alternatives: ['c_other_handler'],
    },
  ],
  steps: ['c_component', 'c_client', 'c_handler'],
  gaps: [
    {
      kind: 'endpoint_unmatched',
      detail: "No indexed handler declares POST '/api/v1/logs'.",
      node_id: 'c_client',
    },
  ],
  explanation: null,
}

const EMPTY_TRACE: FlowTraceResponse = {
  repository_id: 'repo_1',
  entry: { query: 'ghost', resolved_node_id: null, candidates: [] },
  nodes: [],
  edges: [],
  steps: [],
  gaps: [{ kind: 'entry_unresolved', detail: "No indexed symbol matched entry query 'ghost'.", node_id: null }],
  explanation: null,
}

async function runTrace(traceFlow: ReturnType<typeof vi.fn>, query: string) {
  const user = userEvent.setup()
  render(
    <FlowTracePanel client={makeClient(traceFlow)} repositoryId="repo_1" repositoryName="demo-repo" />,
  )
  await user.type(screen.getByLabelText('Trace entry symbol'), query)
  await user.click(screen.getByRole('button', { name: 'Trace Flow' }))
  return user
}

describe('FlowTracePanel', () => {
  it('renders the form with no fabricated pre-trace content', () => {
    render(
      <FlowTracePanel
        client={makeClient(vi.fn())}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )
    expect(screen.getByText(/Feature Flow Trace/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Trace Flow' })).toBeDisabled()
    expect(screen.queryByText(/Flow steps/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Gaps/)).not.toBeInTheDocument()
  })

  it('shows a loading state while the trace request is pending', async () => {
    let resolveTrace: (v: FlowTraceResponse) => void = () => {}
    const traceFlow = vi.fn().mockReturnValue(
      new Promise<FlowTraceResponse>((resolve) => {
        resolveTrace = resolve
      }),
    )
    await runTrace(traceFlow, 'Dashboard')

    expect(screen.getByRole('button', { name: 'Tracing...' })).toBeDisabled()
    resolveTrace(FULL_TRACE)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Trace Flow' })).toBeInTheDocument(),
    )
  })

  it('renders steps with edge kind, confidence, alternatives, and gaps', async () => {
    const traceFlow = vi.fn().mockResolvedValue(FULL_TRACE)
    await runTrace(traceFlow, 'Dashboard')

    await waitFor(() => expect(screen.getByText('Flow steps (3):')).toBeInTheDocument())
    expect(traceFlow).toHaveBeenCalledWith('repo_1', 'Dashboard')

    expect(screen.getByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('fetchStats')).toBeInTheDocument()
    expect(screen.getByText('read_stats')).toBeInTheDocument()

    expect(screen.getByText(/via call: fetchStats @L8/)).toBeInTheDocument()
    expect(screen.getByText(/via HTTP: GET \/api\/v1\/stats @L11/)).toBeInTheDocument()
    expect(screen.getByText('high confidence')).toBeInTheDocument()
    expect(screen.getByText('low confidence')).toBeInTheDocument()
    expect(screen.getByText('1 alternative candidate(s)')).toBeInTheDocument()

    expect(screen.getByText(/Gaps \(1\)/)).toBeInTheDocument()
    expect(screen.getByText('endpoint_unmatched')).toBeInTheDocument()
  })

  it('citation click reveals the cited source snippet', async () => {
    const traceFlow = vi.fn().mockResolvedValue(FULL_TRACE)
    const user = await runTrace(traceFlow, 'Dashboard')

    const citation = await screen.findByRole('button', {
      name: 'client/src/Dashboard.jsx:5-40',
    })
    expect(citation).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('export function Dashboard() { ... }')).not.toBeInTheDocument()

    await user.click(citation)
    expect(citation).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('export function Dashboard() { ... }')).toBeInTheDocument()
  })

  it('renders an honest empty state when the entry is unresolved', async () => {
    const traceFlow = vi.fn().mockResolvedValue(EMPTY_TRACE)
    await runTrace(traceFlow, 'ghost')

    await waitFor(() =>
      expect(
        screen.getByText('No indexed symbol matched "ghost". Nothing was traced.'),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByText(/Flow steps/)).not.toBeInTheDocument()
    expect(screen.getByText('entry_unresolved')).toBeInTheDocument()
  })

  it('shows the API error message when the trace fails', async () => {
    const traceFlow = vi.fn().mockRejectedValue(
      new ApiError('Repository not found.', 'HTTP_ERROR', 404),
    )
    await runTrace(traceFlow, 'main')

    await waitFor(() => expect(screen.getByText('Repository not found.')).toBeInTheDocument())
    expect(screen.queryByText(/Flow steps/)).not.toBeInTheDocument()
  })

  it('falls back to a safe message for non-API errors', async () => {
    const traceFlow = vi.fn().mockRejectedValue(new TypeError('network down'))
    await runTrace(traceFlow, 'main')

    await waitFor(() =>
      expect(screen.getByText('Flow trace failed safely. Try again.')).toBeInTheDocument(),
    )
  })

  it('hides the explain control when generation is unavailable', async () => {
    const traceFlow = vi.fn().mockResolvedValue(FULL_TRACE)
    await runTrace(traceFlow, 'Dashboard')

    await screen.findByText('Flow steps (3):')
    expect(screen.queryByRole('button', { name: 'Explain this flow' })).not.toBeInTheDocument()
  })

  it('explain control re-traces in explain mode and renders the grounded explanation', async () => {
    const explained: FlowTraceResponse = {
      ...FULL_TRACE,
      explanation: {
        text: 'The component [S1] calls the client [S2], which hits the handler [S3].',
        cited_steps: [1, 2, 3],
      },
    }
    const traceFlow = vi
      .fn()
      .mockResolvedValueOnce(FULL_TRACE)
      .mockResolvedValueOnce(explained)

    const user = userEvent.setup()
    render(
      <FlowTracePanel
        client={makeClient(traceFlow)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
        explainAvailable
      />,
    )
    await user.type(screen.getByLabelText('Trace entry symbol'), 'Dashboard')
    await user.click(screen.getByRole('button', { name: 'Trace Flow' }))

    const explainButton = await screen.findByRole('button', { name: 'Explain this flow' })
    await user.click(explainButton)

    await waitFor(() =>
      expect(
        screen.getByText(
          'The component [S1] calls the client [S2], which hits the handler [S3].',
        ),
      ).toBeInTheDocument(),
    )
    expect(screen.getByText(/cites steps S1, S2, S3/)).toBeInTheDocument()
    expect(traceFlow).toHaveBeenNthCalledWith(2, 'repo_1', 'Dashboard', undefined, 'explain')
    // Control disappears once an explanation is shown.
    expect(screen.queryByRole('button', { name: 'Explain this flow' })).not.toBeInTheDocument()
  })

  it('failed explanation keeps the static trace and surfaces the explanation_failed gap', async () => {
    const failed: FlowTraceResponse = {
      ...FULL_TRACE,
      explanation: null,
      gaps: [
        ...FULL_TRACE.gaps,
        {
          kind: 'explanation_failed',
          detail: 'The explanation was discarded; the static trace below is unaffected.',
          node_id: null,
        },
      ],
    }
    const traceFlow = vi
      .fn()
      .mockResolvedValueOnce(FULL_TRACE)
      .mockResolvedValueOnce(failed)

    const user = userEvent.setup()
    render(
      <FlowTracePanel
        client={makeClient(traceFlow)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
        explainAvailable
      />,
    )
    await user.type(screen.getByLabelText('Trace entry symbol'), 'Dashboard')
    await user.click(screen.getByRole('button', { name: 'Trace Flow' }))
    await user.click(await screen.findByRole('button', { name: 'Explain this flow' }))

    await waitFor(() => expect(screen.getByText('explanation_failed')).toBeInTheDocument())
    // Static trace untouched; explain remains offered for retry.
    expect(screen.getByText('Flow steps (3):')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Explain this flow' })).toBeInTheDocument()
  })
})
