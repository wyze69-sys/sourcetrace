"""GitHub repository freshness detection and in-memory TTL caching."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from sourcetrace.ingestion.github_archive import (
    DOWNLOAD_TIMEOUT_SECONDS,
    _default_dns_resolver,
    _validate_target_url_and_ip,
)
from sourcetrace.ingestion.validation import validate_github_url


class GitHubRefCache:
    """Bounded in-memory cache for remote GitHub ref commit SHAs with a 5-minute TTL."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 500) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._cache: dict[tuple[str, str, str], tuple[str | None, datetime]] = {}
        self._lock = threading.Lock()

    def get(
        self, owner: str, repo: str, branch: str, now: datetime | None = None
    ) -> tuple[bool, str | None]:
        now_dt = now or datetime.now(UTC)
        key = (owner.lower(), repo.lower(), branch)
        with self._lock:
            if key in self._cache:
                sha, cached_at = self._cache[key]
                if (now_dt - cached_at).total_seconds() < self._ttl_seconds:
                    return True, sha
                del self._cache[key]
        return False, None

    def set(
        self,
        owner: str,
        repo: str,
        branch: str,
        sha: str | None,
        now: datetime | None = None,
    ) -> None:
        now_dt = now or datetime.now(UTC)
        key = (owner.lower(), repo.lower(), branch)
        with self._lock:
            if len(self._cache) >= self._max_entries:
                oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]
            self._cache[key] = (sha, now_dt)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_GLOBAL_REF_CACHE = GitHubRefCache(ttl_seconds=300.0, max_entries=500)


def get_global_ref_cache() -> GitHubRefCache:
    """Return the global process-shared GitHubRefCache instance."""
    return _GLOBAL_REF_CACHE


def fetch_remote_branch_sha(
    owner: str,
    repo: str,
    branch: str,
    client: httpx.Client | None = None,
    resolver: Callable[[str], list[str]] | None = None,
) -> str | None:
    """Fetch current HEAD commit SHA for a remote branch via public GitHub API.

    Guaranteed never to raise an exception. On rate limit, 404, network failure, or timeout,
    safely returns None without leaking internal details.
    """
    dns_resolver = resolver or _default_dns_resolver
    commits_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"

    try:
        _validate_target_url_and_ip(
            commits_url,
            is_redirect=False,
            resolver=dns_resolver,
        )
    except Exception:
        return None

    own_client = client is None
    http_client = client or httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS),
    )

    try:
        headers = {
            "User-Agent": "SourceTrace/1.0",
            "Accept": "application/vnd.github+json",
        }
        res = http_client.get(commits_url, headers=headers)
        if res.status_code == 200:
            cdata = res.json()
            if isinstance(cdata, dict):
                raw_sha = cdata.get("sha")
                if isinstance(raw_sha, str) and raw_sha.strip():
                    return raw_sha.strip()
        return None
    except Exception:
        return None
    finally:
        if own_client:
            try:
                http_client.close()
            except Exception:  # noqa: BLE001
                pass


def check_github_freshness(
    github_url: str,
    indexed_branch: str,
    indexed_commit_sha: str,
    cache: GitHubRefCache | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[[str], list[str]] | None = None,
    now: datetime | None = None,
) -> tuple[bool | None, str | None]:
    """Check if remote branch SHA differs from indexed_commit_sha.

    Returns (is_stale, remote_sha).
    If remote lookup fails or SHA cannot be resolved, returns (None, None).
    """
    try:
        owner, repo = validate_github_url(github_url)
    except Exception:
        return None, None

    ref_cache = cache or get_global_ref_cache()
    now_dt = now or datetime.now(UTC)

    hit, cached_sha = ref_cache.get(owner, repo, indexed_branch, now=now_dt)
    if hit:
        if cached_sha is None:
            return None, None
        return (cached_sha != indexed_commit_sha), cached_sha

    remote_sha = fetch_remote_branch_sha(
        owner=owner,
        repo=repo,
        branch=indexed_branch,
        client=client,
        resolver=resolver,
    )

    if remote_sha is None:
        return None, None

    ref_cache.set(owner, repo, indexed_branch, remote_sha, now=now_dt)
    return (remote_sha != indexed_commit_sha), remote_sha
