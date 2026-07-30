import { useCallback, useEffect, useRef, useState } from 'react'
import { apiClient as defaultApiClient, ApiError, type ApiClient } from '../services/apiClient'
import type { RepositoryFileContentResponse, RepositoryFileItem } from '../services/types'

export interface RepoExplorerPanelProps {
  client?: ApiClient
  repositoryId: string | null
  repositoryName?: string
  onSelectFile?: (filePath: string) => void
  targetFilePath?: string | null
  targetLineRange?: { start_line: number; end_line: number } | null
}

export interface TreeNodeFolder {
  type: 'folder'
  name: string
  path: string
  children: TreeNode[]
}

export interface TreeNodeFile {
  type: 'file'
  name: string
  path: string
  language: string
  chunk_count: number
}

export type TreeNode = TreeNodeFolder | TreeNodeFile

interface RawFolderNode {
  name: string
  path: string
  subfolders: Map<string, RawFolderNode>
  files: TreeNodeFile[]
}

/**
 * Converts flat file items into a nested tree of folders and files.
 * Folders appear before files, and items are sorted alphabetically within each group.
 */
export function buildFileTree(files: RepositoryFileItem[]): TreeNode[] {
  const rootSubfolders = new Map<string, RawFolderNode>()
  const rootFiles: TreeNodeFile[] = []

  for (const item of files) {
    const parts = item.path.split('/').filter(Boolean)
    if (parts.length === 0) continue

    if (parts.length === 1) {
      rootFiles.push({
        type: 'file',
        name: parts[0],
        path: item.path,
        language: item.language,
        chunk_count: item.chunk_count,
      })
    } else {
      let currentMap = rootSubfolders
      let accumPath = ''
      for (let i = 0; i < parts.length - 1; i++) {
        const folderName = parts[i]
        accumPath = accumPath ? `${accumPath}/${folderName}` : folderName

        let folder = currentMap.get(folderName)
        if (!folder) {
          folder = {
            name: folderName,
            path: accumPath,
            subfolders: new Map(),
            files: [],
          }
          currentMap.set(folderName, folder)
        }

        if (i === parts.length - 2) {
          const fileName = parts[parts.length - 1]
          folder.files.push({
            type: 'file',
            name: fileName,
            path: item.path,
            language: item.language,
            chunk_count: item.chunk_count,
          })
        } else {
          currentMap = folder.subfolders
        }
      }
    }
  }

  function convertRawToTree(
    subfoldersMap: Map<string, RawFolderNode>,
    filesList: TreeNodeFile[],
  ): TreeNode[] {
    const sortedSubfolders = Array.from(subfoldersMap.values()).sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }),
    )

    const sortedFiles = [...filesList].sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }),
    )

    const result: TreeNode[] = []

    for (const sub of sortedSubfolders) {
      result.push({
        type: 'folder',
        name: sub.name,
        path: sub.path,
        children: convertRawToTree(sub.subfolders, sub.files),
      })
    }

    for (const file of sortedFiles) {
      result.push(file)
    }

    return result
  }

  return convertRawToTree(rootSubfolders, rootFiles)
}

/** Helper to extract all folder paths recursively for expand all */
export function extractAllFolderPaths(files: RepositoryFileItem[]): Set<string> {
  const folderPaths = new Set<string>()
  for (const file of files) {
    const parts = file.path.split('/').filter(Boolean)
    let accum = ''
    for (let i = 0; i < parts.length - 1; i++) {
      accum = accum ? `${accum}/${parts[i]}` : parts[i]
      folderPaths.add(accum)
    }
  }
  return folderPaths
}

interface TreeItemProps {
  node: TreeNode
  expandedFolders: Set<string>
  toggleFolder: (path: string) => void
  selectedFilePath: string | null
  onSelectFile: (path: string) => void
}

