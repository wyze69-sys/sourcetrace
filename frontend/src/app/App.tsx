import { useCallback, useEffect, useRef, useState } from 'react'
import { apiClient as defaultApiClient, ApiError, type ApiClient } from '../services/apiClient'
import { FlowTracePanel } from './FlowTracePanel'
import { ImpactPanel } from './ImpactPanel'
import { RepoExplorerPanel } from './RepoExplorerPanel'
import type {
  EvidenceSearchItem,
  HealthResponse,
  IndexingJob,
  Message,
  Repository,
  ServerCapabilities,
} from '../services/types'

export interface AppProps {
  client?: ApiClient
}

type AppState = 'loading' | 'ready' | 'error'
export type WorkspaceSection = 'understand' | 'files' | 'find_code' | 'flow_trace' | 'change_impact'

const SAFE_NETWORK_ERROR_MESSAGE =
  'Unable to reach the SourceTrace API. Check that the local service is running and try again.'

const STARTER_QUESTIONS = [
  'Where does the application start?',
  'How does authentication work?',
  'Where is data stored?',
  'What should I read first?',
]

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

export function formatRelativeTime(isoString?: string | null): string {
  if (!isoString) return 'never'
  try {
    const date = new Date(isoString)
    if (isNaN(date.getTime())) return 'never'
    const diffSeconds = Math.floor((Date.now() - date.getTime()) / 1000)
    if (diffSeconds < 0 || diffSeconds < 30) return 'just now'
    if (diffSeconds < 60) return `${diffSeconds}s ago`
    if (diffSeconds < 3600) {
      const mins = Math.floor(diffSeconds / 60)
      return `${mins}m ago`
    }
    if (diffSeconds < 86400) {
      const hours = Math.floor(diffSeconds / 3600)
      return `${hours}h ago`
    }
    const days = Math.floor(diffSeconds / 86400)
    return `${days}d ago`
  } catch {
    return 'never'
  }
}

export type FreshnessState = 'fresh' | 'stale' | 'unknown' | 'refreshing'

