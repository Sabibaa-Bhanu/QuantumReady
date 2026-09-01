"""
github_scanner.py — Live GitHub Repository Scanner for QuantumReady

Fetches repository file trees via GitHub REST API, downloads source files,
filters out binaries/dependencies/lockfiles, and scans code using QuantumReady's
existing regex and risk analysis engine.
"""

import os
import re
import base64
from typing import Dict, List, Any, Optional, Tuple, Callable
import urllib.request
import urllib.error
import json

import scanner
import risk_engine


class GitHubScanError(Exception):
    """Base exception for GitHub scanning errors."""
    pass


class GitHubRateLimitError(GitHubScanError):
    """Raised when GitHub REST API rate limit is exceeded."""
    def __init__(self, message: str = "GitHub API rate limit exceeded. Please set GITHUB_TOKEN environment variable.", reset_time: Optional[int] = None):
        super().__init__(message)
        self.reset_time = reset_time


class GitHubRepoNotFoundError(GitHubScanError):
    """Raised when repository or branch is not found or is private."""
    pass


# Directories to completely skip during repo scanning
IGNORED_DIRECTORIES = {
    'node_modules', 'vendor', '.git', '.github', 'dist', 'build', 'out',
    'target', 'venv', '.venv', 'env', '.env', '__pycache__', '.pytest_cache',
    '.vscode', '.idea', 'coverage', '.next', '.nuxt', 'bin', 'obj',
    'Pods', 'Carthage', 'test_data', 'fixtures'
}

# Specific filenames (like lockfiles) to skip
IGNORED_FILENAMES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'cargo.lock',
    'gemfile.lock', 'composer.lock', 'poetry.lock', 'mix.lock',
    'packages.lock.json', 'nuget.config'
}

MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1 MB maximum per source file


def get_github_token() -> Optional[str]:
    """Retrieve GitHub token from environment variables if set."""
    return os.environ.get('GITHUB_TOKEN') or os.environ.get('GITHUB_PAT') or None