export function TreeItemView({
  node,
  expandedFolders,
  toggleFolder,
  selectedFilePath,
  onSelectFile,
}: TreeItemProps) {
  if (node.type === 'folder') {
    const isExpanded = expandedFolders.has(node.path)
    return (
      <div className="tree-node tree-folder" data-testid={`folder-${node.path}`}>
        <button
          type="button"
          className="tree-node-row folder-row"
          onClick={() => toggleFolder(node.path)}
          aria-expanded={isExpanded}
        >
          <span className="tree-icon folder-icon">{isExpanded ? '📂' : '📁'}</span>
          <span className="tree-name folder-name">{node.name}</span>
        </button>
        {isExpanded && (
          <div className="tree-children">
            {node.children.map((child) => (
              <TreeItemView
                key={child.path}
                node={child}
                expandedFolders={expandedFolders}
                toggleFolder={toggleFolder}
                selectedFilePath={selectedFilePath}
                onSelectFile={onSelectFile}
              />
            ))}
          </div>
        )}
      </div>
    )
  }

  const isSelected = selectedFilePath === node.path
  return (
    <button
      type="button"
      className={`tree-node-row file-row ${isSelected ? 'selected' : ''}`}
      onClick={() => onSelectFile(node.path)}
      aria-selected={isSelected}
      data-testid={`file-${node.path}`}
    >
      <span className="tree-icon file-icon">•</span>
      <span className="tree-name file-name">{node.name}</span>
      <span className="file-meta">
        {node.language && <span className="file-lang-badge">{node.language}</span>}
        <span className="file-chunk-badge">
          {node.chunk_count} {node.chunk_count === 1 ? 'chunk' : 'chunks'}
        </span>
      </span>
    </button>
  )
}

interface CodeViewerProps {
  selectedPath: string
  contentData: RepositoryFileContentResponse | null
  loading: boolean
  error: string | null
  onRetry: () => void
  highlightLineRange?: { start_line: number; end_line: number } | null
}

export function CodeViewer({
  selectedPath,
  contentData,
  loading,
  error,
  onRetry,
  highlightLineRange,
}: CodeViewerProps) {
  const startLineRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (startLineRef.current && typeof startLineRef.current.scrollIntoView === 'function') {
      startLineRef.current.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  }, [highlightLineRange, contentData])

  if (loading) {
    return (
      <div className="code-viewer-panel loading-state" data-testid="code-viewer-loading">
        <span className="status-dot loading" />
        <span className="panel-text" style={{ margin: 0 }}>
          Loading source code for <span className="mono">{selectedPath}</span>...
        </span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="code-viewer-panel error-state" data-testid="code-viewer-error">
        <div className="form-error" style={{ marginBottom: '12px' }}>
          {error}
        </div>
        <button type="button" className="btn-action btn-retry" onClick={onRetry}>
          Retry Loading Content
        </button>
      </div>
    )
  }

  if (!contentData) {
    return null
  }

  const lines = contentData.content ? contentData.content.split('\n') : []

  const isCitationLineUnavailable = Boolean(
    highlightLineRange &&
      (highlightLineRange.start_line > lines.length ||
        highlightLineRange.start_line <= 0 ||
        highlightLineRange.end_line < highlightLineRange.start_line),
  )

  const isLineHighlighted = (lineNum: number) => {
    if (!highlightLineRange || isCitationLineUnavailable) return false
    const { start_line, end_line } = highlightLineRange
    return lineNum >= start_line && lineNum <= end_line
  }

  return (
    <div className="code-viewer-panel" data-testid="code-viewer-container">
      <div className="code-viewer-header">
        <div className="code-viewer-title">
          <span className="mono bold">{contentData.path}</span>
          <span className="file-lang-badge">{contentData.language}</span>
          <span className="file-chunk-badge">
            {contentData.line_count} {contentData.line_count === 1 ? 'line' : 'lines'}
          </span>
        </div>
        {!contentData.is_complete && (
          <div className="partial-notice-badge" data-testid="partial-content-notice">
            ⚠️ Notice: Displayed source content is indexed chunks only and may be incomplete (original end-of-file boundary is unverified).
          </div>
        )}
        {isCitationLineUnavailable && highlightLineRange && (
          <div className="partial-notice-badge unavailable-notice" data-testid="citation-line-unavailable-notice">
            ⚠️ Notice: Cited line range (L{highlightLineRange.start_line}-L{highlightLineRange.end_line}) is outside the available content for this file ({lines.length} lines available).
          </div>
        )}
      </div>

      {lines.length === 0 ? (
        <div className="state-box">This file is empty.</div>
      ) : (
        <div className="code-viewer-body">
          <div className="line-numbers-gutter" aria-hidden="true">
            {lines.map((_, idx) => {
              const lineNum = idx + 1
              const highlighted = isLineHighlighted(lineNum)
              return (
                <span
                  key={lineNum}
                  className={`line-number ${highlighted ? 'cited-line-number-highlight' : ''}`}
                  data-line-number={lineNum}
                  data-testid={highlighted ? 'cited-line-number' : undefined}
                >
                  {lineNum}
                </span>
              )
            })}
          </div>
          <pre className="code-text-pre">
            <code>
              {lines.map((line, idx) => {
                const lineNum = idx + 1
                const highlighted = isLineHighlighted(lineNum)
                const isStartLine = highlightLineRange && lineNum === highlightLineRange.start_line
                return (
                  <div
                    key={lineNum}
                    ref={isStartLine ? startLineRef : undefined}
                    className={`code-line ${highlighted ? 'cited-line-highlight' : ''}`}
                    data-line-number={lineNum}
                    data-testid={highlighted ? 'cited-code-line' : undefined}
                  >
                    {line || ' '}
                  </div>
                )
              })}
            </code>
          </pre>
        </div>
      )}
    </div>
  )
}

