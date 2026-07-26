import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type ApiClient } from '../services/apiClient'
import type { ChangeImpactResponse } from '../services/types'
import { ImpactPanel } from './ImpactPanel'

function makeClient(previewImpact: ReturnType<typeof vi.fn>): ApiClient {
  return { previewImpact } as unknown as ApiClient
}

const FULL_IMPACT: ChangeImpactResponse = {
  repository_id: 'repo_1',
  target: {
    query: 'load_stats',
    resolved_node_id: 'c_target',
    candidates: ['c_target'],
  },
  upstream: [
    {
      node_id: 'c_handler',
      relative_path: 'backend/routes/stats.py',
      symbol_name: 'read_stats',
      symbol_type: 'function',
      start_line: 12,
      end_line: 30,
      distance: 1,
      confidence: 'high',
      edge_kind: 'call',
      via_node_id: 'c_target',
      evidence_node_id: 'c_handler',
      evidence_label: 'load_stats',
      evidence_line_start: 15,
      evidence_line_end: 15,
    },
    {
      node_id: 'c_client',
      relative_path: 'client/src/api.js',
      symbol_name: 'fetchStats',
      symbol_type: 'async_function',
      start_line: 9,
      end_line: 14,
      distance: 2,
      confidence: 'low',
      edge_kind: 'http',
      via_node_id: 'c_handler',
      evidence_node_id: 'c_client',
      evidence_label: 'GET /api/v1/stats',
      evidence_line_start: 11,
      evidence_line_end: 11,
    },
  ],
  downstream: [
    {
      node_id: 'c_store',
      relative_path: 'backend/services/store.py',
      symbol_name: 'query_rows',
      symbol_type: 'function',
      start_line: 4,
      end_line: 20,
      distance: 1,
      confidence: 'medium',
      edge_kind: 'call',
      via_node_id: 'c_target',
      evidence_node_id: 'c_target',
      evidence_label: 'query_rows',
      evidence_line_start: 44,
      evidence_line_end: 44,
    },
  ],
  affected_endpoints: [
    { http_method: 'GET', normalized_path: '/api/v1/stats', node_id: 'c_handler' },
  ],
  affected_components: [],
  affected_tests: ['c_test'],
  risk_level: 'medium',
  risk_factors: [
    {
      kind: 'endpoint_exposure',
      severity: 'medium',
      detail: '1 HTTP endpoint(s) transitively depend on this symbol.',
    },
  ],
  gaps: [
    {
      kind: 'unresolved_references',
      detail: '2 reference(s) to repo-internal modules did not resolve.',
      node_id: null,
    },
  ],
}

const UNRESOLVED_IMPACT: ChangeImpactResponse = {
  repository_id: 'repo_1',
  target: { query: 'ghost_symbol', resolved_node_id: null, candidates: [] },
  upstream: [],
  downstream: [],
  affected_endpoints: [],
  affected_components: [],
  affected_tests: [],
  risk_level: 'unknown',
  risk_factors: [],
  gaps: [
    {
      kind: 'entry_unresolved',
      detail: "No indexed symbol matched query 'ghost_symbol'.",
      node_id: null,
    },
  ],
}

const NO_IMPACT: ChangeImpactResponse = {
  repository_id: 'repo_1',
  target: { query: 'lonely', resolved_node_id: 'c_lonely', candidates: ['c_lonely'] },
  upstream: [],
  downstream: [],
  affected_endpoints: [],
  affected_components: [],
  affected_tests: [],
  risk_level: 'medium',
  risk_factors: [
    {
      kind: 'no_test_coverage',
      severity: 'medium',
      detail: 'No indexed test file references this symbol.',
    },
  ],
  gaps: [],
}

async function submitSymbol(text: string) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Impact target symbol'), text)
  await user.click(screen.getByRole('button', { name: /preview impact/i }))
  return user
}

