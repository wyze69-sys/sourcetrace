import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  buildFileTree,
  extractAllFolderPaths,
  RepoExplorerPanel,
  type TreeNodeFile,
  type TreeNodeFolder,
} from './RepoExplorerPanel'
import { ApiError, type ApiClient } from '../services/apiClient'
import type {
  RepositoryFileContentResponse,
  RepositoryFileItem,
  RepositoryFileListResponse,
} from '../services/types'

describe('RepoExplorerPanel - buildFileTree unit tests', () => {
  it('converts flat paths into nested folders and files', () => {
    const flatFiles: RepositoryFileItem[] = [
      { path: 'src/components/Button.tsx', language: 'typescript', chunk_count: 2 },
      { path: 'src/components/Header.tsx', language: 'typescript', chunk_count: 1 },
      { path: 'src/utils.ts', language: 'typescript', chunk_count: 3 },
      { path: 'README.md', language: 'markdown', chunk_count: 1 },
    ]

    const tree = buildFileTree(flatFiles)

    // Top level should have folder 'src' first, then file 'README.md'
    expect(tree).toHaveLength(2)
    expect(tree[0].type).toBe('folder')
    const srcFolder = tree[0] as TreeNodeFolder
    expect(srcFolder.name).toBe('src')
    expect(srcFolder.path).toBe('src')

    expect(tree[1].type).toBe('file')
    const readmeFile = tree[1] as TreeNodeFile
    expect(readmeFile.name).toBe('README.md')

    // Inside 'src': subfolder 'components' first, then file 'utils.ts'
    expect(srcFolder.children).toHaveLength(2)
    expect(srcFolder.children[0].type).toBe('folder')
    const componentsFolder = srcFolder.children[0] as TreeNodeFolder
    expect(componentsFolder.name).toBe('components')

    expect(srcFolder.children[1].type).toBe('file')
    expect((srcFolder.children[1] as TreeNodeFile).name).toBe('utils.ts')

    // Inside 'src/components': files 'Button.tsx' and 'Header.tsx'
    expect(componentsFolder.children).toHaveLength(2)
    expect((componentsFolder.children[0] as TreeNodeFile).name).toBe('Button.tsx')
    expect((componentsFolder.children[1] as TreeNodeFile).name).toBe('Header.tsx')
  })

  it('orders folders before files, alphabetically within each group', () => {
    const flatFiles: RepositoryFileItem[] = [
      { path: 'z_file.py', language: 'python', chunk_count: 1 },
      { path: 'b_folder/sub.py', language: 'python', chunk_count: 1 },
      { path: 'a_folder/sub.py', language: 'python', chunk_count: 1 },
      { path: 'a_file.py', language: 'python', chunk_count: 1 },
    ]

    const tree = buildFileTree(flatFiles)

    // Expected top level: folder 'a_folder', folder 'b_folder', file 'a_file.py', file 'z_file.py'
    expect(tree).toHaveLength(4)
    expect((tree[0] as TreeNodeFolder).name).toBe('a_folder')
    expect((tree[1] as TreeNodeFolder).name).toBe('b_folder')
    expect((tree[2] as TreeNodeFile).name).toBe('a_file.py')
    expect((tree[3] as TreeNodeFile).name).toBe('z_file.py')
  })

  it('extracts all folder paths correctly', () => {
    const flatFiles: RepositoryFileItem[] = [
      { path: 'src/components/Button.tsx', language: 'typescript', chunk_count: 2 },
      { path: 'src/utils/helpers/math.ts', language: 'typescript', chunk_count: 1 },
    ]

    const folderPaths = extractAllFolderPaths(flatFiles)
    expect(folderPaths.has('src')).toBe(true)
    expect(folderPaths.has('src/components')).toBe(true)
    expect(folderPaths.has('src/utils')).toBe(true)
    expect(folderPaths.has('src/utils/helpers')).toBe(true)
    expect(folderPaths.size).toBe(4)
  })
})

