import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from './apiClient'
import type { TokenStorage } from './apiClient'

describe('ApiClient', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
  })

  it('getHealth() is public: uses credentials: omit and sends no Authorization header', async () => {
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
      credentials: 'omit',
      headers: expect.any(Headers),
    })

    const headers = mockFetch.mock.calls[0][1]?.headers as Headers
    expect(headers.get('Accept')).toBe('application/json')
    expect(headers.has('Authorization')).toBe(false)
    expect(health).toEqual({
      status: 'ok',
      version: '1.0.0',
      timestamp: '2026-07-23T16:00:00Z',
    })
  })

  it('getCapabilities() is public: uses credentials: omit and sends no Authorization header', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        allowed_index_modes: ['static'],
        default_index_mode: 'static',
        lexical_search_available: true,
        semantic_search_available: false,
        generation_available: false,
      }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    await client.getCapabilities()

    const callInit = mockFetch.mock.calls[0][1] as RequestInit
    expect(callInit.credentials).toBe('omit')
    const headers = callInit.headers as Headers
    expect(headers.has('Authorization')).toBe(false)
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

  it('supports custom production backend URL via options.baseUrl', async () => {
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

  it('first protected request provisions token via POST /auth/session, then sends request with Bearer header', async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          access_token: 'jwt_mock_token_123',
          token_type: 'Bearer',
          expires_in: 604800,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          repositories: [],
        }),
      })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    const repos = await client.listRepositories()

    expect(mockFetch).toHaveBeenCalledTimes(2)

    // Call 1: Provisioning request
    const [provUrl, provInit] = mockFetch.mock.calls[0]
    expect(provUrl).toBe('/api/v1/auth/session')
    expect(provInit.method).toBe('POST')
    expect(provInit.credentials).toBe('include')
    const provHeaders = provInit.headers as Headers
    expect(provHeaders.has('Authorization')).toBe(false)

    // Call 2: Protected request
    const [reqUrl, reqInit] = mockFetch.mock.calls[1]
    expect(reqUrl).toBe('/api/v1/repositories')
    expect(reqInit.method).toBe('GET')
    expect(reqInit.credentials).toBe('omit')
    const reqHeaders = reqInit.headers as Headers
    expect(reqHeaders.get('Authorization')).toBe('Bearer jwt_mock_token_123')

    expect(sessionStorage.getItem('sourcetrace.access_token')).toBe('jwt_mock_token_123')
    expect(localStorage.getItem('sourcetrace.access_token')).toBeNull()
    expect(repos).toEqual({ repositories: [] })
  })

  it('validates TokenResponse strictly and rejects invalid responses', async () => {
    const invalidResponses = [
      { access_token: '', token_type: 'Bearer', expires_in: 604800 },
      { access_token: 'valid', token_type: 'Basic', expires_in: 604800 },
      { access_token: 'valid', token_type: 'Bearer', expires_in: 0 },
      { access_token: 'valid', token_type: 'Bearer', expires_in: -10 },
      { access_token: 'valid', token_type: 'Bearer', expires_in: '604800' },
      { access_token: 'valid', token_type: 'Bearer', expires_in: null },
      { access_token: 'valid', token_type: 'Bearer' },
      { token_type: 'Bearer', expires_in: 604800 },
    ]

    for (const invalidResp of invalidResponses) {
      const mockFetch = vi.fn().mockResolvedValueOnce({
        ok: true,
        json: async () => invalidResp,
      })

      const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })

      try {
        await client.listRepositories()
        expect.fail(`Should have thrown ApiError for invalid response: ${JSON.stringify(invalidResp)}`)
      } catch (err) {
        const apiErr = err as ApiError
        expect(apiErr).toBeInstanceOf(ApiError)
        expect(apiErr.code).toBe('AUTH_RESPONSE_INVALID')
        expect(apiErr.message).toBe('Authentication response was invalid.')
      }

      expect(sessionStorage.getItem('sourcetrace.access_token')).toBeNull()
    }
  })

  it('uses pre-populated token from sessionStorage without re-provisioning', async () => {
    sessionStorage.setItem('sourcetrace.access_token', 'jwt_existing_stored_token')

    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ repositories: [] }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    await client.listRepositories()

    expect(mockFetch).toHaveBeenCalledTimes(1)
    const [reqUrl, reqInit] = mockFetch.mock.calls[0]
    expect(reqUrl).toBe('/api/v1/repositories')
    const reqHeaders = reqInit.headers as Headers
    expect(reqHeaders.get('Authorization')).toBe('Bearer jwt_existing_stored_token')
  })

  it('deduplicates concurrent protected requests to share a single provisioning call', async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          access_token: 'jwt_shared_token',
          token_type: 'Bearer',
          expires_in: 604800,
        }),
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({ repositories: [] }),
      })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })

    const [res1, res2, res3] = await Promise.all([
      client.listRepositories(),
      client.listRepositories(),
      client.listRepositories(),
    ])

    // Exactly 1 provisioning call + 3 protected calls = 4 fetch calls
    expect(mockFetch).toHaveBeenCalledTimes(4)
    expect(mockFetch.mock.calls[0][0]).toBe('/api/v1/auth/session')

    expect(res1).toEqual({ repositories: [] })
    expect(res2).toEqual({ repositories: [] })
    expect(res3).toEqual({ repositories: [] })
  })

  it('recovers from 401 on protected request by clearing stale token, re-provisioning, and retrying once', async () => {
    sessionStorage.setItem('sourcetrace.access_token', 'jwt_stale_token')

    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({
          error: {
            code: 'UNAUTHORIZED',
            message: 'Authentication credentials are missing or invalid.',
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          access_token: 'jwt_fresh_replacement_token',
          token_type: 'Bearer',
          expires_in: 604800,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ repositories: [] }),
      })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })
    const res = await client.listRepositories()

    expect(mockFetch).toHaveBeenCalledTimes(3)

    // Call 1: Initial protected request with stale token -> returns 401
    expect(mockFetch.mock.calls[0][0]).toBe('/api/v1/repositories')
    expect((mockFetch.mock.calls[0][1].headers as Headers).get('Authorization')).toBe('Bearer jwt_stale_token')

    // Call 2: Re-provisioning request -> returns fresh token
    expect(mockFetch.mock.calls[1][0]).toBe('/api/v1/auth/session')

    // Call 3: Retried protected request -> returns 200 with fresh token
    expect(mockFetch.mock.calls[2][0]).toBe('/api/v1/repositories')
    expect((mockFetch.mock.calls[2][1].headers as Headers).get('Authorization')).toBe(
      'Bearer jwt_fresh_replacement_token',
    )

    expect(sessionStorage.getItem('sourcetrace.access_token')).toBe('jwt_fresh_replacement_token')
    expect(res).toEqual({ repositories: [] })
  })

  it('stops after single 401 retry if retry fails with 401 again (no infinite loop)', async () => {
    sessionStorage.setItem('sourcetrace.access_token', 'jwt_stale_token')

    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({
          error: { code: 'UNAUTHORIZED', message: 'Stale token' },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          access_token: 'jwt_replacement_token',
          token_type: 'Bearer',
          expires_in: 604800,
        }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({
          error: { code: 'UNAUTHORIZED', message: 'Replacement token also rejected' },
        }),
      })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })

    try {
      await client.listRepositories()
      expect.fail('Should have thrown ApiError on second 401')
    } catch (err) {
      const apiErr = err as ApiError
      expect(apiErr.status).toBe(401)
      expect(apiErr.code).toBe('UNAUTHORIZED')
      expect(apiErr.message).toBe('Replacement token also rejected')
    }

    expect(mockFetch).toHaveBeenCalledTimes(3)
  })

  it('404 or 500 response on protected request does NOT clear or replace stored token', async () => {
    sessionStorage.setItem('sourcetrace.access_token', 'jwt_valid_token')

    const mockFetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({
        error: { code: 'RESOURCE_NOT_FOUND', message: 'Not found' },
      }),
    })

    const client = new ApiClient({ customFetch: mockFetch as unknown as typeof fetch })

    try {
      await client.getRepository('repo_missing')
      expect.fail('Should have thrown 404 ApiError')
    } catch (err) {
      expect((err as ApiError).status).toBe(404)
    }

    expect(sessionStorage.getItem('sourcetrace.access_token')).toBe('jwt_valid_token')
  })

  it('safely falls back to in-memory storage if sessionStorage throws SecurityError', async () => {
    const throwingStorage: TokenStorage = {
      getItem: () => {
        throw new Error('SecurityError: Access denied')
      },
      setItem: () => {
        throw new Error('SecurityError: Quota exceeded')
      },
      removeItem: () => {
        throw new Error('SecurityError: Access denied')
      },
    }

    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          access_token: 'jwt_memory_only_token',
          token_type: 'Bearer',
          expires_in: 604800,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ repositories: [] }),
      })

    const client = new ApiClient({
      customFetch: mockFetch as unknown as typeof fetch,
      storage: throwingStorage,
    })

    const res = await client.listRepositories()
    expect(res).toEqual({ repositories: [] })
    expect(mockFetch).toHaveBeenCalledTimes(2)
  })

  it('createGitHubRepository() sends POST to /api/v1/repositories with JSON body and Bearer token', async () => {
    sessionStorage.setItem('sourcetrace.access_token', 'jwt_github_test_token')

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

    const callInit = mockFetch.mock.calls[0][1] as RequestInit
    expect(callInit.credentials).toBe('omit')
    const headers = callInit.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer jwt_github_test_token')
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(res.repository.repository_id).toBe('repo_123')
  })

  it('uploadZipRepository() sends POST to /api/v1/repositories/upload with FormData and Bearer token', async () => {
    sessionStorage.setItem('sourcetrace.access_token', 'jwt_zip_test_token')

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

    const callInit = mockFetch.mock.calls[0][1] as RequestInit
    expect(callInit.credentials).toBe('omit')
    const headers = callInit.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer jwt_zip_test_token')
    expect(res.repository.repository_id).toBe('repo_456')
  })
})
