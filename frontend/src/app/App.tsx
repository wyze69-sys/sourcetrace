import { useCallback, useEffect, useRef, useState } from 'react'
import { apiClient as defaultApiClient, ApiError, type ApiClient } from '../services/apiClient'
import { FlowTracePanel } from './FlowTracePanel'
import type {
  EvidenceSearchItem,
  HealthResponse,
  IndexingJob,
  IndexMode,
  Message,
  Repository,
  ServerCapabilities,
} from '../services/types'

export interface AppProps {
  client?: ApiClient
}

type AppState = 'loading' | 'ready' | 'error'

const SAFE_NETWORK_ERROR_MESSAGE =
  'Unable to reach the SourceTrace API. Check that the local service is running and try again.'

function validateGitHubUrl(url: string): string | null {
  const trimmed = url.trim()
  if (!trimmed) {
    return 'GitHub URL is required.'
  }
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return 'Please enter a valid HTTPS GitHub URL (e.g. https://github.com/owner/repo).'
  }

  if (parsed.protocol !== 'https:') {
    return 'URL protocol must be https:.'
  }

  const hostname = parsed.hostname.toLowerCase()
  if (hostname !== 'github.com' && hostname !== 'www.github.com') {
    return 'URL host must be github.com.'
  }

  const pathSegments = parsed.pathname.split('/').filter(Boolean)
  if (pathSegments.length < 2) {
    return 'URL must include repository owner and name (e.g. https://github.com/owner/repo).'
  }

  return null
}

function validateZipFile(file: File | null): string | null {
  if (!file) {
    return 'Please select a ZIP file to upload.'
  }
  if (!file.name.toLowerCase().endsWith('.zip')) {
    return 'Uploaded file must have a .zip extension.'
  }
  const maxBytes = 25 * 1024 * 1024
  if (file.size > maxBytes) {
    return 'ZIP file size exceeds the 25 MB limit.'
  }
  return null
}

function getSafeErrorCategory(errorMessage?: string | null, currentStep?: string | null): string {
  if (errorMessage && errorMessage.trim()) {
    const cleanMsg = errorMessage.split('\n')[0].replace(/([A-Z]:\\[^\s]+|\/[^\s]+)/gi, '[path]').trim()
    const lower = cleanMsg.toLowerCase()
    if (
      lower.includes('quota is exhausted') ||
      lower.includes('rate limited') ||
      lower.includes('input limit') ||
      lower.includes('configuration is invalid') ||
      lower.includes('timed out') ||
      lower.includes('server error')
    ) {
      return cleanMsg
    }
  }

  const text = `${currentStep || ''} ${errorMessage || ''}`.toLowerCase()

  if (text.includes('private') || text.includes('unavailable') || text.includes('url is invalid')) {
    return 'GitHub repository is private or unavailable.'
  }
  if (text.includes('download') || text.includes('archive download')) {
    return 'GitHub archive could not be downloaded.'
  }
  if (text.includes('limit') || text.includes('too large') || text.includes('exceeds')) {
    return 'Repository archive exceeded the ingestion limits.'
  }
  if (text.includes('no supported') || text.includes('unsupported') || text.includes('empty')) {
    return 'Repository contains no supported source files.'
  }
  if (text.includes('embedding') || text.includes('embed')) {
    return 'Indexing failed while generating embeddings.'
  }
  if (text.includes('storage') || text.includes('database') || text.includes('mongo')) {
    return 'Database storage failed.'
  }
  if (errorMessage && errorMessage.trim()) {
    return errorMessage.split('\n')[0].replace(/([A-Z]:\\[^\s]+|\/[^\s]+)/gi, '[path]')
  }
  return 'Indexing failed safely.'
}

