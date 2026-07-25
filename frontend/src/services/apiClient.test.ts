import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from './apiClient'

describe('ApiClient', () => {
  it('getHealth() uses relative /api/v1/health and credentials: include', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        status: 'ok',
        version: '1.0.0',
        timestamp: '2026-07-23T16:00:00Z',
      }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    const health = await client.getHealth()

    expect(mockFetch).toHaveBeenCalledWith('/api/v1/health', {
      method: 'GET',
      credentials: 'include',
      headers: {
        Accept: 'application/json',
      },
    })
    expect(health).toEqual({
      status: 'ok',
      version: '1.0.0',
      timestamp: '2026-07-23T16:00:00Z',
    })
  })

  it('defaults to relative /api/v1 base path when VITE_API_BASE_URL is unset', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'ok', version: '1.0.0', timestamp: '2026-07-23T16:00:00Z' }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    await client.getHealth()

    const requestedUrl = mockFetch.mock.calls[0][0] as string
    expect(requestedUrl).toBe('/api/v1/health')
  })

  it('supports custom production backend URL via VITE_API_BASE_URL or options.baseUrl', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'ok', version: '1.0.0', timestamp: '2026-07-23T16:00:00Z' }),
    })

    const clientWithOptions = new ApiClient({
      baseUrl: 'https://sourcetrace-backend.onrender.com/api/v1',
      customFetch: mockFetch as unknown as typeof fetch,
    })
    await clientWithOptions.getHealth()
    const requestedUrl = mockFetch.mock.calls[0][0] as string
    expect(requestedUrl).toBe('https://sourcetrace-backend.onrender.com/api/v1/health')
  })

  it('maps JSON ErrorEnvelope to safe typed ApiError', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({
        error: {
          code: 'RESOURCE_NOT_FOUND',
          message: 'The requested resource was not found.',
          request_id: 'req_12345',
        },
      }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })

    try {
      await client.getHealth()
      expect.fail('Should have thrown ApiError')
    } catch (err) {
      const apiErr = err as ApiError
      expect(apiErr).toBeInstanceOf(ApiError)
      expect(apiErr.code).toBe('RESOURCE_NOT_FOUND')
      expect(apiErr.message).toBe('The requested resource was not found.')
      expect(apiErr.status).toBe(404)
      expect(apiErr.requestId).toBe('req_12345')
    }
  })

  it('maps non-JSON HTTP errors safely', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error('Non-JSON body')
      },
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })

    try {
      await client.getHealth()
      expect.fail('Should have thrown ApiError')
    } catch (err) {
      const apiErr = err as ApiError
      expect(apiErr).toBeInstanceOf(ApiError)
      expect(apiErr.code).toBe('HTTP_ERROR')
      expect(apiErr.message).toBe('API request failed with status 500')
      expect(apiErr.status).toBe(500)
    }
  })

  it('binds default global fetch to globalThis when customFetch is omitted', async () => {
    let isGlobalReceiver = false
    let requestedUrl: string | undefined
    let requestOptions: RequestInit | undefined
    const originalFetch = globalThis.fetch

    const mockGlobalFetch = function (this: unknown, url: string | URL | Request, init?: RequestInit) {
      if (this === globalThis) {
        isGlobalReceiver = true
      }
      requestedUrl = String(url)
      requestOptions = init
      return Promise.resolve({
        ok: true,
        json: async () => ({
          status: 'ok',
          version: '1.0.0',
          timestamp: '2026-07-23T16:00:00Z',
        }),
      } as Response)
    }

    globalThis.fetch = mockGlobalFetch as typeof fetch

    try {
      const client = new ApiClient()
      const health = await client.getHealth()

      expect(isGlobalReceiver).toBe(true)
      expect(requestedUrl).toBe('/api/v1/health')
      expect(requestOptions?.credentials).toBe('include')
      expect(health).toEqual({
        status: 'ok',
        version: '1.0.0',
        timestamp: '2026-07-23T16:00:00Z',
      })
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('createGitHubRepository() sends POST to /api/v1/repositories with JSON body and credentials', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        repository: {
          repository_id: 'repo_123',
          name: 'Hello-World',
          source_type: 'github',
          github_url: 'https://github.com/octocat/Hello-World',
          status: 'pending',
          file_count: 0,
          chunk_count: 0,
          created_at: '2026-07-24T00:00:00Z',
          updated_at: '2026-07-24T00:00:00Z',
        },
        indexing_job: {
          job_id: 'job_123',
          repository_id: 'repo_123',
          status: 'queued',
          progress_percentage: 0,
          current_step: 'Queued',
          created_at: '2026-07-24T00:00:00Z',
          updated_at: '2026-07-24T00:00:00Z',
        },
      }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    const res = await client.createGitHubRepository('https://github.com/octocat/Hello-World')

    expect(mockFetch).toHaveBeenCalledWith('/api/v1/repositories', {
      method: 'POST',
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        github_url: 'https://github.com/octocat/Hello-World',
        index_mode: 'static',
      }),
    })
    expect(res.repository.repository_id).toBe('repo_123')
    expect(res.indexing_job.job_id).toBe('job_123')
  })

  it('uploadZipRepository() sends POST to /api/v1/repositories/upload with FormData and credentials', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        repository: {
          repository_id: 'repo_456',
          name: 'my-project',
          source_type: 'zip',
          status: 'pending',
          file_count: 0,
          chunk_count: 0,
          created_at: '2026-07-24T00:00:00Z',
          updated_at: '2026-07-24T00:00:00Z',
        },
        indexing_job: {
          job_id: 'job_456',
          repository_id: 'repo_456',
          status: 'queued',
          progress_percentage: 0,
          current_step: 'Queued',
          created_at: '2026-07-24T00:00:00Z',
          updated_at: '2026-07-24T00:00:00Z',
        },
      }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    const file = new File(['fake zip content'], 'project.zip', { type: 'application/zip' })
    const res = await client.uploadZipRepository(file, 'my-project')

    expect(mockFetch).toHaveBeenCalledWith('/api/v1/repositories/upload', {
      method: 'POST',
      credentials: 'include',
      headers: {
        Accept: 'application/json',
      },
      body: expect.any(FormData),
    })

    const callInit = mockFetch.mock.calls[0][1] as RequestInit
    const formData = callInit.body as FormData
    expect(formData.get('file')).toBe(file)
    expect(formData.get('name')).toBe('my-project')
    expect(res.repository.repository_id).toBe('repo_456')
  })
})
