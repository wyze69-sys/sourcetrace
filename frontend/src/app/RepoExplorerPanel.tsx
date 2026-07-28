import { useCallback, useEffect, useState } from 'react'
import { apiClient as defaultApiClient, ApiError, type ApiClient } from '../services/apiClient'
import type { RepositoryFileItem } from '../services/types'

export interface RepoExplorerPanelProps {
  client?: ApiClient
  repositoryId: string | null
  repositoryName?: string
  onSelectFile?: (filePath: string) => void
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

    const folderNodes: TreeNodeFolder[] = sortedSubfolders.map((sf) => ({
      type: 'folder',
      name: sf.name,
      path: sf.path,
      children: convertRawToTree(sf.subfolders, sf.files),
    }))

    return [...folderNodes, ...sortedFiles]
  }

  return convertRawToTree(rootSubfolders, rootFiles)
}

/**
 * Extracts all folder paths from flat file items.
 */
export function extractAllFolderPaths(files: RepositoryFileItem[]): Set<string> {
  const folderPaths = new Set<string>()
  for (const item of files) {
    const parts = item.path.split('/').filter(Boolean)
    let accum = ''
    for (let i = 0; i < parts.length - 1; i++) {
      accum = accum ? `${accum}/${parts[i]}` : parts[i]
      folderPaths.add(accum)
    }
  }
  return folderPaths
}

function TreeItemView({
  node,
  expandedFolders,
  toggleFolder,
  selectedFilePath,
  onSelectFile,
}: {
  node: TreeNode
  expandedFolders: Set<string>
  toggleFolder: (path: string) => void
  selectedFilePath: string | null
  onSelectFile: (filePath: string) => void
}) {
  if (node.type === 'folder') {
    const isExpanded = expandedFolders.has(node.path)
    return (
      <div className="tree-folder-group" data-testid={`folder-${node.path}`}>
        <button
          type="button"
          className="tree-item tree-folder"
          onClick={() => toggleFolder(node.path)}
          aria-expanded={isExpanded}
        >
          <span className="tree-icon">{isExpanded ? '▾' : '▸'}</span>
          <span className="tree-name folder-name">{node.name}/</span>
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
      className={`tree-item tree-file ${isSelected ? 'selected' : ''}`}
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

export function RepoExplorerPanel({
  client = defaultApiClient,
  repositoryId,
  repositoryName,
  onSelectFile,
}: RepoExplorerPanelProps) {
  const [files, setFiles] = useState<RepositoryFileItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null)
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set())

  const fetchFiles = useCallback(async () => {
    if (!repositoryId || typeof client.listRepositoryFiles !== 'function') {
      setFiles([])
      setLoading(false)
      setError(null)
      setSelectedFilePath(null)
      return
    }

    setLoading(true)
    setError(null)

    try {
      const res = await client.listRepositoryFiles(repositoryId)
      if (res.repository_id !== repositoryId) {
        return
      }
      setFiles(res.files || [])
      setExpandedFolders(extractAllFolderPaths(res.files || []))
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Failed to load repository files.')
      }
    } finally {
      setLoading(false)
    }
  }, [client, repositoryId])

  useEffect(() => {
    let isCancelled = false

    if (!repositoryId || typeof client.listRepositoryFiles !== 'function') {
      setFiles([])
      setLoading(false)
      setError(null)
      setSelectedFilePath(null)
      return
    }

    setLoading(true)
    setError(null)
    setSelectedFilePath(null)

    client
      .listRepositoryFiles(repositoryId)
      .then((res) => {
        if (isCancelled) return
        if (res.repository_id !== repositoryId) return
        setFiles(res.files || [])
        setExpandedFolders(extractAllFolderPaths(res.files || []))
        setLoading(false)
      })
      .catch((err) => {
        if (isCancelled) return
        setLoading(false)
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('Failed to load repository files.')
        }
      })

    return () => {
      isCancelled = true
    }
  }, [client, repositoryId])

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
    },
    [onSelectFile],
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
          <button type="button" className="btn-action btn-retry" onClick={fetchFiles}>
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
    </section>
  )
}