describe('RepoExplorerPanel - Component Integration & State Tests', () => {
  function createMockClient(): ApiClient {
    return {
      listRepositoryFiles: vi.fn(),
      getRepositoryFileContent: vi.fn(),
    } as unknown as ApiClient
  }

  it('shows no repository selected state when repositoryId is null', () => {
    const mockClient = createMockClient()
    render(<RepoExplorerPanel client={mockClient} repositoryId={null} />)

    expect(screen.getByText('Repository Explorer')).toBeInTheDocument()
    expect(
      screen.getByText(/Select a ready repository from the sidebar to inspect its file structure/i),
    ).toBeInTheDocument()
    expect(mockClient.listRepositoryFiles).not.toHaveBeenCalled()
  })

  it('renders loading state, guidance text, and fetches files when repositoryId is provided', async () => {
    const mockClient = createMockClient()
    let resolvePromise: (value: RepositoryFileListResponse) => void = () => {}
    const promise = new Promise<RepositoryFileListResponse>((resolve) => {
      resolvePromise = resolve
    })

    vi.mocked(mockClient.listRepositoryFiles).mockReturnValue(promise)

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_123" repositoryName="My Repo" />)

    // Should render loading state and guidance text
    expect(screen.getByRole('heading', { level: 2, name: /Repository Explorer: My Repo/i })).toBeInTheDocument()
    expect(
      screen.getByText(/Choose a file to orient yourself, or search\/ask a question about the repository\./i),
    ).toBeInTheDocument()
    expect(screen.getByText(/Loading repository files\.\.\./i)).toBeInTheDocument()

    // Resolve API request
    resolvePromise({
      repository_id: 'repo_123',
      files: [
        { path: 'src/app.py', language: 'python', chunk_count: 3 },
        { path: 'README.md', language: 'markdown', chunk_count: 1 },
      ],
    })

    await waitFor(() => {
      expect(screen.queryByText(/Loading repository files\.\.\./i)).not.toBeInTheDocument()
    })

    expect(screen.getByTestId('file-tree-container')).toBeInTheDocument()
    expect(screen.getByText('src')).toBeInTheDocument()
    expect(screen.getByText('app.py')).toBeInTheDocument()
    expect(screen.getByText('README.md')).toBeInTheDocument()
    expect(screen.getByText('3 chunks')).toBeInTheDocument()
  })

  it('renders empty state when repository has no indexed files', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_empty',
      files: [],
    })

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_empty" />)

    await waitFor(() => {
      expect(screen.getByText(/No indexed files available for this repository\./i)).toBeInTheDocument()
    })
  })

  it('renders API error state with visible retry action', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockRejectedValue(
      new ApiError('Failed to fetch files.', 'STORAGE_ERROR', 500),
    )

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_error" />)

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch files.')).toBeInTheDocument()
    })

    const retryButton = screen.getByRole('button', { name: /Retry Loading Files/i })
    expect(retryButton).toBeInTheDocument()

    // Mock successful retry
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_error',
      files: [{ path: 'main.py', language: 'python', chunk_count: 1 }],
    })

    fireEvent.click(retryButton)

    await waitFor(() => {
      expect(screen.getByText('main.py')).toBeInTheDocument()
    })
  })

  it('triggers the correct file-list request when repositoryId changes', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockImplementation(async (repoId: string) => ({
      repository_id: repoId,
      files: [{ path: `${repoId}_file.py`, language: 'python', chunk_count: 1 }],
    }))

    const { rerender } = render(<RepoExplorerPanel client={mockClient} repositoryId="repo_A" />)

    await waitFor(() => {
      expect(screen.getByText('repo_A_file.py')).toBeInTheDocument()
    })
    expect(mockClient.listRepositoryFiles).toHaveBeenCalledWith('repo_A')

    rerender(<RepoExplorerPanel client={mockClient} repositoryId="repo_B" />)

    await waitFor(() => {
      expect(screen.getByText('repo_B_file.py')).toBeInTheDocument()
    })
    expect(mockClient.listRepositoryFiles).toHaveBeenCalledWith('repo_B')
  })

  it('prevents stale responses from a previously selected repository from replacing current tree', async () => {
    const mockClient = createMockClient()

    let resolveRepoA: (val: RepositoryFileListResponse) => void = () => {}
    let resolveRepoB: (val: RepositoryFileListResponse) => void = () => {}

    vi.mocked(mockClient.listRepositoryFiles).mockImplementation((repoId: string) => {
      if (repoId === 'repo_A') {
        return new Promise<RepositoryFileListResponse>((res) => {
          resolveRepoA = res
        })
      }
      return new Promise<RepositoryFileListResponse>((res) => {
        resolveRepoB = res
      })
    })

    const { rerender } = render(<RepoExplorerPanel client={mockClient} repositoryId="repo_A" />)

    // Switch to repo_B before repo_A resolves
    rerender(<RepoExplorerPanel client={mockClient} repositoryId="repo_B" />)

    // Resolve repo_B first
    resolveRepoB({
      repository_id: 'repo_B',
      files: [{ path: 'repo_B_fresh.py', language: 'python', chunk_count: 1 }],
    })

    await waitFor(() => {
      expect(screen.getByText('repo_B_fresh.py')).toBeInTheDocument()
    })

    // Now resolve repo_A (stale response)
    resolveRepoA({
      repository_id: 'repo_A',
      files: [{ path: 'repo_A_stale.py', language: 'python', chunk_count: 1 }],
    })

    // Wait a tick to ensure no re-render with repo_A data occurred
    await new Promise((r) => setTimeout(r, 50))

    expect(screen.queryByText('repo_A_stale.py')).not.toBeInTheDocument()
    expect(screen.getByText('repo_B_fresh.py')).toBeInTheDocument()
  })

  it('allows toggling folders and selecting files', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_interactive',
      files: [{ path: 'src/utils/math.py', language: 'python', chunk_count: 2 }],
    })
    vi.mocked(mockClient.getRepositoryFileContent).mockResolvedValue({
      repository_id: 'repo_interactive',
      path: 'src/utils/math.py',
      language: 'python',
      content: 'def add(): pass',
      line_count: 1,
      is_complete: false,
      completeness_reason: 'source_boundary_unavailable',
    })

    const onSelectFile = vi.fn()
    render(
      <RepoExplorerPanel
        client={mockClient}
        repositoryId="repo_interactive"
        onSelectFile={onSelectFile}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('math.py')).toBeInTheDocument()
    })

    const fileButton = screen.getByTestId('file-src/utils/math.py')
    expect(fileButton).not.toHaveClass('selected')

    fireEvent.click(fileButton)

    await waitFor(() => {
      expect(fileButton).toHaveClass('selected')
      expect(fileButton).toHaveAttribute('aria-selected', 'true')
      expect(onSelectFile).toHaveBeenCalledWith('src/utils/math.py')
      expect(screen.getByTestId('code-viewer-container')).toBeInTheDocument()
    })

    // Test folder collapse
    const folderButton = screen.getByTestId('folder-src').querySelector('button')!
    expect(folderButton).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(folderButton)

    expect(folderButton).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('math.py')).not.toBeInTheDocument()
  })

  it('prevents stale retry responses from overwriting the active repository explorer', async () => {
    const mockClient = createMockClient()

    let resolveRepoARetry: (val: RepositoryFileListResponse) => void = () => {}

    let repoACallCount = 0
    vi.mocked(mockClient.listRepositoryFiles).mockImplementation((repoId: string) => {
      if (repoId === 'repo_A') {
        repoACallCount++
        if (repoACallCount === 1) {
          return Promise.reject(new ApiError('Failed to load repo_A', 'ERROR', 500))
        }
        return new Promise<RepositoryFileListResponse>((res) => {
          resolveRepoARetry = res
        })
      }
      return Promise.resolve({
        repository_id: 'repo_B',
        files: [{ path: 'repo_B_file.py', language: 'python', chunk_count: 1 }],
      })
    })

    const { rerender } = render(<RepoExplorerPanel client={mockClient} repositoryId="repo_A" />)

    await waitFor(() => {
      expect(screen.getByText('Failed to load repo_A')).toBeInTheDocument()
    })

    const retryButton = screen.getByRole('button', { name: /Retry Loading Files/i })
    fireEvent.click(retryButton)

    rerender(<RepoExplorerPanel client={mockClient} repositoryId="repo_B" />)

    await waitFor(() => {
      expect(screen.getByText('repo_B_file.py')).toBeInTheDocument()
    })

    await act(async () => {
      resolveRepoARetry({
        repository_id: 'repo_A',
        files: [{ path: 'repo_A_stale_retry.py', language: 'python', chunk_count: 1 }],
      })
    })

    await new Promise((r) => setTimeout(r, 50))

    expect(screen.queryByText('repo_A_stale_retry.py')).not.toBeInTheDocument()
    expect(screen.getByText('repo_B_file.py')).toBeInTheDocument()
  })

  it('selecting a file requests the correct repository ID and path and renders content with line numbers', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_123',
      files: [{ path: 'src/main.py', language: 'python', chunk_count: 2 }],
    })
    vi.mocked(mockClient.getRepositoryFileContent).mockResolvedValue({
      repository_id: 'repo_123',
      path: 'src/main.py',
      language: 'python',
      content: 'def foo():\n    return 42',
      line_count: 2,
      is_complete: false,
      completeness_reason: 'source_boundary_unavailable',
    })

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_123" />)

    await waitFor(() => {
      expect(screen.getByText('main.py')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('main.py'))

    await waitFor(() => {
      expect(mockClient.getRepositoryFileContent).toHaveBeenCalledWith('repo_123', 'src/main.py')
      expect(screen.getByTestId('code-viewer-container')).toBeInTheDocument()
      expect(screen.getByText('def foo():')).toBeInTheDocument()
      expect(screen.getByText('return 42')).toBeInTheDocument()
      expect(screen.getByText('2 lines')).toBeInTheDocument()
    })
  })

  it('renders unverified end-of-file boundary notice when is_complete is false', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_123',
      files: [{ path: 'src/partial.py', language: 'python', chunk_count: 1 }],
    })
    vi.mocked(mockClient.getRepositoryFileContent).mockResolvedValue({
      repository_id: 'repo_123',
      path: 'src/partial.py',
      language: 'python',
      content: '\n\n\ndef partial(): pass',
      line_count: 4,
      is_complete: false,
      completeness_reason: 'unindexed_line_gaps',
    })

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_123" />)

    await waitFor(() => {
      expect(screen.getByText('partial.py')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('partial.py'))

    await waitFor(() => {
      expect(screen.getByTestId('partial-content-notice')).toBeInTheDocument()
      expect(screen.getByText(/original end-of-file boundary is unverified/i)).toBeInTheDocument()
    })
  })

  it('stale file content responses from a previously selected file do not overwrite newly selected file', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_123',
      files: [
        { path: 'src/fileA.py', language: 'python', chunk_count: 1 },
        { path: 'src/fileB.py', language: 'python', chunk_count: 1 },
      ],
    })

    let resolveFileA: (val: RepositoryFileContentResponse) => void = () => {}

    vi.mocked(mockClient.getRepositoryFileContent).mockImplementation((_repoId: string, path: string) => {
      if (path === 'src/fileA.py') {
        return new Promise((res) => {
          resolveFileA = res
        })
      }
      return Promise.resolve({
        repository_id: 'repo_123',
        path: 'src/fileB.py',
        language: 'python',
        content: 'content of file B',
        line_count: 1,
        is_complete: false,
        completeness_reason: 'source_boundary_unavailable',
      })
    })

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_123" />)

    await waitFor(() => {
      expect(screen.getByText('fileA.py')).toBeInTheDocument()
    })

    // Click file A (pending promise)
    fireEvent.click(screen.getByText('fileA.py'))

    // Immediately click file B
    fireEvent.click(screen.getByText('fileB.py'))

    await waitFor(() => {
      expect(screen.getByText('content of file B')).toBeInTheDocument()
    })

    // Late resolve file A
    await act(async () => {
      resolveFileA({
        repository_id: 'repo_123',
        path: 'src/fileA.py',
        language: 'python',
        content: 'stale content of file A',
        line_count: 1,
        is_complete: false,
        completeness_reason: 'source_boundary_unavailable',
      })
    })

    await new Promise((r) => setTimeout(r, 50))

    expect(screen.queryByText('stale content of file A')).not.toBeInTheDocument()
    expect(screen.getByText('content of file B')).toBeInTheDocument()
  })

  it('renders source text as plain text safely without HTML injection', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_123',
      files: [{ path: 'src/xss.html', language: 'html', chunk_count: 1 }],
    })
    vi.mocked(mockClient.getRepositoryFileContent).mockResolvedValue({
      repository_id: 'repo_123',
      path: 'src/xss.html',
      language: 'html',
      content: '<script>alert("xss")</script><img src=x onerror=alert(1)>',
      line_count: 1,
      is_complete: false,
      completeness_reason: 'source_boundary_unavailable',
    })

    render(<RepoExplorerPanel client={mockClient} repositoryId="repo_123" />)

    await waitFor(() => {
      expect(screen.getByText('xss.html')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('xss.html'))

    await waitFor(() => {
      expect(screen.getByText('<script>alert("xss")</script><img src=x onerror=alert(1)>')).toBeInTheDocument()
    })
  })

  it('ACCEPT-NAV-001: opens targetFilePath automatically and highlights cited line range', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_123',
      files: [
        { path: 'backend/server.js', language: 'javascript', chunk_count: 2 },
        { path: 'backend/app.js', language: 'javascript', chunk_count: 1 },
      ],
    })
    vi.mocked(mockClient.getRepositoryFileContent).mockResolvedValue({
      repository_id: 'repo_123',
      path: 'backend/server.js',
      language: 'javascript',
      content: 'line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\nconst app = require("./app");\nline 12',
      line_count: 12,
      is_complete: true,
      completeness_reason: 'source_file_exact_match',
    })

    render(
      <RepoExplorerPanel
        client={mockClient}
        repositoryId="repo_123"
        targetFilePath="backend/server.js"
        targetLineRange={{ start_line: 11, end_line: 11 }}
      />,
    )

    await waitFor(() => {
      expect(mockClient.getRepositoryFileContent).toHaveBeenCalledWith('repo_123', 'backend/server.js')
      expect(screen.getByText('const app = require("./app");')).toBeInTheDocument()
    })

    const citedLines = screen.getAllByTestId('cited-code-line')
    expect(citedLines).toHaveLength(1)
    expect(citedLines[0]).toHaveTextContent('const app = require("./app");')

    const citedLineNumbers = screen.getAllByTestId('cited-line-number')
    expect(citedLineNumbers).toHaveLength(1)
    expect(citedLineNumbers[0]).toHaveTextContent('11')
  })

  it('ACCEPT-NAV-001: handles invalid/out-of-range line range safely without crashing', async () => {
    const mockClient = createMockClient()
    vi.mocked(mockClient.listRepositoryFiles).mockResolvedValue({
      repository_id: 'repo_123',
      files: [{ path: 'short.py', language: 'python', chunk_count: 1 }],
    })
    vi.mocked(mockClient.getRepositoryFileContent).mockResolvedValue({
      repository_id: 'repo_123',
      path: 'short.py',
      language: 'python',
      content: 'print("hello")',
      line_count: 1,
      is_complete: true,
      completeness_reason: 'source_file_exact_match',
    })

    render(
      <RepoExplorerPanel
        client={mockClient}
        repositoryId="repo_123"
        targetFilePath="short.py"
        targetLineRange={{ start_line: 999, end_line: 1000 }}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('print("hello")')).toBeInTheDocument()
    })

    expect(screen.queryByTestId('cited-code-line')).not.toBeInTheDocument()
    expect(screen.queryByTestId('cited-line-number')).not.toBeInTheDocument()
  })
})