def parse_github_url(url: str, branch: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    """
    Parse a GitHub repository URL into (owner, repo, branch).
    Supports:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - https://github.com/owner/repo/tree/branch_name
      - http(s)://github.com/owner/repo/tree/feature/sub-branch
      - github.com/owner/repo
      - owner/repo
    """
    clean_url = url.strip().rstrip('/')
    # Strip optional .git suffix
    if clean_url.endswith('.git'):
        clean_url = clean_url[:-4]

    # Regex pattern matching GitHub URLs
    pattern = r"^(?:https?:\/\/)?(?:www\.)?github\.com\/([^\/\s]+)\/([^\/\s]+)(?:\/tree\/([^\/\s]+(?:\/[^\/\s]+)*))?"
    match = re.match(pattern, clean_url, re.IGNORECASE)

    if match:
        owner = match.group(1)
        repo = match.group(2)
        url_branch = match.group(3)
        final_branch = branch or url_branch
        return owner, repo, final_branch

    # Fallback pattern for shorthand 'owner/repo'
    short_pattern = r"^([a-zA-Z0-9_\-\.]+)\/([a-zA-Z0-9_\-\.]+)$"
    short_match = re.match(short_pattern, clean_url)
    if short_match:
        return short_match.group(1), short_match.group(2), branch

    raise GitHubScanError(f"Invalid GitHub repository URL: '{url}'. Expected format: 'https://github.com/owner/repo'")


def make_github_request(url: str, token: Optional[str] = None, accept: str = "application/vnd.github+json") -> Tuple[int, Dict[str, Any], Any]:
    """
    Execute an HTTP request to the GitHub API or raw content URL.
    Returns (status_code, headers_dict, decoded_body_or_json).
    """
    headers = {
        "User-Agent": "QuantumReady-PQC-Scanner/2.0",
        "Accept": accept,
    }
    auth_token = token or get_github_token()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            body = resp.read()
            if "application/json" in resp_headers.get("Content-Type", ""):
                try:
                    data = json.loads(body.decode('utf-8'))
                except Exception:
                    data = body
            else:
                data = body
            return status, resp_headers, data
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = dict(e.headers)
        err_body = e.read().decode('utf-8', errors='ignore')
        remaining = resp_headers.get("x-ratelimit-remaining") or resp_headers.get("X-RateLimit-Remaining")

        if status in (403, 429) and (remaining == "0" or "rate limit" in err_body.lower()):
            reset_time = resp_headers.get("x-ratelimit-reset") or resp_headers.get("X-RateLimit-Reset")
            raise GitHubRateLimitError(
                "GitHub API rate limit exceeded. Provide a personal access token via GITHUB_TOKEN environment variable to increase limit.",
                reset_time=int(reset_time) if reset_time and reset_time.isdigit() else None
            )

        if status == 404:
            raise GitHubRepoNotFoundError("GitHub repository or branch not found. Verify the URL and branch name (or ensure token has repo access if private).")

        raise GitHubScanError(f"GitHub API returned error {status}: {err_body}")
    except urllib.error.URLError as e:
        raise GitHubScanError(f"Failed to connect to GitHub: {str(e.reason)}")
    except Exception as e:
        raise GitHubScanError(f"Unexpected error communicating with GitHub: {str(e)}")


def get_default_branch(owner: str, repo: str, token: Optional[str] = None) -> str:
    """Fetch default branch for repository (e.g. 'main' or 'master')."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    _, _, data = make_github_request(url, token=token)
    if isinstance(data, dict) and 'default_branch' in data:
        return data['default_branch']
    return 'main'


def fetch_repository_tree(owner: str, repo: str, branch: str, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch complete recursive git tree for owner/repo@branch."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    _, _, data = make_github_request(url, token=token)
    if isinstance(data, dict) and 'tree' in data:
        return data['tree']
    raise GitHubScanError("Invalid response format when retrieving repository tree from GitHub.")


def filter_source_files(tree_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filter git tree items to only keep supported source files,
    ignoring binaries, vendor/node_modules directories, and lockfiles.
    """
    filtered = []
    for item in tree_items:
        if item.get('type') != 'blob':
            continue

        path = item.get('path', '')
        size = item.get('size', 0)

        # Skip oversized files
        if size > MAX_FILE_SIZE_BYTES:
            continue

        # Check ignored path segments
        path_parts = path.replace('\\', '/').split('/')
        if any(part in IGNORED_DIRECTORIES for part in path_parts[:-1]):
            continue

        filename = path_parts[-1]
        if filename.lower() in IGNORED_FILENAMES:
            continue

        # Check extension against scanner.SUPPORTED_EXTENSIONS
        _, ext = os.path.splitext(filename)
        if ext.lower() in scanner.SUPPORTED_EXTENSIONS:
            filtered.append(item)

    return filtered


def fetch_raw_file_content(owner: str, repo: str, branch: str, file_path: str, token: Optional[str] = None) -> str:
    """
    Fetch raw file content from GitHub using raw.githubusercontent.com
    with fallback to the GitHub contents API.
    """
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    try:
        _, _, data = make_github_request(raw_url, token=token, accept="text/plain")
        if isinstance(data, bytes):
            return data.decode('utf-8', errors='ignore')
        return str(data)
    except Exception:
        # Fallback to GitHub Contents API
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={branch}"
        _, _, data = make_github_request(api_url, token=token)
        if isinstance(data, dict) and 'content' in data:
            encoding = data.get('encoding', '')
            if encoding == 'base64':
                content_bytes = base64.b64decode(data['content'])
                return content_bytes.decode('utf-8', errors='ignore')
        raise GitHubScanError(f"Could not retrieve content for file '{file_path}'")


def scan_single_code_content(file_path: str, content: str) -> Dict[str, Any]:
    """
    Run code through existing QuantumReady scanner logic without saving to disk.
    Reuses scanner.scan_text_with_lines and scanner.calculate_score.
    """
    findings = scanner.scan_text_with_lines(content)
    score, label = scanner.calculate_score(findings)
    matches = list(dict.fromkeys([f["vulnerability_type"] for f in findings]))
    return {
        'path': file_path,
        'matches': matches,
        'findings': findings,
        'score': score,
        'label': label,
    }


def scan_github_repository(
    repo_url: str,
    branch: Optional[str] = None,
    token: Optional[str] = None,
    on_file_scanned: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    End-to-end scanning of a GitHub repository:
      1. Parses URL and resolves default branch if not provided.
      2. Retrieves recursive file tree.
      3. Filters source files.
      4. Downloads and runs each file through scanner logic.
      5. Invokes on_file_scanned callback for each file (if provided) for real-time streaming.
      6. Returns comprehensive results summary.
    """
    owner, repo, resolved_branch = parse_github_url(repo_url, branch=branch)
    if not resolved_branch:
        resolved_branch = get_default_branch(owner, repo, token=token)

    tree = fetch_repository_tree(owner, repo, resolved_branch, token=token)
    source_files = filter_source_files(tree)
    total_files = len(source_files)

    files_result = []
    summary = {k: 0 for k in scanner.VULNERABILITY_PATTERNS.keys()}
    all_findings = []

    for idx, item in enumerate(source_files, start=1):
        file_path = item['path']
        try:
            content = fetch_raw_file_content(owner, repo, resolved_branch, file_path, token=token)
            result = scan_single_code_content(file_path, content)
        except Exception as e:
            # If a single file fails to download, record safe empty result so scan continues
            result = {
                'path': file_path,
                'matches': [],
                'findings': [],
                'score': 100,
                'label': 'SAFE',
                'error': str(e),
            }

        for m in result['matches']:
            summary[m] = summary.get(m, 0) + 1
        all_findings.extend(result['findings'])
        files_result.append(result)

        if on_file_scanned:
            # Emit callback matching specification:
            # { file_path, findings: [...], score, label, progress: {scanned, total} }
            on_file_scanned({
                'file_path': file_path,
                'findings': result['findings'],
                'score': result['score'],
                'label': result['label'],
                'progress': {'scanned': idx, 'total': total_files},
            })

    overall_score, overall_label = scanner.calculate_score(all_findings)
    raw_findings_dict = {
        'files': files_result,
        'summary': summary,
        'overall_score': overall_score,
        'overall_label': overall_label,
        'total_findings': len(all_findings),
    }

    # Analyze findings through QuantumReady's rule engine
    analysis = risk_engine.analyze_findings(raw_findings_dict)

    return {
        'repo': f"{owner}/{repo}",
        'branch': resolved_branch,
        'total_files': total_files,
        'overall_score': overall_score,
        'overall_label': overall_label,
        'total_findings': len(all_findings),
        'summary': summary,
        'analysis': analysis,
        'files': files_result,
    }
