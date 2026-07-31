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
    expect(screen.getByText('Understand a codebase before you change it.')).toBeInTheDocument()
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

    expect(screen.getByText('Import a Repository to Begin')).toBeInTheDocument()
    expect(
      screen.getByText('Import a public GitHub repository URL or ZIP archive to begin evidence-grounded analysis.'),
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
      screen.getByText(/AI Assist uses semantic embeddings during indexing and grounded generation when you ask questions\./i),
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

  describe('RUNTIME-UX-005 — Question-First Workspace & Navigation Requirements', () => {
    const readyRepo1 = {
      repository_id: 'repo_ux_1',
      name: 'Repo-One',
      source_type: 'github' as const,
      status: 'ready' as const,
      file_count: 5,
      chunk_count: 10,
      index_mode: 'static' as const,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    }

    const readyRepo2 = {
      repository_id: 'repo_ux_2',
      name: 'Repo-Two',
      source_type: 'github' as const,
      status: 'ready' as const,
      file_count: 12,
      chunk_count: 30,
      index_mode: 'static' as const,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    }

    it('Understand is the default workspace after selecting a ready repository', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
      })

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      expect(screen.getByRole('button', { name: 'Understand' })).toHaveAttribute('aria-current', 'page')
      expect(screen.getByText('Try a starter question:')).toBeInTheDocument()
    })

    it('Changing to another ready repository resets the active workspace to Understand', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1, readyRepo2] }),
        listRepositoryFiles: vi.fn().mockResolvedValue({ repository_id: 'repo_ux_1', files: [] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      // Switch to Files section
      await userEvent.click(screen.getByRole('button', { name: 'Files' }))
      expect(screen.getByRole('button', { name: 'Files' })).toHaveAttribute('aria-current', 'page')

      // Select second repository from sidebar
      const repo2Card = screen.getByRole('button', { name: /Repo-Two/i })
      await userEvent.click(repo2Card)

      // Workspace resets to Understand for Repo-Two
      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-Two?',
          }),
        ).toBeInTheDocument()
      })
      expect(screen.getByRole('button', { name: 'Understand' })).toHaveAttribute('aria-current', 'page')
    })

    it('Navigation switches correctly among Understand, Files, Find code, Flow Trace, and Change Impact', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
        listRepositoryFiles: vi.fn().mockResolvedValue({ repository_id: 'repo_ux_1', files: [] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      // Files
      await userEvent.click(screen.getByRole('button', { name: 'Files' }))
      expect(screen.getByRole('button', { name: 'Files' })).toHaveAttribute('aria-current', 'page')
      expect(screen.getByRole('heading', { level: 2, name: /Repository Explorer/i })).toBeInTheDocument()

      // Find code
      const findCodeNavBtn = screen.getAllByRole('button', { name: 'Find code' })[0]
      await userEvent.click(findCodeNavBtn)
      expect(findCodeNavBtn).toHaveAttribute('aria-current', 'page')
      expect(screen.getByRole('heading', { level: 2, name: /Find code in Repo-One/i })).toBeInTheDocument()

      // Expand Advanced analysis
      const advHeader = screen.getByRole('button', { name: /Advanced analysis/i })
      await userEvent.click(advHeader)

      // Show how this works -> Flow Trace
      await userEvent.click(screen.getByRole('button', { name: 'Show how this works' }))
      expect(screen.getByRole('button', { name: 'Show how this works' })).toHaveAttribute('aria-current', 'page')
      expect(screen.getByText(/Feature Flow Trace/i)).toBeInTheDocument()

      // What could this change affect? -> Change Impact
      await userEvent.click(screen.getByRole('button', { name: 'What could this change affect?' }))
      expect(screen.getByRole('button', { name: 'What could this change affect?' })).toHaveAttribute('aria-current', 'page')
      expect(screen.getByText(/Change Impact Preview/i)).toBeInTheDocument()

      // Back to Understand
      await userEvent.click(screen.getByRole('button', { name: 'Understand' }))
      expect(screen.getByRole('button', { name: 'Understand' })).toHaveAttribute('aria-current', 'page')
      expect(screen.getByRole('heading', { level: 3, name: 'What do you want to understand about Repo-One?' })).toBeInTheDocument()
    })

    it('Advanced analysis is collapsed initially and can be expanded/collapsed accessibly', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      const advBtn = screen.getByRole('button', { name: /Advanced analysis/i })
      expect(advBtn).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByRole('button', { name: 'Show how this works' })).not.toBeInTheDocument()

      // Expand
      await userEvent.click(advBtn)
      expect(advBtn).toHaveAttribute('aria-expanded', 'true')
      expect(screen.getByRole('button', { name: 'Show how this works' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'What could this change affect?' })).toBeInTheDocument()

      // Collapse
      await userEvent.click(advBtn)
      expect(advBtn).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByRole('button', { name: 'Show how this works' })).not.toBeInTheDocument()
    })

    it('Each starter question sends exact expected text through existing conversation submission path', async () => {
      const createConvMock = vi.fn().mockResolvedValue({
        conversation_id: 'conv_123',
        user_message: {
          message_id: 'msg_u1',
          conversation_id: 'conv_123',
          role: 'user',
          content: 'Where does the application start?',
          created_at: '2026-07-28T00:00:00Z',
        },
        assistant_message: {
          message_id: 'msg_a1',
          conversation_id: 'conv_123',
          role: 'assistant',
          content: 'The application entry point is main.py.',
          citations: [{ relative_path: 'main.py', start_line: 1, end_line: 10, symbol_name: 'main' }],
          evidence: [],
          created_at: '2026-07-28T00:00:00Z',
        },
      })

      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
        createConversation: createConvMock,
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      const starterBtn = screen.getByRole('button', { name: 'Where does the application start?' })
      await userEvent.click(starterBtn)

      expect(createConvMock).toHaveBeenCalledWith('repo_ux_1', {
        question: 'Where does the application start?',
      })

      await waitFor(() => {
        expect(screen.getByText('The application entry point is main.py.')).toBeInTheDocument()
      })
    })

    it('Starter question and normal Ask controls honor pending/submitting state', async () => {
      let resolveConv: (value: unknown) => void = () => {}
      const pendingPromise = new Promise((res) => {
        resolveConv = res
      })
      const createConvMock = vi.fn().mockReturnValue(pendingPromise)

      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
        createConversation: createConvMock,
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      const starterBtn = screen.getByRole('button', { name: 'How does authentication work?' })
      await userEvent.click(starterBtn)

      // While submitting, controls are disabled
      expect(screen.getByRole('button', { name: 'Asking...' })).toBeDisabled()
      expect(starterBtn).toBeDisabled()
      expect(screen.getByPlaceholderText('Ask about this repository...')).toBeDisabled()

      // Resolve promise
      resolveConv({
        conversation_id: 'conv_456',
        user_message: {
          message_id: 'msg_u2',
          conversation_id: 'conv_456',
          role: 'user',
          content: 'How does authentication work?',
          created_at: '2026-07-28T00:00:00Z',
        },
        assistant_message: {
          message_id: 'msg_a2',
          conversation_id: 'conv_456',
          role: 'assistant',
          content: 'Authentication is handled via JWT tokens.',
          created_at: '2026-07-28T00:00:00Z',
        },
      })

      await waitFor(() => {
        expect(screen.getByText('Authentication is handled via JWT tokens.')).toBeInTheDocument()
      })

      expect(screen.getByRole('button', { name: 'Ask' })).toBeInTheDocument()
    })
  })

  describe('RUNTIME-UX-005-FIX — Question-First Layout Verification', () => {
    const readyRepo1 = {
      repository_id: 'repo_ux_fix_1',
      name: 'Fix-Repo-One',
      source_type: 'github' as const,
      status: 'ready' as const,
      file_count: 8,
      chunk_count: 15,
      index_mode: 'static' as const,
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    }

    it('Ready repository shows Understand hero as primary workspace element without rendering import form above it', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Fix-Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      // Subtitle is present in header
      expect(screen.getByText('Understand a codebase before you change it.')).toBeInTheDocument()

      // Full import form is NOT rendered in ready workspace when tray is closed
      expect(screen.queryByText('Public GitHub Repository URL')).not.toBeInTheDocument()
      expect(screen.queryByText('Upload ZIP Archive')).not.toBeInTheDocument()

      // Compact Import repository button exists in header
      expect(screen.getByRole('button', { name: 'Import repository' })).toBeInTheDocument()
    })

    it('Header Import repository button toggles closable import tray', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Fix-Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      const importToggleBtn = screen.getByRole('button', { name: 'Import repository' })
      await userEvent.click(importToggleBtn)

      // Tray is now open
      expect(screen.getByText('Public GitHub Repository URL')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Close Import' })).toBeInTheDocument()

      // Click Close Import button in header
      await userEvent.click(screen.getByRole('button', { name: 'Close Import' }))
      expect(screen.queryByText('Public GitHub Repository URL')).not.toBeInTheDocument()
    })

    it('GitHub and ZIP import forms work from inside the import tray', async () => {
      const createGitHubMock = vi.fn().mockResolvedValue({
        repository: { ...readyRepo1, repository_id: 'repo_ux_fix_2', name: 'Fix-Repo-Two' },
        indexing_job: {
          job_id: 'job_fix_2',
          repository_id: 'repo_ux_fix_2',
          status: 'queued',
          progress_percentage: 0,
          current_step: 'Queued',
          created_at: '2026-07-28T00:00:00Z',
          updated_at: '2026-07-28T00:00:00Z',
        },
      })

      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
        createGitHubRepository: createGitHubMock,
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Import repository' })).toBeInTheDocument()
      })

      // Open tray
      await userEvent.click(screen.getByRole('button', { name: 'Import repository' }))

      const githubInput = screen.getByLabelText(/Public GitHub Repository URL/i)
      const githubBtn = screen.getByRole('button', { name: /Import GitHub Repository/i })

      await userEvent.type(githubInput, 'https://github.com/octocat/Fix-Repo-Two')
      await userEvent.click(githubBtn)

      expect(createGitHubMock).toHaveBeenCalledWith('https://github.com/octocat/Fix-Repo-Two', 'static')
    })

    it('No-repository state renders full import onboarding panel', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(screen.getByText('API Online (1.0.0)')).toBeInTheDocument()
      })

      expect(screen.getByText('Import a Repository to Begin')).toBeInTheDocument()
      expect(screen.getByText('Public GitHub Repository URL')).toBeInTheDocument()
      expect(screen.getByText('ZIP Archive File (.zip)')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Import repository' })).not.toBeInTheDocument()
    })

    it('Workspace navigation controls appear in the sidebar/rail', async () => {
      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(
          screen.getByRole('heading', {
            level: 3,
            name: 'What do you want to understand about Fix-Repo-One?',
          }),
        ).toBeInTheDocument()
      })

      const railNav = screen.getByRole('navigation', { name: 'Workspace Navigation' })
      expect(railNav).toBeInTheDocument()
      expect(railNav.closest('aside.trace-rail')).not.toBeNull()

      expect(screen.getByRole('button', { name: 'Understand' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Files' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Find code' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Advanced analysis/i })).toBeInTheDocument()
    })

    it('Renders Start Here Reading Guide role and clickable citations for orientation static guidance', async () => {
      const createConvMock = vi.fn().mockResolvedValue({
        conversation_id: 'conv_orient_1',
        repository_id: 'repo_ux_fix_1',
        user_message: {
          message_id: 'u1',
          role: 'user',
          content: 'What should I read first?',
          created_at: '2026-07-28T12:00:00Z',
        },
        assistant_message: {
          message_id: 'a1',
          role: 'assistant',
          content: 'Start here to explore this repository:\n1. Read README.md [E1]\n2. Read src/main.py [E2]',
          answer_mode: 'static_guidance',
          citations: [
            { relative_path: 'README.md', start_line: 1, end_line: 20, symbol_name: 'README', symbol_type: 'file' },
            { relative_path: 'src/main.py', start_line: 1, end_line: 15, symbol_name: 'main', symbol_type: 'function' },
          ],
          created_at: '2026-07-28T12:00:01Z',
        },
        request_metadata: { latency_ms: 45, chunks_retrieved: 2, retrieval_mode: 'static' },
      })

      const listFilesMock = vi.fn().mockResolvedValue({
        repository_id: 'repo_ux_fix_1',
        files: [{ path: 'README.md', language: 'markdown', chunk_count: 1 }],
      })

      const getFileContentMock = vi.fn().mockResolvedValue({
        repository_id: 'repo_ux_fix_1',
        path: 'README.md',
        language: 'markdown',
        content: '# Welcome to project\nThis is line 2.',
        line_count: 2,
        is_complete: true,
        completeness_reason: 'source_file_exact_match',
      })

      const mockClient = {
        getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: '1.0.0', timestamp: '2026-07-28' }),
        listRepositories: vi.fn().mockResolvedValue({ repositories: [readyRepo1] }),
        createConversation: createConvMock,
        listRepositoryFiles: listFilesMock,
        getRepositoryFileContent: getFileContentMock,
      } as unknown as ApiClient

      render(<App client={mockClient} />)

      await waitFor(() => {
        expect(screen.getByText('What should I read first?')).toBeInTheDocument()
      })

      // Click starter question 'What should I read first?'
      await userEvent.click(screen.getByRole('button', { name: 'What should I read first?' }))

      await waitFor(() => {
        expect(screen.getByText('Start Here Reading Guide')).toBeInTheDocument()
        expect(screen.getByText(/Start here to explore this repository:/i)).toBeInTheDocument()
        expect(screen.getByRole('button', { name: /\[1\] README.md:1-20 \(README\)/i })).toBeInTheDocument()
      })

      // Click citation button -> switches view to files tab, opens cited file and highlights cited lines
      await userEvent.click(screen.getByRole('button', { name: /\[1\] README.md:1-20 \(README\)/i }))

      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Files' })).toHaveClass('active')
        expect(getFileContentMock).toHaveBeenCalledWith('repo_ux_fix_1', 'README.md')
        expect(screen.getByText('# Welcome to project')).toBeInTheDocument()
      })
    })
  })
})