export function App({ client = defaultApiClient }: AppProps) {
  const [state, setState] = useState<AppState>('loading')
  const [healthData, setHealthData] = useState<HealthResponse | null>(null)
  const [errorDetails, setErrorDetails] = useState<string | null>(null)
  const [capabilities, setCapabilities] = useState<ServerCapabilities | null>(null)
  const [selectedIndexMode, setSelectedIndexMode] = useState<IndexMode>('static')

  // Repositories & Indexing jobs state
  const [repositories, setRepositories] = useState<Repository[]>([])
  const [activeJobs, setActiveJobs] = useState<Record<string, IndexingJob>>({})
  const [selectedRepoId, setSelectedRepoId] = useState<string | null>(null)
  const [ariaLiveMsg, setAriaLiveMsg] = useState<string>('')

  // GitHub Form State
  const [githubUrl, setGithubUrl] = useState('')
  const [githubError, setGithubError] = useState<string | null>(null)
  const [githubSubmitting, setGithubSubmitting] = useState(false)

  // ZIP Form State
  const [zipFile, setZipFile] = useState<File | null>(null)
  const [zipName, setZipName] = useState('')
  const [zipError, setZipError] = useState<string | null>(null)
  const [zipSubmitting, setZipSubmitting] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Chat State
  const [conversations, setConversations] = useState<
    Record<string, { conversationId: string; messages: Message[] }>
  >({})
  const [chatQuestion, setChatQuestion] = useState('')
  const [chatSubmitting, setChatSubmitting] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)

  // Evidence Search State
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<EvidenceSearchItem[] | null>(null)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)

  const loadRepositories = useCallback(async () => {
    try {
      const res = await client.listRepositories()
      setRepositories(res.repositories || [])
    } catch {
      // Non-fatal if session has no repos yet or on cold start
    }
  }, [client])

  const checkHealth = useCallback(async () => {
    setState('loading')
    setErrorDetails(null)
    try {
      const data = await client.getHealth()
      setHealthData(data)
      try {
        const caps = await client.getCapabilities()
        setCapabilities(caps)
        if (caps.default_index_mode === 'static' || caps.default_index_mode === 'cloud_ai') {
          setSelectedIndexMode(caps.default_index_mode as IndexMode)
        }
      } catch {
        // Fallback capabilities if getCapabilities not reachable yet
      }
      setState('ready')
      await loadRepositories()
    } catch (err) {
      setHealthData(null)
      if (err instanceof ApiError) {
        setErrorDetails(err.message)
      } else {
        setErrorDetails(SAFE_NETWORK_ERROR_MESSAGE)
      }
      setState('error')
    }
  }, [client, loadRepositories])

  useEffect(() => {
    checkHealth()
  }, [checkHealth])

  // Polling for active jobs
  useEffect(() => {
    const unfinishedJobIds = Object.values(activeJobs)
      .filter((job) => job.status !== 'ready' && job.status !== 'failed')
      .map((job) => job.job_id)

    // Also check repos that are pending/indexing without an explicit job tracked
    const pendingRepoIds = repositories
      .filter((r) => r.status === 'pending' || r.status === 'indexing')
      .map((r) => r.repository_id)

    if (unfinishedJobIds.length === 0 && pendingRepoIds.length === 0) {
      return
    }

    const timer = setInterval(async () => {
      let statusChanged = false

      for (const jobId of unfinishedJobIds) {
        try {
          const updatedJob = await client.getIndexingJob(jobId)
          setActiveJobs((prev) => ({ ...prev, [jobId]: updatedJob }))
          if (updatedJob.status === 'ready' || updatedJob.status === 'failed') {
            statusChanged = true
            setAriaLiveMsg(`Indexing job ${jobId} finished with status ${updatedJob.status}.`)
          }
        } catch {
          // Ignore transient polling errors
        }
      }

      if (statusChanged || pendingRepoIds.length > 0) {
        await loadRepositories()
      }
    }, 2000)

    return () => {
      clearInterval(timer)
    }
  }, [activeJobs, repositories, client, loadRepositories])

  // Auto-select first repository if none selected
  useEffect(() => {
    if (!selectedRepoId && repositories.length > 0) {
      const readyRepo = repositories.find((r) => r.status === 'ready')
      const targetRepo = readyRepo || repositories[0]
      if (targetRepo) {
        setSelectedRepoId(targetRepo.repository_id)
      }
    }
  }, [repositories, selectedRepoId])

  const handleRetryRepo = async (repo: Repository) => {
    try {
      if (repo.source_type === 'github' && repo.github_url) {
        const res = await client.createGitHubRepository(repo.github_url, repo.index_mode)
        setRepositories((prev) => [
          res.repository,
          ...prev.filter((r) => r.repository_id !== res.repository.repository_id),
        ])
        setActiveJobs((prev) => ({ ...prev, [res.indexing_job.job_id]: res.indexing_job }))
        setSelectedRepoId(res.repository.repository_id)
        setAriaLiveMsg(`Retrying indexing for ${res.repository.name}.`)
      } else {
        const res = await client.refreshRepository(repo.repository_id)
        setActiveJobs((prev) => ({ ...prev, [res.indexing_job.job_id]: res.indexing_job }))
        setSelectedRepoId(repo.repository_id)
        setAriaLiveMsg(`Retrying indexing job for ${repo.name}.`)
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setGithubError(err.message)
      } else {
        setGithubError(SAFE_NETWORK_ERROR_MESSAGE)
      }
    }
  }

  const handleDeleteRepo = async (repoId: string) => {
    try {
      await client.deleteRepository(repoId)
      setRepositories((prev) => prev.filter((r) => r.repository_id !== repoId))
      if (selectedRepoId === repoId) {
        setSelectedRepoId(null)
      }
      setAriaLiveMsg('Repository deleted.')
    } catch {
      // Ignore quiet failure
    }
  }

  const handleGitHubSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setGithubError(null)

    const validationError = validateGitHubUrl(githubUrl)
    if (validationError) {
      setGithubError(validationError)
      return
    }

    setGithubSubmitting(true)
    try {
      const res = await client.createGitHubRepository(githubUrl.trim(), selectedIndexMode)
      setRepositories((prev) => [
        res.repository,
        ...prev.filter((r) => r.repository_id !== res.repository.repository_id),
      ])
      setActiveJobs((prev) => ({ ...prev, [res.indexing_job.job_id]: res.indexing_job }))
      setAriaLiveMsg(`Repository ${res.repository.name} accepted for indexing.`)
      setGithubUrl('')
    } catch (err) {
      if (err instanceof ApiError) {
        setGithubError(err.message)
      } else {
        setGithubError(SAFE_NETWORK_ERROR_MESSAGE)
      }
    } finally {
      setGithubSubmitting(false)
    }
  }

  const handleZipSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setZipError(null)

    const validationError = validateZipFile(zipFile)
    if (validationError) {
      setZipError(validationError)
      return
    }

    setZipSubmitting(true)
    try {
      const res = await client.uploadZipRepository(zipFile!, zipName.trim() || undefined, selectedIndexMode)
      setRepositories((prev) => [
        res.repository,
        ...prev.filter((r) => r.repository_id !== res.repository.repository_id),
      ])
      setActiveJobs((prev) => ({ ...prev, [res.indexing_job.job_id]: res.indexing_job }))
      setAriaLiveMsg(`ZIP repository ${res.repository.name} accepted for indexing.`)
      setZipFile(null)
      setZipName('')
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setZipError(err.message)
      } else {
        setZipError(SAFE_NETWORK_ERROR_MESSAGE)
      }
    } finally {
      setZipSubmitting(false)
    }
  }

  const handleEvidenceSearch = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedRepoId || !searchQuery.trim()) return

    setSearchLoading(true)
    setSearchError(null)
    try {
      const res = await client.searchEvidence(selectedRepoId, searchQuery.trim())
      setSearchResults(res.items)
    } catch (err) {
      setSearchResults([])
      if (err instanceof ApiError) {
        setSearchError(err.message)
      } else {
        setSearchError('Evidence search failed safely.')
      }
    } finally {
      setSearchLoading(false)
    }
  }

  const handleChatSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedRepoId || !chatQuestion.trim()) return

    setChatError(null)
    setChatSubmitting(true)
    const questionText = chatQuestion.trim()
    const activeConv = conversations[selectedRepoId]

    try {
      if (!activeConv) {
        const res = await client.createConversation(selectedRepoId, { question: questionText })
        setConversations((prev) => ({
          ...prev,
          [selectedRepoId]: {
            conversationId: res.conversation_id,
            messages: [res.user_message, res.assistant_message],
          },
        }))
      } else {
        const res = await client.sendMessage(selectedRepoId, activeConv.conversationId, {
          question: questionText,
        })
        setConversations((prev) => ({
          ...prev,
          [selectedRepoId]: {
            conversationId: res.conversation_id,
            messages: [...activeConv.messages, res.user_message, res.assistant_message],
          },
        }))
      }
      setChatQuestion('')
    } catch (err) {
      if (err instanceof ApiError) {
        setChatError(err.message)
      } else {
        setChatError(SAFE_NETWORK_ERROR_MESSAGE)
      }
    } finally {
      setChatSubmitting(false)
    }
  }

  const selectedRepo = repositories.find((r) => r.repository_id === selectedRepoId)
  const currentConversation = selectedRepoId ? conversations[selectedRepoId] : undefined

  return (
    <div className="app-container">
      <div aria-live="polite" className="sr-only">
        {ariaLiveMsg}
      </div>

      <header className="app-header">
        <div className="brand-section">
          <h1>SourceTrace</h1>
          <p className="subtitle">Evidence-grounded codebase intelligence</p>
        </div>
        <div className="status-indicator">
          {state === 'loading' && (
            <>
              <span className="status-dot loading" />
              <span>Checking API status...</span>
            </>
          )}
          {state === 'ready' && healthData && (
            <>
              <span className="status-dot healthy" />
              <span className="mono">API Online ({healthData.version})</span>
            </>
          )}
          {state === 'error' && (
            <>
              <span className="status-dot offline" />
              <span className="mono">API Offline</span>
            </>
          )}
        </div>
      </header>

      <div className="app-body">
        <aside className="trace-rail">
          <div className="rail-section">
            <span className="rail-label">Repositories</span>
            {repositories.length === 0 ? (
              <span className="rail-value mono">No repositories indexed yet.</span>
            ) : (
              <ul className="repo-list">
                {repositories.map((repo) => {
                  const job = Object.values(activeJobs).find(
                    (j) => j.repository_id === repo.repository_id,
                  )
                  const isSelected = selectedRepoId === repo.repository_id
                  const isReady = repo.status === 'ready'

                  return (
                    <li key={repo.repository_id}>
                      <button
                        type="button"
                        className={`repo-card ${isSelected ? 'selected' : ''}`}
                        onClick={() => {
                          setSelectedRepoId(repo.repository_id)
                        }}
                      >
                        <div className="repo-header">
                          <span className="repo-title">{repo.name}</span>
                          <span className={`repo-badge ${job?.status || repo.status}`}>
                            {job?.status || repo.status}
                          </span>
                        </div>
                        <div className="repo-meta mono">
                          <span>{repo.source_type.toUpperCase()}</span>
                          {isReady && <span>{repo.file_count} files</span>}
                        </div>
                        {job && job.status !== 'ready' && job.status !== 'failed' && (
                          <div className="progress-track">
                            <div
                              className="progress-fill"
                              style={{ width: `${job.progress_percentage}%` }}
                            />
                          </div>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          <div className="rail-section">
            <span className="rail-label">Evidence rule</span>
            <span className="rail-value">Citations strictly grounded in indexed code</span>
          </div>

          <div className="rail-section">
            <span className="rail-label">Current service status</span>
            <span className="rail-value mono">
              {state === 'loading' && 'Checking...'}
              {state === 'ready' && (healthData ? `Healthy (${healthData.status})` : 'Healthy')}
              {state === 'error' && 'Unavailable'}
            </span>
          </div>
        </aside>

        <main className="main-workspace">
          {state === 'loading' && (
            <section className="card-panel">
              <h2 className="panel-header">Checking the API boundary</h2>
              <p className="panel-text">Connecting to backend health service...</p>
            </section>
          )}

          {state === 'ready' && (
            <>
              <section className="card-panel">
                <h2 className="panel-header">API Boundary Reachable</h2>
                <p className="panel-text">
                  Import a public repository to begin evidence-grounded analysis.
                </p>

                <div className="form-group" style={{ marginBottom: '16px', maxWidth: '300px' }}>
                  <label htmlFor="index-mode-select" className="form-label">
                    Indexing Mode
                  </label>
                  <select
                    id="index-mode-select"
                    className="input-text"
                    value={selectedIndexMode}
                    onChange={(e) => setSelectedIndexMode(e.target.value as IndexMode)}
                  >
                    <option value="static">Static Inspection (No AI Tokens)</option>
                    {capabilities?.semantic_search_available && (
                      <option value="cloud_ai">Cloud AI (Embeddings + LLM)</option>
                    )}
                  </select>
                </div>

                <div className="import-grid">
                  {/* GitHub Form */}
                  <form onSubmit={handleGitHubSubmit} className="import-form">
                    <div className="form-group">
                      <label htmlFor="github-url-input" className="form-label">
                        Public GitHub Repository URL
                      </label>
                      <input
                        id="github-url-input"
                        type="text"
                        className="input-text"
                        placeholder="https://github.com/owner/repo"
                        value={githubUrl}
                        onChange={(e) => setGithubUrl(e.target.value)}
                        disabled={githubSubmitting}
                      />
                      {githubError && <div className="form-error">{githubError}</div>}
                    </div>
                    <button
                      type="submit"
                      disabled={githubSubmitting}
                      className="btn-action"
                    >
                      {githubSubmitting ? 'Importing...' : 'Import GitHub Repository'}
                    </button>
                  </form>

                  {/* ZIP Upload Form */}
                  <form onSubmit={handleZipSubmit} className="import-form">
                    <div className="form-group">
                      <label htmlFor="zip-file-input" className="form-label">
                        ZIP Archive File (.zip)
                      </label>
                      <input
                        ref={fileInputRef}
                        id="zip-file-input"
                        type="file"
                        accept=".zip,application/zip,application/x-zip-compressed"
                        className="input-file"
                        onChange={(e) => {
                          const file = e.target.files?.[0] || null
                          setZipFile(file)
                        }}
                        disabled={zipSubmitting}
                      />
                    </div>
                    <div className="form-group">
                      <label htmlFor="zip-name-input" className="form-label">
                        Display Name (Optional)
                      </label>
                      <input
                        id="zip-name-input"
                        type="text"
                        className="input-text"
                        placeholder="My Project"
                        value={zipName}
                        onChange={(e) => setZipName(e.target.value)}
                        disabled={zipSubmitting}
                      />
                      {zipError && <div className="form-error">{zipError}</div>}
                    </div>
                    <button
                      type="submit"
                      disabled={zipSubmitting}
                      className="btn-action"
                    >
                      {zipSubmitting ? 'Uploading...' : 'Upload ZIP Archive'}
                    </button>
                  </form>
                </div>
              </section>

              {/* Selected Repo Search, Chat, Failed Error Display, or Empty Status */}
              {selectedRepo && selectedRepo.status === 'failed' ? (
                <section className="card-panel error-panel">
                  <h2 className="panel-header" style={{ color: 'var(--color-danger)' }}>
                    Indexing Failed: <span className="mono">{selectedRepo.name}</span>
                  </h2>
                  <div className="failed-details" style={{ marginTop: '0.75rem', marginBottom: '1rem' }}>
                    <p className="panel-text">
                      <strong>Failed stage:</strong>{' '}
                      <span className="mono">
                        {Object.values(activeJobs).find((j) => j.repository_id === selectedRepo.repository_id)?.current_step ||
                          'Acquisition / Indexing'}
                      </span>
                    </p>
                    <p className="panel-text">
                      <strong>Safe error:</strong>{' '}
                      {getSafeErrorCategory(
                        Object.values(activeJobs).find((j) => j.repository_id === selectedRepo.repository_id)?.error_message,
                        Object.values(activeJobs).find((j) => j.repository_id === selectedRepo.repository_id)?.current_step,
                      )}
                    </p>
                  </div>
                  <div className="action-row" style={{ display: 'flex', gap: '0.5rem' }}>
                    <button
                      type="button"
                      className="btn-action btn-retry"
                      onClick={() => handleRetryRepo(selectedRepo)}
                    >
                      Retry Indexing
                    </button>
                    <button
                      type="button"
                      className="btn-action"
                      style={{ backgroundColor: 'transparent', border: '1px solid var(--color-border)' }}
                      onClick={() => handleDeleteRepo(selectedRepo.repository_id)}
                    >
                      Delete Repository
                    </button>
                  </div>
                </section>
              ) : selectedRepo && selectedRepo.status === 'ready' && selectedRepo.index_mode === 'static' ? (
                <section className="card-panel search-panel">
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                    <h2 className="panel-header" style={{ margin: 0 }}>
                      Repository: <span className="mono">{selectedRepo.name}</span>
                    </h2>
                    <span className="static-banner" style={{ background: '#0e2a35', color: '#38bdf8', border: '1px solid #0284c7', padding: '4px 12px', borderRadius: '12px', fontSize: '0.8rem', fontWeight: 600 }}>
                      Static inspection — no AI token required.
                    </span>
                  </div>
                  <p className="panel-text" style={{ marginBottom: '16px' }}>
                    Search code symbols, classes, functions, and relative file paths without AI tokens.
                  </p>

                  <form onSubmit={handleEvidenceSearch} className="search-form" style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
                    <input
                      type="text"
                      className="input-text chat-input"
                      placeholder="Search symbol name or relative path (e.g. User, parse_data, models/domain.py)..."
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      disabled={searchLoading}
                    />
                    <button
                      type="submit"
                      disabled={searchLoading || !searchQuery.trim()}
                      className="btn-action"
                    >
                      {searchLoading ? 'Searching...' : 'Search Evidence'}
                    </button>
                  </form>

                  {searchError && <div className="form-error" style={{ marginBottom: '16px' }}>{searchError}</div>}

                  {searchResults !== null && (
                    <div className="search-results">
                      <h3 style={{ fontSize: '0.9rem', color: '#9ca3af', marginBottom: '12px' }}>
                        Evidence Results ({searchResults.length}):
                      </h3>
                      {searchResults.length === 0 ? (
                        <p className="panel-text" style={{ fontStyle: 'italic' }}>
                          No code evidence matches "{searchQuery}".
                        </p>
                      ) : (
                        searchResults.map((item) => (
                          <div key={item.chunk_id} className="search-card" style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: '8px', padding: '12px', marginBottom: '12px' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px', fontSize: '0.85rem' }}>
                              <span className="mono" style={{ color: '#38bdf8' }}>
                                {item.relative_path}:{item.start_line}-{item.end_line}
                              </span>
                              <span style={{ color: '#94a3b8' }}>
                                {item.symbol_type} <strong>{item.symbol_name}</strong>
                              </span>
                            </div>
                            <pre className="evidence-block" style={{ margin: 0, padding: '8px', background: '#020617', borderRadius: '4px', overflowX: 'auto', fontSize: '0.8rem' }}>
                              <code>{item.snippet}</code>
                            </pre>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </section>
              ) : selectedRepo && selectedRepo.status === 'ready' ? (
                <section className="card-panel chat-panel">
                  <h2 className="panel-header">
                    Repository: <span className="mono">{selectedRepo.name}</span>
                  </h2>
                  <p className="panel-text">
                    Ask natural-language questions grounded in verified source code.
                  </p>

                  <div className="chat-history">
                    {currentConversation?.messages.map((msg) => (
                      <div
                        key={msg.message_id}
                        className={`chat-bubble ${msg.role === 'user' ? 'user' : 'assistant'}`}
                      >
                        <div className="bubble-role">
                          {msg.role === 'user' ? 'Question' : 'Assistant (Grounded Evidence)'}
                        </div>
                        <div className="bubble-content">{msg.content}</div>

                        {msg.role === 'assistant' && msg.citations && msg.citations.length > 0 && (
                          <div className="citations-list">
                            <span className="citation-heading">Citations:</span>
                            {msg.citations.map((c, idx) => (
                              <span key={idx} className="citation-item">
                                {c.relative_path}:{c.start_line}-{c.end_line} ({c.symbol_name})
                              </span>
                            ))}
                          </div>
                        )}

                        {msg.role === 'assistant' && msg.evidence && msg.evidence.length > 0 && (
                          <div className="citations-list">
                            <span className="citation-heading">Evidence Snippets:</span>
                            {msg.evidence.map((e, idx) => (
                              <pre key={idx} className="evidence-block">
                                <code>
                                  {e.relative_path}:{e.start_line}-{e.end_line} [{e.symbol_name}]:
                                  {'\n'}
                                  {e.snippet}
                                </code>
                              </pre>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  <form onSubmit={handleChatSubmit} className="chat-form">
                    <input
                      type="text"
                      className="input-text chat-input"
                      placeholder="Ask a question about this codebase..."
                      value={chatQuestion}
                      onChange={(e) => setChatQuestion(e.target.value)}
                      disabled={chatSubmitting}
                    />
                    <button
                      type="submit"
                      disabled={chatSubmitting || !chatQuestion.trim()}
                      className="btn-action"
                    >
                      {chatSubmitting ? 'Asking...' : 'Send Question'}
                    </button>
                  </form>
                  {chatError && <div className="form-error">{chatError}</div>}
                </section>
              ) : (
                <section className="state-box">
                  <strong>Repository status:</strong>{' '}
                  {repositories.length === 0
                    ? 'No repositories imported yet. Enter a GitHub URL or select a ZIP archive above.'
                    : 'Select a ready repository from the sidebar to inspect or search.'}
                </section>
              )}

              {selectedRepo && selectedRepo.status === 'ready' && (
                <FlowTracePanel
                  client={client}
                  repositoryId={selectedRepo.repository_id}
                  repositoryName={selectedRepo.name}
                />
              )}
            </>
          )}

          {state === 'error' && (
            <section className="card-panel">
              <h2 className="panel-header" style={{ color: 'var(--color-danger)' }}>
                API Boundary Unavailable
              </h2>
              <p className="panel-text">{errorDetails ?? SAFE_NETWORK_ERROR_MESSAGE}</p>
              <button type="button" onClick={checkHealth} className="btn-action btn-retry">
                Retry API Health Check
              </button>
            </section>
          )}
        </main>
      </div>

      <footer className="app-footer">
        <p>Evidence is shown only when retrieved from source.</p>
      </footer>
    </div>
  )
}

export default App
