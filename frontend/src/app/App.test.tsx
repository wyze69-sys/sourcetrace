import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from '../services/apiClient'
import App from './App'

describe('App Forensic Workspace Shell & Repository Import Workflow', () => {
  it('renders loading state initially while checking health', () => {
    const mockClient = {
      getHealth: () => new Promise(() => {}),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    expect(screen.getByRole('heading', { level: 1, name: /SourceTrace/i })).toBeInTheDocument()
    expect(screen.getByText('Evidence-grounded codebase intelligence')).toBeInTheDocument()
    expect(screen.getByText('Checking API status...')).toBeInTheDocument()
    expect(screen.getByText('Checking the API boundary')).toBeInTheDocument()
    expect(screen.getByText('Evidence is shown only when retrieved from source.')).toBeInTheDocument()
  })

  it('renders ready state from mocked health data with import forms enabled', async () => {
    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({
        status: 'ok',
        version: '1.0.0',
        timestamp: '2026-07-23T16:00:00Z',
      }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    expect(screen.getByText('API Boundary Reachable')).toBeInTheDocument()
    expect(
      screen.getByText('Import a public repository to begin evidence-grounded analysis.'),
    ).toBeInTheDocument()

    // Forms and buttons are present and not disabled
    const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })
    const zipBtn = screen.getByRole('button', { name: /Upload ZIP Archive/i })

    expect(githubBtn).not.toBeDisabled()
    expect(zipBtn).not.toBeDisabled()

    expect(screen.getByLabelText(/Public GitHub Repository URL/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/ZIP Archive File/i)).toBeInTheDocument()
  })

  it('rejects invalid GitHub URL locally without calling API', async () => {
    const createGitHubMock = vi.fn()
    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-24' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      createGitHubRepository: createGitHubMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const githubInput = screen.getByLabelText(/Public GitHub Repository URL/i)
    const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })

    // Try submitting empty URL
    await userEvent.click(githubBtn)
    expect(screen.getByText('GitHub URL is required.')).toBeInTheDocument()
    expect(createGitHubMock).not.toHaveBeenCalled()

    // Try submitting invalid URL host
    await userEvent.type(githubInput, 'https://gitlab.com/owner/repo')
    await userEvent.click(githubBtn)
    expect(screen.getByText('URL host must be github.com.')).toBeInTheDocument()
    expect(createGitHubMock).not.toHaveBeenCalled()
  })

  it('rejects invalid or non-ZIP file locally without calling API', async () => {
    const uploadZipMock = vi.fn()
    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-24' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      uploadZipRepository: uploadZipMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const zipBtn = screen.getByRole('button', { name: /Upload ZIP Archive/i })

    // Submit without file
    await userEvent.click(zipBtn)
    expect(screen.getByText('Please select a ZIP file to upload.')).toBeInTheDocument()
    expect(uploadZipMock).not.toHaveBeenCalled()

    // Submit invalid file extension via fireEvent to bypass jsdom accept filter
    const txtFile = new File(['text'], 'notes.txt', { type: 'text/plain' })
    const fileInput = screen.getByLabelText(/ZIP Archive File/i)
    fireEvent.change(fileInput, { target: { files: [txtFile] } })
    await userEvent.click(zipBtn)

    expect(screen.getByText('Uploaded file must have a .zip extension.')).toBeInTheDocument()
    expect(uploadZipMock).not.toHaveBeenCalled()
  })

  it('submits GitHub import request and polls job until ready', async () => {
    const mockRepo = {
      repository_id: 'repo_gh_1',
      name: 'Hello-World',
      source_type: 'github' as const,
      github_url: 'https://github.com/octocat/Hello-World',
      status: 'pending' as const,
      file_count: 0,
      chunk_count: 0,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }

    const mockJobQueued = {
      job_id: 'job_gh_1',
      repository_id: 'repo_gh_1',
      status: 'queued' as const,
      progress_percentage: 10,
      current_step: 'Queued',
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }

    const mockJobReady = {
      ...mockJobQueued,
      status: 'ready' as const,
      progress_percentage: 100,
      current_step: 'Done',
    }

    const createGitHubMock = vi.fn().mockResolvedValue({
      repository: mockRepo,
      indexing_job: mockJobQueued,
    })

    const getJobMock = vi.fn().mockResolvedValue(mockJobReady)

    const listRepoMock = vi
      .fn()
      .mockResolvedValueOnce({ repositories: [] })
      .mockResolvedValue({
        repositories: [{ ...mockRepo, status: 'ready', file_count: 5, chunk_count: 10 }],
      })

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-24' }),
      listRepositories: listRepoMock,
      createGitHubRepository: createGitHubMock,
      getIndexingJob: getJobMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const githubInput = screen.getByLabelText(/Public GitHub Repository URL/i)
    const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })

    await userEvent.type(githubInput, 'https://github.com/octocat/Hello-World')
    await userEvent.click(githubBtn)

    expect(createGitHubMock).toHaveBeenCalledWith(
      'https://github.com/octocat/Hello-World',
      'static',
    )

    await waitFor(
      () => {
        expect(getJobMock).toHaveBeenCalledWith('job_gh_1')
      },
      { timeout: 4000 },
    )
  })

  it('submits ZIP upload request and handles API errors safely', async () => {
    const uploadZipMock = vi
      .fn()
      .mockRejectedValue(new ApiError('Upload payload too large', 'PAYLOAD_TOO_LARGE', 413))

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-24' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      uploadZipRepository: uploadZipMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const zipFile = new File(['dummy zip content'], 'project.zip', { type: 'application/zip' })
    const fileInput = screen.getByLabelText(/ZIP Archive File/i)
    const zipBtn = screen.getByRole('button', { name: /Upload ZIP Archive/i })

    await userEvent.upload(fileInput, zipFile)
    await userEvent.click(zipBtn)

    await waitFor(() => {
      expect(screen.getByText('Upload payload too large')).toBeInTheDocument()
    })
  })

  it('renders ApiError message for contract errors and allows health retry', async () => {
    const getHealthMock = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError('Resource not found', 'RESOURCE_NOT_FOUND', 404, 'req_999'),
      )
      .mockResolvedValueOnce({
        status: 'ok',
        version: '1.0.0',
        timestamp: '2026-07-23T16:00:00Z',
      })

    const mockClient = {
      getHealth: getHealthMock,
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Offline')).toBeInTheDocument()
    })

    expect(screen.getByText('API Boundary Unavailable')).toBeInTheDocument()
    expect(screen.getByText('Resource not found')).toBeInTheDocument()

    const retryButton = screen.getByRole('button', { name: /Retry API Health Check/i })
    expect(retryButton).not.toBeDisabled()

    await userEvent.click(retryButton)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    expect(getHealthMock).toHaveBeenCalledTimes(2)
  })

  it('displays fixed safe message for unknown or network errors instead of raw Error.message', async () => {
    const rawNetworkError = new TypeError('Failed to fetch at http://internal-server-stack-trace')
    const mockClient = {
      getHealth: vi.fn().mockRejectedValue(rawNetworkError),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Offline')).toBeInTheDocument()
    })

    expect(
      screen.getByText(
        'Unable to reach the SourceTrace API. Check that the local service is running and try again.',
      ),
    ).toBeInTheDocument()

    expect(
      screen.queryByText('Failed to fetch at http://internal-server-stack-trace'),
    ).not.toBeInTheDocument()
  })

  it('renders safe failed error category and retry action when indexing fails', async () => {
    const mockFailedRepo = {
      repository_id: 'repo_failed_1',
      name: 'Private-Repo',
      source_type: 'github' as const,
      github_url: 'https://github.com/octocat/Private-Repo',
      status: 'failed' as const,
      file_count: 0,
      chunk_count: 0,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }

    const mockFailedJob = {
      job_id: 'job_failed_1',
      repository_id: 'repo_failed_1',
      status: 'failed' as const,
      progress_percentage: 0,
      current_step: 'Acquiring repository source',
      error_message: 'GitHub repository is private or unavailable.',
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
    }

    const createGitHubMock = vi.fn().mockResolvedValue({
      repository: mockFailedRepo,
      indexing_job: mockFailedJob,
    })

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-24' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      createGitHubRepository: createGitHubMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const githubInput = screen.getByLabelText(/Public GitHub Repository URL/i)
    const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })

    await userEvent.type(githubInput, 'https://github.com/octocat/Private-Repo')
    await userEvent.click(githubBtn)

    await waitFor(() => {
      expect(screen.getByText(/Indexing Failed:/i)).toBeInTheDocument()
      expect(screen.getByText('GitHub repository is private or unavailable.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Retry Indexing/i })).toBeInTheDocument()
    })

    const retryBtn = screen.getByRole('button', { name: /Retry Indexing/i })
    await userEvent.click(retryBtn)

    expect(createGitHubMock).toHaveBeenCalledTimes(2)
  })

  it('triggers GitHub repository refresh and tracks active refresh job', async () => {
    const mockReadyRepo = {
      repository_id: 'repo_ready_1',
      name: 'Active-Repo',
      source_type: 'github' as const,
      github_url: 'https://github.com/octocat/Active-Repo',
      status: 'ready' as const,
      file_count: 10,
      chunk_count: 25,
      index_mode: 'static' as const,
      created_at: '2026-07-24T00:00:00Z',
      updated_at: '2026-07-24T00:00:00Z',
      is_stale: true,
    }

    const mockRefreshJobQueued = {
      job_id: 'job_ref_1',
      repository_id: 'repo_ready_1',
      status: 'queued' as const,
      job_type: 'refresh' as const,
      progress_percentage: 0,
      current_step: 'Queued repository refresh',
      created_at: '2026-07-27T00:00:00Z',
      updated_at: '2026-07-27T00:00:00Z',
    }

    const refreshMock = vi.fn().mockResolvedValue({
      repository: mockReadyRepo,
      indexing_job: mockRefreshJobQueued,
    })

    const getJobMock = vi.fn().mockResolvedValue({
      ...mockRefreshJobQueued,
      status: 'ready' as const,
      progress_percentage: 100,
      current_step: 'Done',
    })

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-24' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [mockReadyRepo] }),
      refreshRepository: refreshMock,
      getIndexingJob: getJobMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
      expect(screen.getByText('Active-Repo')).toBeInTheDocument()
    })

    const repoCard = screen.getByRole('button', { name: /Active-Repo/i })
    await userEvent.click(repoCard)

    const refreshBtn = screen.getByRole('button', { name: /↻ Refresh/i })
    await userEvent.click(refreshBtn)

    expect(refreshMock).toHaveBeenCalledWith('repo_ready_1')

    await waitFor(
      () => {
        expect(getJobMock).toHaveBeenCalledWith('job_ref_1')
      },
      { timeout: 4000 },
    )
  })

  it('renders AI Assist (Free) option and guidance text when generation_available is true', async () => {
    const createGitHubMock = vi.fn().mockResolvedValue({
      repository: {
        repository_id: 'repo_ai_1',
        name: 'AI-Repo',
        source_type: 'github',
        status: 'pending',
        file_count: 0,
        chunk_count: 0,
        created_at: '2026-07-28T00:00:00Z',
        updated_at: '2026-07-28T00:00:00Z',
      },
      indexing_job: {
        job_id: 'job_ai_1',
        repository_id: 'repo_ai_1',
        status: 'queued',
        progress_percentage: 0,
        current_step: 'Queued',
        created_at: '2026-07-28T00:00:00Z',
        updated_at: '2026-07-28T00:00:00Z',
      },
    })

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
      getCapabilities: vi.fn().mockResolvedValue({
        allowed_index_modes: ['static'],
        default_index_mode: 'static',
        lexical_search_available: true,
        semantic_search_available: true,
        generation_available: true,
      }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      createGitHubRepository: createGitHubMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const select = screen.getByLabelText(/Indexing Mode/i) as HTMLSelectElement
    expect(select).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Static' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'AI Assist (Free)' })).toBeInTheDocument()
    expect(select.value).toBe('ai_assist')

    expect(
      screen.getByText(/Code is indexed with static analysis and AI is used afterward for "Explain this flow."/i),
    ).toBeInTheDocument()

    // Technical terms should not be present in the select options
    expect(screen.queryByText(/Cloud AI/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Embeddings \+ LLM/i)).not.toBeInTheDocument()

    const githubInput = screen.getByLabelText(/Public GitHub Repository URL/i)
    const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })

    await userEvent.type(githubInput, 'https://github.com/octocat/AI-Repo')
    await userEvent.click(githubBtn)

    expect(createGitHubMock).toHaveBeenCalledWith('https://github.com/octocat/AI-Repo', 'ai_assist')
  })

  it('renders Static option only with unavailable message when generation_available is false', async () => {
    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
      getCapabilities: vi.fn().mockResolvedValue({
        allowed_index_modes: ['static'],
        default_index_mode: 'static',
        lexical_search_available: true,
        semantic_search_available: false,
        generation_available: false,
      }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const select = screen.getByLabelText(/Indexing Mode/i) as HTMLSelectElement
    expect(select).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Static' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'AI Assist (Free)' })).not.toBeInTheDocument()
    expect(select.value).toBe('static')

    expect(
      screen.getByText('AI explanation assist unavailable (no LLM generation capability).'),
    ).toBeInTheDocument()
  })

  it('renders AI Assist (Static Evidence Mode) badge in chat panel when generation is available but semantic search is false', async () => {
    const mockReadyRepo = {
      repository_id: 'repo_static_chat_1',
      name: 'Static-Chat-Repo',
      source_type: 'github' as const,
      github_url: 'https://github.com/octocat/Static-Chat-Repo',
      status: 'ready' as const,
      file_count: 10,
      chunk_count: 25,
      index_mode: 'ai_assist' as const,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    }

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
      getCapabilities: vi.fn().mockResolvedValue({
        allowed_index_modes: ['static'],
        default_index_mode: 'static',
        lexical_search_available: true,
        semantic_search_available: false,
        generation_available: true,
      }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [mockReadyRepo] }),
      getConversation: vi.fn().mockResolvedValue(null),
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
      expect(screen.getByText('Static-Chat-Repo')).toBeInTheDocument()
    })

    const repoCard = screen.getByRole('button', { name: /Static-Chat-Repo/i })
    await userEvent.click(repoCard)

    await waitFor(() => {
      expect(screen.getByText('AI Assist (Static Evidence Mode)')).toBeInTheDocument()
    })
    expect(
      screen.getByText('Ask natural-language questions grounded in verified static code evidence.'),
    ).toBeInTheDocument()
  })

  it('deletes repository, removes it from list, and displays success banner', async () => {
    const mockFailedRepo = {
      repository_id: 'repo_del_test_1',
      name: 'Failed-Import-Repo',
      source_type: 'github' as const,
      github_url: 'https://github.com/octocat/Failed-Import-Repo',
      status: 'failed' as const,
      file_count: 0,
      chunk_count: 0,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    }

    const deleteMock = vi.fn().mockResolvedValue({
      message: 'Repository deleted successfully.',
      repository_id: 'repo_del_test_1',
    })

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [mockFailedRepo] }),
      deleteRepository: deleteMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
      expect(screen.getByText('Failed-Import-Repo')).toBeInTheDocument()
    })

    const repoCard = screen.getByRole('button', { name: /Failed-Import-Repo/i })
    await userEvent.click(repoCard)

    const deleteBtn = await screen.findByRole('button', { name: /Delete Repository/i })
    await userEvent.click(deleteBtn)

    expect(deleteMock).toHaveBeenCalledWith('repo_del_test_1')

    await waitFor(() => {
      expect(screen.getByText('Repository deleted successfully.')).toBeInTheDocument()
    })
  })

  it('maps HTTP 429 quota error during import to clear quota recovery message', async () => {
    const createGitHubMock = vi
      .fn()
      .mockRejectedValue(new ApiError('Repository quota exceeded', 'QUOTA_EXCEEDED', 429))

    const mockClient = {
      getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
      listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      createGitHubRepository: createGitHubMock,
    } as unknown as ApiClient

    render(<App client={mockClient} />)

    await waitFor(() => {
      expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
    })

    const githubInput = screen.getByLabelText(/Public GitHub Repository URL/i)
    const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })

    await userEvent.type(githubInput, 'https://github.com/octocat/Quota-Repo')
    await userEvent.click(githubBtn)

    await waitFor(() => {
      expect(
        screen.getByText(
          'Repository limit reached (3 max). Delete an existing repository before importing another.',
        ),
      ).toBeInTheDocument()
    })
  })
})