export function RepoExplorerPanel({
  client = defaultApiClient,
  repositoryId,
  repositoryName,
  onSelectFile,
  targetFilePath,
  targetLineRange,
}: RepoExplorerPanelProps) {
  const [files, setFiles] = useState<RepositoryFileItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null)
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set())

  // State for source code content viewer
  const [fileContent, setFileContent] = useState<RepositoryFileContentResponse | null>(null)
  const [contentLoading, setContentLoading] = useState(false)
  const [contentError, setContentError] = useState<string | null>(null)

  const requestIdRef = useRef(0)
  const contentRequestIdRef = useRef(0)

  const loadFileContent = useCallback(
    async (targetRepoId: string, filePath: string) => {
      const currentRequestId = ++contentRequestIdRef.current

      if (typeof client.getRepositoryFileContent !== 'function') {
        setFileContent(null)
        setContentLoading(false)
        setContentError(null)
        return
      }

      setContentLoading(true)
      setContentError(null)

      try {
        const res = await client.getRepositoryFileContent(targetRepoId, filePath)
        if (contentRequestIdRef.current !== currentRequestId) {
          return
        }
        if (res.repository_id !== targetRepoId || res.path !== filePath) {
          return
        }
        setFileContent(res)
        setContentError(null)
      } catch (err) {
        if (contentRequestIdRef.current !== currentRequestId) {
          return
        }
        if (err instanceof ApiError) {
          setContentError(err.message)
        } else {
          setContentError('Failed to load file content.')
        }
      } finally {
        if (contentRequestIdRef.current === currentRequestId) {
          setContentLoading(false)
        }
      }
    },
    [client],
  )

  const loadFiles = useCallback(
    async (targetRepoId: string | null) => {
      const currentRequestId = ++requestIdRef.current
      contentRequestIdRef.current++

      if (!targetRepoId || typeof client.listRepositoryFiles !== 'function') {
        setFiles([])
        setLoading(false)
        setError(null)
        setSelectedFilePath(null)
        setFileContent(null)
        setContentLoading(false)
        setContentError(null)
        return
      }

      setLoading(true)
      setError(null)
      setSelectedFilePath(null)
      setFileContent(null)
      setContentLoading(false)
      setContentError(null)

      try {
        const res = await client.listRepositoryFiles(targetRepoId)
        if (requestIdRef.current !== currentRequestId) {
          return
        }
        if (res.repository_id !== targetRepoId) {
          return
        }
        setFiles(res.files || [])
        setExpandedFolders(extractAllFolderPaths(res.files || []))
        setError(null)
      } catch (err) {
        if (requestIdRef.current !== currentRequestId) {
          return
        }
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('Failed to load repository files.')
        }
      } finally {
        if (requestIdRef.current === currentRequestId) {
          setLoading(false)
        }
      }
    },
    [client],
  )

  useEffect(() => {
    loadFiles(repositoryId)
    return () => {
      // eslint-disable-next-line react-hooks/exhaustive-deps
      requestIdRef.current++
      // eslint-disable-next-line react-hooks/exhaustive-deps
      contentRequestIdRef.current++
    }
  }, [repositoryId, loadFiles])

  useEffect(() => {
    if (targetFilePath && repositoryId) {
      setSelectedFilePath(targetFilePath)
      setExpandedFolders((prev) => {
        const next = new Set(prev)
        const parts = targetFilePath.split('/').filter(Boolean)
        let accum = ''
        for (let i = 0; i < parts.length - 1; i++) {
          accum = accum ? `${accum}/${parts[i]}` : parts[i]
          next.add(accum)
        }
        return next
      })
      loadFileContent(repositoryId, targetFilePath)
    }
  }, [repositoryId, targetFilePath, loadFileContent])

  const handleRetryFiles = useCallback(() => {
    loadFiles(repositoryId)
  }, [repositoryId, loadFiles])

  const handleRetryContent = useCallback(() => {
    if (repositoryId && selectedFilePath) {
      loadFileContent(repositoryId, selectedFilePath)
    }
  }, [repositoryId, selectedFilePath, loadFileContent])

  const toggleFolder = useCallback((folderPath: string) => {
    setExpandedFolders((prev) => {
      const next = new Set(prev)
      if (next.has(folderPath)) {
        next.delete(folderPath)
      } else {
        next.add(folderPath)
      }
      return next
    })
  }, [])

  const expandAll = useCallback(() => {
    setExpandedFolders(extractAllFolderPaths(files))
  }, [files])

  const collapseAll = useCallback(() => {
    setExpandedFolders(new Set())
  }, [])

  const handleSelectFile = useCallback(
    (filePath: string) => {
      setSelectedFilePath(filePath)
      onSelectFile?.(filePath)
      if (repositoryId) {
        loadFileContent(repositoryId, filePath)
      }
    },
    [repositoryId, onSelectFile, loadFileContent],
  )

  if (!repositoryId) {
    return (
      <section className="card-panel repo-explorer-panel">
        <h2 className="panel-header">Repository Explorer</h2>
        <div className="state-box">
          Select a ready repository from the sidebar to inspect its file structure.
        </div>
      </section>
    )
  }

  const treeNodes = buildFileTree(files)

  return (
    <section className="card-panel repo-explorer-panel" data-testid="repo-explorer-panel">
      <div className="explorer-header-row">
        <h2 className="panel-header" style={{ margin: 0 }}>
          Repository Explorer: <span className="mono">{repositoryName || repositoryId}</span>
        </h2>
        {files.length > 0 && !loading && !error && (
          <div className="explorer-controls">
            <button type="button" className="btn-action btn-sm" onClick={expandAll}>
              Expand All
            </button>
            <button type="button" className="btn-action btn-sm" onClick={collapseAll}>
              Collapse All
            </button>
          </div>
        )}
      </div>

      <p className="panel-text explorer-guidance" style={{ marginTop: '8px', marginBottom: '12px' }}>
        Choose a file to orient yourself, or search/ask a question about the repository.
      </p>

      {loading && (
        <div className="loading-state" style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '12px 0' }}>
          <span className="status-dot loading" />
          <span className="panel-text" style={{ margin: 0 }}>Loading repository files...</span>
        </div>
      )}

      {error && !loading && (
        <div className="explorer-error-box">
          <div className="form-error" style={{ marginBottom: '12px' }}>
            {error}
          </div>
          <button type="button" className="btn-action btn-retry" onClick={handleRetryFiles}>
            Retry Loading Files
          </button>
        </div>
      )}

      {!loading && !error && files.length === 0 && (
        <div className="state-box">
          No indexed files available for this repository.
        </div>
      )}

      {!loading && !error && files.length > 0 && (
        <div className="file-tree-container" data-testid="file-tree-container">
          {treeNodes.map((node) => (
            <TreeItemView
              key={node.path}
              node={node}
              expandedFolders={expandedFolders}
              toggleFolder={toggleFolder}
              selectedFilePath={selectedFilePath}
              onSelectFile={handleSelectFile}
            />
          ))}
        </div>
      )}

      {selectedFilePath && (
        <CodeViewer
          selectedPath={selectedFilePath}
          contentData={fileContent}
          loading={contentLoading}
          error={contentError}
          onRetry={handleRetryContent}
          highlightLineRange={selectedFilePath === targetFilePath ? targetLineRange : null}
        />
      )}
    </section>
  )
}
