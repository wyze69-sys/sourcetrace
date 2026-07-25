import type {
  ConversationDetailResponse,
  CreateConversationRequest,
  CreateConversationResponse,
  CreateRepositoryResponse,
  DeleteRepositoryResponse,
  ErrorEnvelope,
  EvidenceSearchResponse,
  HealthResponse,
  IndexingJob,
  Repository,
  RepositoryListResponse,
  SendMessageRequest,
  SendMessageResponse,
  ServerCapabilities,
} from './types'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly requestId?: string

  constructor(message: string, code: string, status: number, requestId?: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.requestId = requestId
  }
}

export interface ApiClientOptions {
  customFetch?: typeof fetch
  baseUrl?: string
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly fetcher: typeof fetch

  constructor(options: ApiClientOptions = {}) {
    this.fetcher = options.customFetch ?? globalThis.fetch.bind(globalThis)
    if (options.baseUrl !== undefined) {
      this.baseUrl = options.baseUrl
    } else {
      const envUrl = import.meta.env.VITE_API_BASE_URL
      if (envUrl && envUrl.trim().length > 0) {
        const trimmed = envUrl.trim().replace(/\/$/, '')
        this.baseUrl = trimmed.endsWith('/api/v1') ? trimmed : `${trimmed}/api/v1`
      } else {
        this.baseUrl = '/api/v1'
      }
    }
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const url = `${this.baseUrl}${path}`
    const response = await this.fetcher(url, {
      ...init,
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        ...init?.headers,
      },
    })

    if (!response.ok) {
      let code = 'HTTP_ERROR'
      let message = `API request failed with status ${response.status}`
      let requestId: string | undefined

      try {
        const body = (await response.json()) as ErrorEnvelope
        if (body?.error) {
          code = body.error.code || code
          message = body.error.message || message
          requestId = body.error.request_id
        }
      } catch {
        // Fallback for non-JSON error responses
      }

      throw new ApiError(message, code, response.status, requestId)
    }

    return (await response.json()) as T
  }

  async getHealth(): Promise<HealthResponse> {
    return this.request<HealthResponse>('/health', { method: 'GET' })
  }

  async getCapabilities(): Promise<ServerCapabilities> {
    return this.request<ServerCapabilities>('/capabilities', { method: 'GET' })
  }

  async searchEvidence(
    repositoryId: string,
    query: string,
    limit = 5,
  ): Promise<EvidenceSearchResponse> {
    return this.request<EvidenceSearchResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/search`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, limit }),
      },
    )
  }

  // Future API method signatures (not called by application yet)
  async listRepositories(): Promise<RepositoryListResponse> {
    return this.request<RepositoryListResponse>('/repositories', { method: 'GET' })
  }

  async createGitHubRepository(githubUrl: string, indexMode?: string): Promise<CreateRepositoryResponse> {
    return this.request<CreateRepositoryResponse>('/repositories', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ github_url: githubUrl, index_mode: indexMode ?? 'static' }),
    })
  }

  async uploadZipRepository(file: File, name?: string, indexMode?: string): Promise<CreateRepositoryResponse> {
    const formData = new FormData()
    formData.append('file', file)
    if (name && name.trim()) {
      formData.append('name', name.trim())
    }
    if (indexMode) {
      formData.append('index_mode', indexMode)
    }
    return this.request<CreateRepositoryResponse>('/repositories/upload', {
      method: 'POST',
      body: formData,
    })
  }

  async getRepository(repositoryId: string): Promise<Repository> {
    return this.request<Repository>(`/repositories/${encodeURIComponent(repositoryId)}`, {
      method: 'GET',
    })
  }

  async deleteRepository(repositoryId: string): Promise<DeleteRepositoryResponse> {
    return this.request<DeleteRepositoryResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}`,
      { method: 'DELETE' },
    )
  }

  async refreshRepository(repositoryId: string): Promise<CreateRepositoryResponse> {
    return this.request<CreateRepositoryResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/refresh`,
      { method: 'POST' },
    )
  }

  async getIndexingJob(jobId: string): Promise<IndexingJob> {
    return this.request<IndexingJob>(`/indexing-jobs/${encodeURIComponent(jobId)}`, {
      method: 'GET',
    })
  }

  async createConversation(
    repositoryId: string,
    req: CreateConversationRequest,
  ): Promise<CreateConversationResponse> {
    return this.request<CreateConversationResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/conversations`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      },
    )
  }

  async getConversation(
    repositoryId: string,
    conversationId: string,
  ): Promise<ConversationDetailResponse> {
    return this.request<ConversationDetailResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/conversations/${encodeURIComponent(conversationId)}`,
      { method: 'GET' },
    )
  }

  async sendMessage(
    repositoryId: string,
    conversationId: string,
    req: SendMessageRequest,
  ): Promise<SendMessageResponse> {
    return this.request<SendMessageResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      },
    )
  }
}

export const apiClient = new ApiClient()
