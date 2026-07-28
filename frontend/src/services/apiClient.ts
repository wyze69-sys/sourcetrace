import type {
  ChangeImpactResponse,
  ConversationDetailResponse,
  CreateConversationRequest,
  CreateConversationResponse,
  CreateRepositoryResponse,
  DeleteRepositoryResponse,
  DiffImpactResponse,
  ErrorEnvelope,
  EvidenceSearchResponse,
  FlowTraceResponse,
  HealthResponse,
  ImpactMode,
  IndexingJob,
  Repository,
  RepositoryListResponse,
  SendMessageRequest,
  SendMessageResponse,
  ServerCapabilities,
  TokenResponse,
  TraceMode,
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

export interface TokenStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

const TOKEN_STORAGE_KEY = 'sourcetrace.access_token'

class SafeSessionStorage implements TokenStorage {
  getItem(key: string): string | null {
    try {
      if (typeof globalThis.sessionStorage !== 'undefined') {
        return globalThis.sessionStorage.getItem(key)
      }
    } catch {
      // Storage access blocked or unavailable
    }
    return null
  }

  setItem(key: string, value: string): void {
    try {
      if (typeof globalThis.sessionStorage !== 'undefined') {
        globalThis.sessionStorage.setItem(key, value)
      }
    } catch {
      // Storage access blocked or quota exceeded
    }
  }

  removeItem(key: string): void {
    try {
      if (typeof globalThis.sessionStorage !== 'undefined') {
        globalThis.sessionStorage.removeItem(key)
      }
    } catch {
      // Storage access blocked
    }
  }
}

export interface ApiClientOptions {
  customFetch?: typeof fetch
  baseUrl?: string
  storage?: TokenStorage | null
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly fetcher: typeof fetch
  private readonly storage: TokenStorage | null
  private accessToken: string | null = null
  private provisioningPromise: Promise<string> | null = null

  constructor(options: ApiClientOptions = {}) {
    this.fetcher = options.customFetch ?? globalThis.fetch.bind(globalThis)
    this.storage = options.storage !== undefined ? options.storage : new SafeSessionStorage()

    if (this.storage) {
      try {
        const stored = this.storage.getItem(TOKEN_STORAGE_KEY)
        if (stored && stored.trim().length > 0) {
          this.accessToken = stored.trim()
        }
      } catch {
        // Storage read failed
      }
    }

    this.baseUrl = options.baseUrl ?? '/api/v1'
  }

  clearAccessToken(): void {
    this.accessToken = null
    if (this.storage) {
      try {
        this.storage.removeItem(TOKEN_STORAGE_KEY)
      } catch {
        // Storage remove failed
      }
    }
  }

  private clearToken(staleToken?: string): void {
    if (staleToken !== undefined && this.accessToken !== staleToken) {
      return
    }
    this.clearAccessToken()
  }

  private async provisionAccessToken(): Promise<string> {
    if (this.provisioningPromise) {
      return this.provisioningPromise
    }

    this.provisioningPromise = (async () => {
      try {
        const res = await this.request<TokenResponse>('/auth/session', { method: 'POST' }, 'provisioning')

        if (
          !res ||
          typeof res !== 'object' ||
          typeof res.access_token !== 'string' ||
          res.access_token.trim().length === 0 ||
          res.token_type !== 'Bearer' ||
          typeof res.expires_in !== 'number' ||
          !Number.isInteger(res.expires_in) ||
          res.expires_in <= 0
        ) {
          throw new ApiError('Authentication response was invalid.', 'AUTH_RESPONSE_INVALID', 500)
        }

        const token = res.access_token.trim()
        this.accessToken = token
        if (this.storage) {
          try {
            this.storage.setItem(TOKEN_STORAGE_KEY, token)
          } catch {
            // Storage write failed
          }
        }
        return token
      } finally {
        this.provisioningPromise = null
      }
    })()

    return this.provisioningPromise
  }

  private async request<T>(
    path: string,
    init?: RequestInit,
    mode: 'public' | 'protected' | 'provisioning' = 'protected',
    retried = false,
  ): Promise<T> {
    let tokenForRequest: string | null = null

    if (mode === 'protected') {
      if (!this.accessToken) {
        await this.provisionAccessToken()
      }
      tokenForRequest = this.accessToken
    }

    const url = `${this.baseUrl}${path}`
    const headers = new Headers(init?.headers)
    if (!headers.has('Accept')) {
      headers.set('Accept', 'application/json')
    }

    if (mode === 'protected' && tokenForRequest) {
      headers.set('Authorization', `Bearer ${tokenForRequest}`)
    } else if (mode === 'public' || mode === 'provisioning') {
      headers.delete('Authorization')
    }

    const credentialsMode: RequestCredentials = mode === 'provisioning' ? 'include' : 'omit'

    const response = await this.fetcher(url, {
      ...init,
      credentials: credentialsMode,
      headers,
    })

    if (!response.ok) {
      if (mode === 'protected' && response.status === 401 && !retried) {
        // No-ops if a concurrent request already swapped in a fresh token; the
        // retry then re-provisions only when no token is left.
        this.clearToken(tokenForRequest ?? undefined)
        return this.request<T>(path, init, 'protected', true)
      }

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
    return this.request<HealthResponse>('/health', { method: 'GET' }, 'public')
  }

  async getCapabilities(): Promise<ServerCapabilities> {
    return this.request<ServerCapabilities>('/capabilities', { method: 'GET' }, 'public')
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
      'protected',
    )
  }

  async traceFlow(
    repositoryId: string,
    entry: string,
    maxDepth?: number,
    mode: TraceMode = 'static',
  ): Promise<FlowTraceResponse> {
    return this.request<FlowTraceResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/trace`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          entry,
          mode,
          ...(maxDepth !== undefined ? { max_depth: maxDepth } : {}),
        }),
      },
      'protected',
    )
  }

  async previewImpact(
    repositoryId: string,
    symbol: string,
    maxDepth?: number,
    mode: ImpactMode = 'static',
  ): Promise<ChangeImpactResponse> {
    return this.request<ChangeImpactResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/impact`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol,
          mode,
          ...(maxDepth !== undefined ? { max_depth: maxDepth } : {}),
        }),
      },
      'protected',
    )
  }

  async previewDiffImpact(
    repositoryId: string,
    diff: string,
    maxDepth?: number,
    mode: ImpactMode = 'static',
  ): Promise<DiffImpactResponse> {
    return this.request<DiffImpactResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/impact/diff`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          diff,
          mode,
          ...(maxDepth !== undefined ? { max_depth: maxDepth } : {}),
        }),
      },
      'protected',
    )
  }

  async listRepositories(): Promise<RepositoryListResponse> {
    return this.request<RepositoryListResponse>('/repositories', { method: 'GET' }, 'protected')
  }

  async createGitHubRepository(githubUrl: string, indexMode?: string): Promise<CreateRepositoryResponse> {
    const effectiveMode = indexMode === 'ai_assist' ? 'static' : (indexMode ?? 'static')
    return this.request<CreateRepositoryResponse>(
      '/repositories',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ github_url: githubUrl, index_mode: effectiveMode }),
      },
      'protected',
    )
  }

  async uploadZipRepository(file: File, name?: string, indexMode?: string): Promise<CreateRepositoryResponse> {
    const effectiveMode = indexMode === 'ai_assist' ? 'static' : indexMode
    const formData = new FormData()
    formData.append('file', file)
    if (name && name.trim()) {
      formData.append('name', name.trim())
    }
    if (effectiveMode) {
      formData.append('index_mode', effectiveMode)
    }
    return this.request<CreateRepositoryResponse>(
      '/repositories/upload',
      {
        method: 'POST',
        body: formData,
      },
      'protected',
    )
  }

  async getRepository(
    repositoryId: string,
    checkFreshness: boolean = false,
  ): Promise<Repository> {
    const query = checkFreshness ? '?check_freshness=true' : ''
    return this.request<Repository>(
      `/repositories/${encodeURIComponent(repositoryId)}${query}`,
      { method: 'GET' },
      'protected',
    )
  }

  async deleteRepository(repositoryId: string): Promise<DeleteRepositoryResponse> {
    return this.request<DeleteRepositoryResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}`,
      { method: 'DELETE' },
      'protected',
    )
  }

  async refreshRepository(repositoryId: string): Promise<CreateRepositoryResponse> {
    return this.request<CreateRepositoryResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/refresh`,
      { method: 'POST' },
      'protected',
    )
  }

  async getIndexingJob(jobId: string): Promise<IndexingJob> {
    return this.request<IndexingJob>(
      `/indexing-jobs/${encodeURIComponent(jobId)}`,
      { method: 'GET' },
      'protected',
    )
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
      'protected',
    )
  }

  async getConversation(
    repositoryId: string,
    conversationId: string,
  ): Promise<ConversationDetailResponse> {
    return this.request<ConversationDetailResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/conversations/${encodeURIComponent(conversationId)}`,
      { method: 'GET' },
      'protected',
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
      'protected',
    )
  }
}

export const apiClient = new ApiClient()