describe('ImpactPanel', () => {
  it('submits the trimmed symbol to the impact API for the selected repository', async () => {
    const previewImpact = vi.fn().mockResolvedValue(NO_IMPACT)
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )

    await submitSymbol('  lonely  ')

    await waitFor(() => expect(previewImpact).toHaveBeenCalledTimes(1))
    expect(previewImpact).toHaveBeenCalledWith('repo_1', 'lonely')
  })

  it('renders risk level, factors, upstream and downstream items with distance and confidence', async () => {
    const previewImpact = vi.fn().mockResolvedValue(FULL_IMPACT)
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )

    await submitSymbol('load_stats')

    expect(await screen.findByText('medium')).toBeInTheDocument()
    expect(screen.getByText('endpoint_exposure')).toBeInTheDocument()

    expect(screen.getByText(/Upstream dependents \(2\)/)).toBeInTheDocument()
    expect(screen.getByText('read_stats')).toBeInTheDocument()
    expect(screen.getByText('fetchStats')).toBeInTheDocument()
    // "distance 1" appears on both the direct upstream dependent and the
    // direct downstream dependency.
    expect(screen.getAllByText('distance 1')).toHaveLength(2)
    expect(screen.getByText('distance 2')).toBeInTheDocument()
    expect(screen.getByText('high confidence')).toBeInTheDocument()
    expect(screen.getByText('low confidence')).toBeInTheDocument()

    expect(screen.getByText(/Downstream dependencies \(1\)/)).toBeInTheDocument()
    expect(screen.getByText('query_rows')).toBeInTheDocument()

    expect(screen.getByText('GET /api/v1/stats')).toBeInTheDocument()
    expect(screen.getByText(/1 indexed test file symbol/)).toBeInTheDocument()

    expect(screen.getByText('unresolved_references')).toBeInTheDocument()
  })

  it('expands a clickable citation into evidence details', async () => {
    const previewImpact = vi.fn().mockResolvedValue(FULL_IMPACT)
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )
    const user = await submitSymbol('load_stats')

    const citation = await screen.findByRole('button', {
      name: 'backend/routes/stats.py:12-30',
    })
    expect(citation).toHaveAttribute('aria-expanded', 'false')
    await user.click(citation)

    expect(citation).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText(/Cited evidence:/)).toBeInTheDocument()
    expect(screen.getByText('load_stats (target)')).toBeInTheDocument()
  })

  it('reports an unresolved symbol honestly without fabricating impact', async () => {
    const previewImpact = vi.fn().mockResolvedValue(UNRESOLVED_IMPACT)
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )

    await submitSymbol('ghost_symbol')

    expect(
      await screen.findByText(/No indexed symbol matched "ghost_symbol"/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Upstream dependents/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Change risk/)).not.toBeInTheDocument()
    expect(screen.getByText('entry_unresolved')).toBeInTheDocument()
  })

  it('shows the honest zero-impact state for an isolated symbol', async () => {
    const previewImpact = vi.fn().mockResolvedValue(NO_IMPACT)
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )

    await submitSymbol('lonely')

    expect(
      await screen.findByText(/No impact connections found in indexed evidence/),
    ).toBeInTheDocument()
    expect(screen.getByText('no_test_coverage')).toBeInTheDocument()
  })

  it('surfaces ApiError messages and falls back to a safe message otherwise', async () => {
    const previewImpact = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(
          'Repository is not ready for impact analysis (status: indexing).',
          'BAD_REQUEST',
          400,
        ),
      )
      .mockRejectedValueOnce(new TypeError('network down'))
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )
    const user = await submitSymbol('anything')

    expect(
      await screen.findByText(/Repository is not ready for impact analysis/),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /preview impact/i }))
    expect(
      await screen.findByText('Impact preview failed safely. Try again.'),
    ).toBeInTheDocument()
  })

  it('disables the submit button while a preview is loading', async () => {
    let resolveRequest: (value: ChangeImpactResponse) => void = () => {}
    const previewImpact = vi.fn().mockImplementation(
      () =>
        new Promise<ChangeImpactResponse>((resolve) => {
          resolveRequest = resolve
        }),
    )
    render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )

    await submitSymbol('load_stats')

    expect(screen.getByRole('button', { name: 'Previewing...' })).toBeDisabled()
    resolveRequest(NO_IMPACT)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /preview impact/i })).toBeEnabled(),
    )
  })

  it('clears a stale result when the selected repository changes', async () => {
    const previewImpact = vi.fn().mockResolvedValue(FULL_IMPACT)
    const { rerender } = render(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_1"
        repositoryName="demo-repo"
      />,
    )
    await submitSymbol('load_stats')
    expect(await screen.findByText(/Upstream dependents \(2\)/)).toBeInTheDocument()

    rerender(
      <ImpactPanel
        client={makeClient(previewImpact)}
        repositoryId="repo_2"
        repositoryName="other-repo"
      />,
    )

    expect(screen.queryByText(/Upstream dependents/)).not.toBeInTheDocument()
    expect(screen.queryByText('read_stats')).not.toBeInTheDocument()
  })
})