export function getFreshnessState(repo: Repository, activeJob?: IndexingJob): FreshnessState {
  if (
    activeJob &&
    activeJob.job_type === 'refresh' &&
    activeJob.status !== 'ready' &&
    activeJob.status !== 'failed'
  ) {
    return 'refreshing'
  }
  if (repo.source_type !== 'github') {
    return 'unknown'
  }
  if (repo.is_stale === true) {
    return 'stale'
  }
  if (repo.is_stale === false) {
    return 'fresh'
  }
  return 'unknown'
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
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try {
      return localStorage.getItem('sourcetrace-theme') === 'dark' ? 'dark' : 'light'
    } catch {
      return 'light'
    }
  })
  const [state, setState] = useState<AppState>('loading')
  const [healthData, setHealthData] = useState<HealthResponse | null>(null)
  const [errorDetails, setErrorDetails] = useState<string | null>(null)
  const [capabilities, setCapabilities] = useState<ServerCapabilities | null>(null)
  const [selectedIndexMode, setSelectedIndexMode] = useState<'static' | 'ai_assist'>('static')

  // Repositories & Indexing jobs state
  const [repositories, setRepositories] = useState<Repository[]>([])
  const [activeJobs, setActiveJobs] = useState<Record<string, IndexingJob>>({})
  const [selectedRepoId, setSelectedRepoId] = useState<string | null>(null)
  const [ariaLiveMsg, setAriaLiveMsg] = useState<string>('')
  const [showImportTray, setShowImportTray] = useState<boolean>(false)

  // Workspace Navigation state
  const [activeSection, setActiveSection] = useState<WorkspaceSection>('understand')
  const [isAdvancedExpanded, setIsAdvancedExpanded] = useState<boolean>(false)

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

  // Chat / Understand State
  const [conversations, setConversations] = useState<
    Record<string, { conversationId: string; messages: Message[] }>
  >({})
  const [chatQuestion, setChatQuestion] = useState('')
  const [chatSubmitting, setChatSubmitting] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)

  // Focused Citation / Navigation State
  const [focusedCitation, setFocusedCitation] = useState<{
    filePath: string
    startLine: number
    endLine: number
  } | null>(null)

  // Command palette
  const [cmdkOpen, setCmdkOpen] = useState(false)
  const [cmdkQuery, setCmdkQuery] = useState('')
  const [cmdkSelectedIdx, setCmdkSelectedIdx] = useState(0)
  const cmdkInputRef = useRef<HTMLInputElement>(null)

  // Toast notifications
  type Toast = { id: number; kind: 'success' | 'error' | 'info'; message: string }
  const [toasts, setToasts] = useState<Toast[]>([])

  const pushToast = (kind: Toast['kind'], message: string) => {
    const id = Date.now() + Math.random()
    setToasts((prev) => [...prev.slice(-3), { id, kind, message }])
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, 5200)
  }


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
        if (caps.generation_available) {
          setSelectedIndexMode('ai_assist')
        } else {
          setSelectedIndexMode('static')
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

  // Apply and persist theme
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem('sourcetrace-theme', theme)
    } catch {
      // storage unavailable
    }
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))

  // Command palette: Ctrl/Cmd+K to open
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setCmdkOpen((prev) => !prev)
        setCmdkQuery('')
        setCmdkSelectedIdx(0)
      } else if (e.key === 'Escape') {
        setCmdkOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (cmdkOpen) {
      window.setTimeout(() => cmdkInputRef.current?.focus(), 10)
    }
  }, [cmdkOpen])

  const freshnessCheckedRepoRef = useRef<Set<string>>(new Set())

  // Polling for active jobs
  useEffect(() => {
    const unfinishedJobIds = Object.values(activeJobs)
      .filter((job) => job.status !== 'ready' && job.status !== 'failed')
      .map((job) => job.job_id)

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
            if (updatedJob.status === 'ready') {
              pushToast('success', `Indexing complete. Repository is ready.`)
            } else if (updatedJob.status === 'failed') {
              pushToast('error', `Indexing failed. Check the repository card for details.`)
            }
            freshnessCheckedRepoRef.current.delete(updatedJob.repository_id)
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJobs, repositories, client, loadRepositories])

  // Auto-select first repository if none selected & default workspace to Understand
  useEffect(() => {
    if (!selectedRepoId && repositories.length > 0) {
      const readyRepo = repositories.find((r) => r.status === 'ready')
      const targetRepo = readyRepo || repositories[0]
      if (targetRepo) {
        setSelectedRepoId(targetRepo.repository_id)
        if (targetRepo.status === 'ready') {
          setActiveSection('understand')
        }
      }
    }
  }, [repositories, selectedRepoId])

  // Select repository helper that resets active workspace to Understand for ready repos and closes import tray
  const handleSelectRepo = (repoId: string) => {
    setSelectedRepoId(repoId)
    setShowImportTray(false)
    setFocusedCitation(null)
    const repo = repositories.find((r) => r.repository_id === repoId)
    if (!repo || repo.status === 'ready') {
      setActiveSection('understand')
    }
  }

  // Bounded opt-in freshness check once upon selecting a ready GitHub repository
  useEffect(() => {
    if (!selectedRepoId) return
    const repo = repositories.find((r) => r.repository_id === selectedRepoId)
    if (
      repo &&
      repo.status === 'ready' &&
      repo.source_type === 'github' &&
      !freshnessCheckedRepoRef.current.has(selectedRepoId) &&
      typeof client.getRepository === 'function'
    ) {
      freshnessCheckedRepoRef.current.add(selectedRepoId)
      client
        .getRepository(selectedRepoId, true)
        .then((updatedRepo) => {
          setRepositories((prev) =>
            prev.map((r) => (r.repository_id === updatedRepo.repository_id ? updatedRepo : r)),
          )
        })
        .catch(() => {
          // Ignore transient error; freshness status stays as-is
        })
    }
  }, [selectedRepoId, repositories, client])

  const handleRefreshRepo = async (repo: Repository) => {
    try {
      const res = await client.refreshRepository(repo.repository_id)
      setActiveJobs((prev) => ({ ...prev, [res.indexing_job.job_id]: res.indexing_job }))
      setAriaLiveMsg(`Refresh started for ${repo.name}.`)
      pushToast('info', `Refreshing ${repo.name}…`)
    } catch (err) {
      if (err instanceof ApiError) {
        setGithubError(err.message)
      } else {
        setGithubError(SAFE_NETWORK_ERROR_MESSAGE)
      }
    }
  }

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
        setActiveSection('understand')
        setAriaLiveMsg(`Retrying indexing for ${res.repository.name}.`)
      } else {
        const res = await client.refreshRepository(repo.repository_id)
        setActiveJobs((prev) => ({ ...prev, [res.indexing_job.job_id]: res.indexing_job }))
        setSelectedRepoId(repo.repository_id)
        setActiveSection('understand')
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

  const [deletingRepoId, setDeletingRepoId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteSuccess, setDeleteSuccess] = useState<string | null>(null)

  const handleDeleteRepo = async (repoId: string) => {
    setDeletingRepoId(repoId)
    setDeleteError(null)
    setDeleteSuccess(null)
    const repoName = repositories.find((r) => r.repository_id === repoId)?.name ?? 'Repository'
    try {
      const res = await client.deleteRepository(repoId)
      setRepositories((prev) => prev.filter((r) => r.repository_id !== repoId))
      if (selectedRepoId === repoId) {
        setSelectedRepoId(null)
      }
      setDeleteSuccess(res.message || 'Repository deleted successfully.')
      setAriaLiveMsg('Repository deleted.')
      pushToast('success', `${repoName} deleted. Quota slot freed.`)
    } catch (err) {
      if (err instanceof ApiError) {
        setDeleteError(err.message)
      } else {
        setDeleteError(SAFE_NETWORK_ERROR_MESSAGE)
      }
    } finally {
      setDeletingRepoId(null)
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
      setSelectedRepoId(res.repository.repository_id)
      setActiveSection('understand')
      setAriaLiveMsg(`Repository ${res.repository.name} accepted for indexing.`)
      pushToast('success', `${res.repository.name} accepted. Indexing started.`)
      setGithubUrl('')
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 429 || err.code === 'QUOTA_EXCEEDED') {
          setGithubError('Repository limit reached (3 max). Delete an existing repository before importing another.')
        } else {
          setGithubError(err.message)
        }
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
      setSelectedRepoId(res.repository.repository_id)
      setActiveSection('understand')
      setAriaLiveMsg(`ZIP repository ${res.repository.name} accepted for indexing.`)
      setZipFile(null)
      setZipName('')
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 429 || err.code === 'QUOTA_EXCEEDED') {
          setZipError('Repository limit reached (3 max). Delete an existing repository before importing another.')
        } else {
          setZipError(err.message)
        }
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

  const submitQuestion = async (text: string) => {
    if (!selectedRepoId || !text.trim() || chatSubmitting) return

    setChatError(null)
    setChatSubmitting(true)
    const questionText = text.trim()
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

  const handleChatSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    await submitQuestion(chatQuestion)
  }

  const selectedRepo = repositories.find((r) => r.repository_id === selectedRepoId)
  const currentConversation = selectedRepoId ? conversations[selectedRepoId] : undefined

  // Command palette items: repositories + workspace sections + starter questions
  const cmdkItems = (() => {
    const items: Array<{
      id: string
      label: string
      hint: string
      type: 'repo' | 'section' | 'question'
      action: () => void
    }> = []

    repositories.forEach((repo) => {
      items.push({
        id: `repo-${repo.repository_id}`,
        label: repo.name,
        hint: repo.status === 'ready' ? `${repo.source_type} , ready` : repo.status,
        type: 'repo',
        action: () => {
          handleSelectRepo(repo.repository_id)
          setCmdkOpen(false)
        },
      })
    })

    if (selectedRepo) {
      const sections: Array<{ id: WorkspaceSection; label: string; hint: string }> = [
        { id: 'understand', label: 'Understand (chat)', hint: 'Ask & answer with citations' },
        { id: 'files', label: 'Files explorer', hint: 'Browse indexed source tree' },
        { id: 'find_code', label: 'Find code', hint: 'Semantic/lexical evidence search' },
        { id: 'flow_trace', label: 'Flow trace', hint: 'Call & data flow for a symbol' },
        { id: 'change_impact', label: 'Change impact', hint: 'What a change would touch' },
      ]
      sections.forEach((s) => {
        items.push({
          id: `section-${s.id}`,
          label: s.label,
          hint: s.hint,
          type: 'section',
          action: () => {
            setActiveSection(s.id)
            setCmdkOpen(false)
          },
        })
      })

      STARTER_QUESTIONS.forEach((q) => {
        items.push({
          id: `q-${q}`,
          label: q,
          hint: 'starter question',
          type: 'question',
          action: () => {
            setCmdkOpen(false)
            setActiveSection('understand')
            submitQuestion(q)
          },
        })
      })
    }

    const ql = cmdkQuery.trim().toLowerCase()
    if (!ql) return items
    return items.filter(
      (i) =>
        i.label.toLowerCase().includes(ql) ||
        i.hint.toLowerCase().includes(ql),
    )
  })()

  return (
    <div className="app-container">
      <div aria-live="polite" className="sr-only">
        {ariaLiveMsg}
      </div>

      <header className="app-header">
        <div className="brand-section">
          <h1>SourceTrace</h1>
          <p className="subtitle">Understand a codebase before you change it.</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          {repositories.length > 0 && (
            <button
              type="button"
              className="btn-action btn-import-toggle"
              onClick={() => setShowImportTray((prev) => !prev)}
            >
              {showImportTray ? 'Close Import' : 'Import repository'}
            </button>
          )}
          <button
            type="button"
            className="theme-toggle"
            onClick={toggleTheme}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            title={theme === 'dark' ? 'Light theme' : 'Dark theme'}
          >
            <span className="theme-icon" aria-hidden="true">{theme === 'dark' ? '☼' : '☾'}</span>
            <span className="sr-only">{theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}</span>
          </button>
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
                        onClick={() => handleSelectRepo(repo.repository_id)}
                      >
                        <div className="repo-header">
                          <span className="repo-title">{repo.name}</span>
                          <span
                            className={`repo-badge ${
                              isReady
                                ? repo.source_type === 'github'
                                  ? getFreshnessState(repo, job)
                                  : 'ready'
                                : job?.status || repo.status
                            }`}
                          >
                            {isReady
                              ? repo.source_type === 'github'
                                ? getFreshnessState(repo, job)
                                : 'ready'
                              : job?.status || repo.status}
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

          {selectedRepo && selectedRepo.status === 'ready' && (
            <div className="rail-section rail-nav-section">
              <span className="rail-label">Workspace</span>
              <nav className="rail-workspace-nav" aria-label="Workspace Navigation">
                <button
                  type="button"
                  className={`rail-nav-btn ${activeSection === 'understand' ? 'active' : ''}`}
                  aria-current={activeSection === 'understand' ? 'page' : undefined}
                  onClick={() => {
                    setActiveSection('understand')
                    setShowImportTray(false)
                  }}
                >
                  Understand
                </button>
                <button
                  type="button"
                  className={`rail-nav-btn ${activeSection === 'files' ? 'active' : ''}`}
                  aria-current={activeSection === 'files' ? 'page' : undefined}
                  onClick={() => {
                    setActiveSection('files')
                    setShowImportTray(false)
                  }}
                >
                  Files
                </button>
                <button
                  type="button"
                  className={`rail-nav-btn ${activeSection === 'find_code' ? 'active' : ''}`}
                  aria-current={activeSection === 'find_code' ? 'page' : undefined}
                  onClick={() => {
                    setActiveSection('find_code')
                    setShowImportTray(false)
                  }}
                >
                  Find code
                </button>
                <div className="rail-nav-accordion">
                  <button
                    type="button"
                    className="rail-accordion-header"
                    aria-expanded={isAdvancedExpanded}
                    aria-controls="rail-advanced-menu"
                    onClick={() => setIsAdvancedExpanded((prev) => !prev)}
                  >
                    <span>Advanced analysis</span>
                    <span className="accordion-icon">{isAdvancedExpanded ? '▲' : '▼'}</span>
                  </button>
                  {isAdvancedExpanded && (
                    <div id="rail-advanced-menu" className="rail-accordion-body">
                      <button
                        type="button"
                        className={`rail-nav-subbtn ${activeSection === 'flow_trace' ? 'active' : ''}`}
                        aria-current={activeSection === 'flow_trace' ? 'page' : undefined}
                        onClick={() => {
                          setActiveSection('flow_trace')
                          setShowImportTray(false)
                        }}
                      >
                        Show how this works
                      </button>
                      <button
                        type="button"
                        className={`rail-nav-subbtn ${activeSection === 'change_impact' ? 'active' : ''}`}
                        aria-current={activeSection === 'change_impact' ? 'page' : undefined}
                        onClick={() => {
                          setActiveSection('change_impact')
                          setShowImportTray(false)
                        }}
                      >
                        What could this change affect?
                      </button>
                    </div>
                  )}
                </div>
              </nav>
            </div>
          )}
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
              {repositories.length === 0 && (
                <section className="hero-first-run" aria-labelledby="hero-title">
                  <div className="hero-eyebrow">Evidence-grounded code Q&amp;A on Vite, React, and MongoDB</div>
                  <h2 id="hero-title">Understand a codebase before you change it</h2>
                  <p className="hero-lede">
                    Import a repository and SourceTrace reads the code, indexes it with static
                    analysis, then answers questions with citations from real source lines, not
                    vague guesses.
                  </p>
                  <div className="hero-feature-grid">
                    <div className="hero-feature-card">
                      <span className="hero-feature-icon text-glyph" aria-hidden>Ask</span>
                      <h3>Ask, get cited answers</h3>
                      <p>Question the codebase; answers point to exact files and line ranges.</p>
                    </div>
                    <div className="hero-feature-card">
                      <span className="hero-feature-icon text-glyph" aria-hidden>Cite</span>
                      <h3>Open citations to the line</h3>
                      <p>Click a citation to jump straight to the source with lines highlighted.</p>
                    </div>
                    <div className="hero-feature-card">
                      <span className="hero-feature-icon text-glyph" aria-hidden>Tree</span>
                      <h3>Trace impact &amp; flow</h3>
                      <p>See what changes break, and how data and calls flow through code.</p>
                    </div>
                    <div className="hero-feature-card">
                      <span className="hero-feature-icon text-glyph" aria-hidden>Fast</span>
                      <h3>Freshness-aware</h3>
                      <p>Know when a repository has drifted from what was indexed.</p>
                    </div>
                  </div>
                </section>
              )}
              {deleteSuccess && (
                <div
                  className="form-success"
                  style={{
                    marginBottom: '16px',
                    color: '#065f46',
                    background: '#d1fae5',
                    border: '1px solid #a7f3d0',
                    padding: '8px 12px',
                    borderRadius: '6px',
                  }}
                >
                  {deleteSuccess}
                </div>
              )}
              {deleteError && (
                <div className="form-error" style={{ marginBottom: '16px' }}>
                  {deleteError}
                </div>
              )}

              {(showImportTray || repositories.length === 0) && (
                <section className="card-panel import-tray-panel" aria-label="Repository Import">
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginBottom: '12px',
                    }}
                  >
                    <h2 className="panel-header" style={{ margin: 0 }}>
                      {repositories.length === 0
                        ? 'Import a Repository to Begin'
                        : 'Import Repository'}
                    </h2>
                    {repositories.length > 0 && (
                      <button
                        type="button"
                        className="btn-action"
                        style={{ fontSize: '0.8rem', padding: '4px 10px' }}
                        onClick={() => setShowImportTray(false)}
                      >
                        [x] Close
                      </button>
                    )}
                  </div>
                  <p className="panel-text">
                    Import a public GitHub repository URL or ZIP archive to begin evidence-grounded analysis.
                  </p>

                  <div className="form-group" style={{ marginBottom: '16px', maxWidth: '400px' }}>
                    <label htmlFor="index-mode-select" className="form-label">
                      Indexing Mode
                    </label>
                    <select
                      id="index-mode-select"
                      className="input-text"
                      value={selectedIndexMode}
                      onChange={(e) => setSelectedIndexMode(e.target.value as 'static' | 'ai_assist')}
                    >
                      <option value="static">Static</option>
                      {capabilities?.generation_available && (
                        <option value="ai_assist">AI Assist (Free)</option>
                      )}
                    </select>
                    <div
                      className="form-help-text"
                      style={{ marginTop: '6px', fontSize: '0.85rem', color: 'var(--color-muted)' }}
                    >
                      {capabilities?.generation_available
                        ? 'AI Assist uses semantic embeddings during indexing and grounded generation when you ask questions.'
                        : 'AI explanation assist unavailable (no LLM generation capability).'}
                    </div>
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
                      <button type="submit" disabled={githubSubmitting} className="btn-action">
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
                      <button type="submit" disabled={zipSubmitting} className="btn-action">
                        {zipSubmitting ? 'Uploading...' : 'Upload ZIP Archive'}
                      </button>
                    </form>
                  </div>
                </section>
              )}

              {/* Selected Ready Repository Workspace Views */}
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
                      disabled={deletingRepoId === selectedRepo.repository_id}
                    >
                      {deletingRepoId === selectedRepo.repository_id ? 'Deleting...' : 'Delete Repository'}
                    </button>
                  </div>
                </section>
              ) : selectedRepo && selectedRepo.status === 'ready' && !showImportTray ? (
                <div className="workspace-container">


                  {/* Active Workspace Content Views */}
                  {activeSection === 'understand' && (
                    <section className="card-panel understand-panel">
                      <div className="repo-meta-header" style={{ marginBottom: '16px' }}>
                        <div
                          style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            marginBottom: '8px',
                          }}
                        >
                          <h2 className="panel-header" style={{ margin: 0 }}>
                            Repository: <span className="mono">{selectedRepo.name}</span>
                            {selectedRepo.source_type === 'github' && (
                              <span
                                className={`repo-badge ${getFreshnessState(
                                  selectedRepo,
                                  Object.values(activeJobs).find(
                                    (j) => j.repository_id === selectedRepo.repository_id,
                                  ),
                                )}`}
                                style={{
                                  marginLeft: '10px',
                                  fontSize: '0.75rem',
                                  verticalAlign: 'middle',
                                }}
                              >
                                {getFreshnessState(
                                  selectedRepo,
                                  Object.values(activeJobs).find(
                                    (j) => j.repository_id === selectedRepo.repository_id,
                                  ),
                                )}
                              </span>
                            )}
                          </h2>
                          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                            {selectedRepo.source_type === 'github' && (
                              <button
                                id="btn-refresh-repo-understand"
                                type="button"
                                className="btn-action"
                                style={{ fontSize: '0.8rem', padding: '4px 12px' }}
                                onClick={() => handleRefreshRepo(selectedRepo)}
                                disabled={
                                  !!Object.values(activeJobs).find(
                                    (j) =>
                                      j.repository_id === selectedRepo.repository_id &&
                                      j.status !== 'ready' &&
                                      j.status !== 'failed',
                                  )
                                }
                                title={
                                  selectedRepo.indexed_commit_sha
                                    ? `Last indexed SHA: ${selectedRepo.indexed_commit_sha.slice(0, 7)}`
                                    : 'Re-index this repository'
                                }
                              >
                                ↻ Refresh
                              </button>
                            )}
                            <button
                              type="button"
                              className="btn-action"
                              style={{
                                fontSize: '0.8rem',
                                padding: '4px 12px',
                                backgroundColor: 'transparent',
                                border: '1px solid var(--color-border)',
                              }}
                              onClick={() => handleDeleteRepo(selectedRepo.repository_id)}
                              disabled={deletingRepoId === selectedRepo.repository_id}
                            >
                              {deletingRepoId === selectedRepo.repository_id ? 'Deleting...' : 'Delete Repository'}
                            </button>
                            {selectedRepo.index_mode === 'static' ? (
                              <span
                                className="static-banner"
                                style={{
                                  background: '#f0f9ff',
                                  color: '#0284c7',
                                  border: '1px solid #bae6fd',
                                  padding: '4px 12px',
                                  borderRadius: '12px',
                                  fontSize: '0.8rem',
                                  fontWeight: 600,
                                }}
                              >
                                Static inspection mode
                              </span>
                            ) : (
                              capabilities?.generation_available &&
                              !capabilities?.semantic_search_available && (
                                <span
                                  className="static-chat-badge"
                                  style={{
                                    background: '#f0f9ff',
                                    color: '#0284c7',
                                    border: '1px solid #bae6fd',
                                    padding: '4px 12px',
                                    borderRadius: '12px',
                                    fontSize: '0.8rem',
                                    fontWeight: 600,
                                  }}
                                >
                                  AI Assist (Static Evidence Mode)
                                </span>
                              )
                            )}
                          </div>
                        </div>
                        {selectedRepo.source_type === 'github' && (
                          <div
                            className="repo-meta-bar mono"
                            style={{
                              fontSize: '0.78rem',
                              color: 'var(--color-muted)',
                              display: 'flex',
                              gap: '16px',
                              flexWrap: 'wrap',
                            }}
                          >
                            {selectedRepo.indexed_branch && (
                              <span>
                                Branch: <strong>{selectedRepo.indexed_branch}</strong>
                              </span>
                            )}
                            {selectedRepo.indexed_commit_sha && (
                              <span>
                                SHA: <strong>{selectedRepo.indexed_commit_sha.slice(0, 7)}</strong>
                              </span>
                            )}
                            {selectedRepo.last_indexed_at && (
                              <span>
                                Indexed: <strong>{formatRelativeTime(selectedRepo.last_indexed_at)}</strong>
                              </span>
                            )}
                            {selectedRepo.stale_checked_at && (
                              <span>
                                Checked: <strong>{formatRelativeTime(selectedRepo.stale_checked_at)}</strong>
                              </span>
                            )}
                          </div>
                        )}
                      </div>

                      {/* Question Hero Section */}
                      <div className="understand-hero">
                        <h3 className="understand-title">
                          What do you want to understand about {selectedRepo.name}?
                        </h3>
                        <p className="panel-text" style={{ marginBottom: '0.5rem' }}>
                          {capabilities?.semantic_search_available
                            ? 'Ask natural-language questions grounded in verified source code.'
                            : 'Ask natural-language questions grounded in verified static code evidence.'}
                        </p>
                        <form onSubmit={handleChatSubmit} className="chat-form understand-form">
                          <input
                            type="text"
                            className="input-text chat-input hero-input"
                            placeholder="Ask about this repository..."
                            value={chatQuestion}
                            onChange={(e) => setChatQuestion(e.target.value)}
                            disabled={chatSubmitting}
                          />
                          <button
                            type="submit"
                            disabled={chatSubmitting || !chatQuestion.trim()}
                            className="btn-action hero-ask-btn"
                          >
                            {chatSubmitting ? 'Asking...' : 'Ask'}
                          </button>
                        </form>

                        <div className="starter-questions-section">
                          <span className="starter-questions-label">Try a starter question:</span>
                          <div className="starter-questions-grid">
                            {STARTER_QUESTIONS.map((q) => (
                              <button
                                key={q}
                                type="button"
                                className="starter-question-btn"
                                onClick={() => submitQuestion(q)}
                                disabled={chatSubmitting}
                              >
                                {q}
                              </button>
                            ))}
                          </div>
                        </div>
                      </div>

                      {chatError && <div className="form-error" style={{ marginTop: '12px' }}>{chatError}</div>}

                      {/* Conversation / Investigation Workspace */}
                      {currentConversation && currentConversation.messages.length > 0 && (
                        <div className="chat-history" style={{ marginTop: '1.5rem' }}>
                          {currentConversation.messages.map((msg) => (
                            <div
                              key={msg.message_id}
                              className={`chat-bubble ${msg.role === 'user' ? 'user' : 'assistant'}`}
                            >
                              <div className="bubble-role">
                                {msg.role === 'user'
                                  ? 'Question'
                                  : msg.answer_mode === 'static_guidance'
                                  ? 'Start Here Reading Guide'
                                  : msg.answer_mode === 'insufficient_orientation'
                                  ? 'Orientation Status'
                                  : msg.answer_mode === 'reindex_required'
                                  ? 'Re-index Required'
                                  : 'Investigation Result'}
                              </div>
                              <div className="bubble-content">{msg.content}

                                {msg.role === 'assistant' && msg.answer_mode === 'reindex_required' && selectedRepo && (
                                  <div style={{ marginTop: '12px' }}>
                                    <button
                                      type="button"
                                      className="btn-action btn-refresh"
                                      onClick={() => handleRefreshRepo(selectedRepo)}
                                      disabled={
                                        !!Object.values(activeJobs).find(
                                          (j) =>
                                            j.repository_id === selectedRepo.repository_id &&
                                            j.status !== 'ready' &&
                                            j.status !== 'failed',
                                        )
                                      }
                                    >
                                      {Object.values(activeJobs).find(
                                          (j) =>
                                            j.repository_id === selectedRepo.repository_id &&
                                            j.status !== 'ready' &&
                                            j.status !== 'failed',
                                        ) ? 'Re-indexing...' : 'Re-index repository'}
                                    </button>
                                  </div>
                                )}
                              </div>

                              {msg.role === 'assistant' && msg.citations && msg.citations.length > 0 && (
                                <div className="citations-list">
                                  <span className="citation-heading">Citations:</span>
                                  {msg.citations.map((c, idx) => (
                                    <button
                                      key={idx}
                                      className="citation-item citation-btn"
                                      onClick={() => {
                                        setFocusedCitation({
                                          filePath: c.relative_path,
                                          startLine: c.start_line,
                                          endLine: c.end_line,
                                        })
                                        setActiveSection('files')
                                      }}
                                      title={`View ${c.relative_path}`}
                                    >
                                      [{idx + 1}] {c.relative_path}:{c.start_line}-{c.end_line} ({c.symbol_name})
                                    </button>
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
                          {chatSubmitting && (
                            <div className="chat-bubble assistant" aria-label="SourceTrace is analyzing">
                              <div className="bubble-role">SourceTrace</div>
                              <div className="typing-indicator" aria-hidden>
                                <span /><span /><span />
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                    </section>
                  )}

                  {activeSection === 'files' && (
                    <RepoExplorerPanel
                      client={client}
                      repositoryId={selectedRepo.repository_id}
                      repositoryName={selectedRepo.name}
                      targetFilePath={focusedCitation?.filePath}
                      targetLineRange={
                        focusedCitation
                          ? { start_line: focusedCitation.startLine, end_line: focusedCitation.endLine }
                          : null
                      }
                    />
                  )}

                  {activeSection === 'find_code' && (
                    <section className="card-panel search-panel">
                      <div style={{ marginBottom: '16px' }}>
                        <div
                          style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            marginBottom: '8px',
                          }}
                        >
                          <h2 className="panel-header" style={{ margin: 0 }}>
                            Find code in <span className="mono">{selectedRepo.name}</span>
                          </h2>
                          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                            {selectedRepo.source_type === 'github' && (
                              <button
                                id="btn-refresh-repo-find-code"
                                type="button"
                                className="btn-action"
                                style={{ fontSize: '0.8rem', padding: '4px 12px' }}
                                onClick={() => handleRefreshRepo(selectedRepo)}
                                disabled={
                                  !!Object.values(activeJobs).find(
                                    (j) =>
                                      j.repository_id === selectedRepo.repository_id &&
                                      j.status !== 'ready' &&
                                      j.status !== 'failed',
                                  )
                                }
                                title={
                                  selectedRepo.indexed_commit_sha
                                    ? `Last indexed SHA: ${selectedRepo.indexed_commit_sha.slice(0, 7)}`
                                    : 'Re-index this repository'
                                }
                              >
                                ↻ Refresh
                              </button>
                            )}
                            <button
                              type="button"
                              className="btn-action"
                              style={{
                                fontSize: '0.8rem',
                                padding: '4px 12px',
                                backgroundColor: 'transparent',
                                border: '1px solid var(--color-border)',
                              }}
                              onClick={() => handleDeleteRepo(selectedRepo.repository_id)}
                              disabled={deletingRepoId === selectedRepo.repository_id}
                            >
                              {deletingRepoId === selectedRepo.repository_id ? 'Deleting...' : 'Delete Repository'}
                            </button>
                          </div>
                        </div>
                      </div>
                      <p className="panel-text" style={{ marginBottom: '16px' }}>
                        Search code symbols, classes, functions, and relative file paths.
                      </p>

                      <form
                        onSubmit={handleEvidenceSearch}
                        className="search-form"
                        style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}
                      >
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
                          {searchLoading ? 'Searching...' : 'Find code'}
                        </button>
                      </form>

                      {searchError && (
                        <div className="form-error" style={{ marginBottom: '16px' }}>
                          {searchError}
                        </div>
                      )}

                      {searchResults !== null && (
                        <div className="search-results">
                          <h3 style={{ fontSize: '0.9rem', color: 'var(--color-muted)', marginBottom: '12px' }}>
                            Code Results ({searchResults.length}):
                          </h3>
                          {searchResults.length === 0 ? (
                            <p className="panel-text" style={{ fontStyle: 'italic' }}>
                              No code matches "{searchQuery}".
                            </p>
                          ) : (
                            searchResults.map((item) => (
                              <div
                                key={item.chunk_id}
                                className="search-card"
                                style={{
                                  background: 'var(--color-raised)',
                                  border: '1px solid var(--color-border)',
                                  borderRadius: '8px',
                                  padding: '12px',
                                  marginBottom: '12px',
                                }}
                              >
                                <div
                                  style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    marginBottom: '6px',
                                    fontSize: '0.85rem',
                                  }}
                                >
                                  <span className="mono" style={{ color: 'var(--color-signal)' }}>
                                    {item.relative_path}:{item.start_line}-{item.end_line}
                                  </span>
                                  <span style={{ color: 'var(--color-muted)' }}>
                                    {item.symbol_type} <strong>{item.symbol_name}</strong>
                                  </span>
                                </div>
                                <pre
                                  className="evidence-block"
                                  style={{
                                    margin: 0,
                                    padding: '8px',
                                    background: 'var(--color-surface)',
                                    borderRadius: '4px',
                                    overflowX: 'auto',
                                    fontSize: '0.8rem',
                                  }}
                                >
                                  <code>{item.snippet}</code>
                                </pre>
                              </div>
                            ))
                          )}
                        </div>
                      )}
                    </section>
                  )}

                  {activeSection === 'flow_trace' && (
                    <FlowTracePanel
                      client={client}
                      repositoryId={selectedRepo.repository_id}
                      repositoryName={selectedRepo.name}
                      explainAvailable={capabilities?.generation_available ?? false}
                      isStale={selectedRepo.is_stale}
                      sourceType={selectedRepo.source_type}
                    />
                  )}

                  {activeSection === 'change_impact' && (
                    <ImpactPanel
                      client={client}
                      repositoryId={selectedRepo.repository_id}
                      repositoryName={selectedRepo.name}
                      explainAvailable={capabilities?.generation_available ?? false}
                      isStale={selectedRepo.is_stale}
                      sourceType={selectedRepo.source_type}
                    />
                  )}
                </div>
              ) : (
                <section className="state-box">
                  <strong>Repository status:</strong>{' '}
                  {repositories.length === 0
                    ? 'No repositories imported yet. Enter a GitHub URL or select a ZIP archive above.'
                    : 'Select a ready repository from the sidebar to inspect or search.'}
                </section>
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
        <p>
          Evidence is shown only when retrieved from source.
          <span style={{ marginLeft: '0.75rem' }}>
            <kbd className="kbd">Ctrl K</kbd> quick jump, <kbd className="kbd">Esc</kbd> close
          </span>
        </p>
      </footer>

      {/* Command Palette (Ctrl/Cmd+K) */}
      {cmdkOpen && (
        <div className="cmdk-overlay" onClick={() => setCmdkOpen(false)}>
          <div
            className="cmdk-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Command palette"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="cmdk-input-row">
              <span aria-hidden style={{ color: 'var(--color-faint)' }}>⌕</span>
              <input
                ref={cmdkInputRef}
                className="cmdk-input"
                placeholder="Jump to repository, workspace, or ask a question…"
                value={cmdkQuery}
                onChange={(e) => {
                  setCmdkQuery(e.target.value)
                  setCmdkSelectedIdx(0)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    setCmdkSelectedIdx((i) => Math.min(i + 1, cmdkItems.length - 1))
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    setCmdkSelectedIdx((i) => Math.max(i - 1, 0))
                  } else if (e.key === 'Enter') {
                    e.preventDefault()
                    const item = cmdkItems[cmdkSelectedIdx]
                    if (item) item.action()
                  }
                }}
              />
              <kbd className="kbd">Esc</kbd>
            </div>
            <div className="cmdk-list">
              {cmdkItems.length === 0 ? (
                <div className="cmdk-empty">No matches. Try a repository name or workspace.</div>
              ) : (
                cmdkItems.map((item, idx) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`cmdk-item ${idx === cmdkSelectedIdx ? 'selected' : ''}`}
                    onMouseEnter={() => setCmdkSelectedIdx(idx)}
                    onClick={item.action}
                  >
                    <span>
                      {item.type === 'repo' ? 'Repo' : item.type === 'section' ? 'Nav' : 'Qry'}
                    </span>
                    <span>{item.label}</span>
                    <span className="cmdk-item-type">{item.hint}</span>
                  </button>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {/* Toasts */}
      {toasts.length > 0 && (
        <div className="toast-stack" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast ${t.kind}`}>
              <span className="toast-icon">
                {t.kind === 'success' ? 'OK' : t.kind === 'error' ? 'ERR' : 'INFO'}
              </span>
              <span>{t.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default App
