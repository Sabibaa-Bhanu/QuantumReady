"""
github_scanner.py — Live GitHub Repository Scanner for QuantumReady

Fetches repository files via high-speed GitHub zip streaming with fallback
to GitHub REST API tree recursion. Scans code using QuantumReady's
regex and risk analysis engine with full-file auto-remediation.
"""

import os
import re
import base64
import tempfile
import zipfile
import shutil
from typing import Dict, List, Any, Optional, Tuple, Callable
import urllib.request
import urllib.error
import json

import scanner
import risk_engine
import fix_suggester


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
    if clean_url.endswith('.git'):
        clean_url = clean_url[:-4]

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
    """Execute an HTTP request to GitHub API or raw URL."""
    headers = {
        "User-Agent": "QuantumReady-PQC-Scanner/2.0",
        "Accept": accept,
    }
    auth_token = token or get_github_token()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
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
            raise GitHubRepoNotFoundError("GitHub repository or branch not found. Verify the URL and branch name.")

        raise GitHubScanError(f"GitHub returned HTTP {status}: {err_body}")
    except urllib.error.URLError as e:
        raise GitHubScanError(f"Failed to connect to GitHub: {str(e.reason)}")
    except Exception as e:
        raise GitHubScanError(f"Unexpected error communicating with GitHub: {str(e)}")


def get_default_branch(owner: str, repo: str, token: Optional[str] = None) -> str:
    """Fetch default branch for repository (e.g. 'main' or 'master')."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        _, _, data = make_github_request(url, token=token)
        if isinstance(data, dict) and 'default_branch' in data:
            return data['default_branch']
    except Exception:
        pass
    return 'main'


def fetch_repository_tree(owner: str, repo: str, branch: str, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch complete recursive git tree for owner/repo@branch."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    _, _, data = make_github_request(url, token=token)
    if isinstance(data, dict) and 'tree' in data:
        return data['tree']
    raise GitHubScanError("Invalid response format when retrieving repository tree from GitHub.")


def filter_source_files(tree_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter git tree items to only keep supported source files."""
    filtered = []
    for item in tree_items:
        if item.get('type') != 'blob':
            continue

        path = item.get('path', '')
        size = item.get('size', 0)

        if size > MAX_FILE_SIZE_BYTES:
            continue

        path_parts = path.replace('\\', '/').split('/')
        if any(part in IGNORED_DIRECTORIES for part in path_parts[:-1]):
            continue

        filename = path_parts[-1]
        if filename.lower() in IGNORED_FILENAMES:
            continue

        _, ext = os.path.splitext(filename)
        if ext.lower() in scanner.SUPPORTED_EXTENSIONS:
            filtered.append(item)

    return filtered


def fetch_raw_file_content(owner: str, repo: str, branch: str, file_path: str, token: Optional[str] = None) -> str:
    """Fetch raw file content from GitHub raw usercontent or contents API."""
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    try:
        _, _, data = make_github_request(raw_url, token=token, accept="text/plain")
        if isinstance(data, bytes):
            return data.decode('utf-8', errors='ignore')
        return str(data)
    except Exception:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={branch}"
        _, _, data = make_github_request(api_url, token=token)
        if isinstance(data, dict) and 'content' in data:
            encoding = data.get('encoding', '')
            if encoding == 'base64':
                content_bytes = base64.b64decode(data['content'])
                return content_bytes.decode('utf-8', errors='ignore')
        raise GitHubScanError(f"Could not retrieve content for file '{file_path}'")


def scan_single_code_content(file_path: str, content: str) -> Dict[str, Any]:
    """Run code through QuantumReady scanner and full auto-remediator."""
    findings = scanner.scan_text_with_lines(content)
    score, label = scanner.calculate_score(findings)
    matches = list(dict.fromkeys([f["vulnerability_type"] for f in findings]))
    
    try:
        fixed_code, changelog = fix_suggester.remediate_full_file(content, findings, filename=file_path)
    except Exception:
        fixed_code, changelog = content, []

    return {
        'path': file_path,
        'matches': matches,
        'findings': findings,
        'score': score,
        'label': label,
        'original_code': content,
        'fixed_code': fixed_code,
        'changelog': changelog,
    }


def download_github_zip_archive(owner: str, repo: str, branch: Optional[str], token: Optional[str] = None) -> Tuple[str, str]:
    """
    Download repository archive as a single zip file.
    Returns (temp_dir_path, detected_branch).
    """
    candidates = [branch] if branch else ['main', 'master', 'trunk', 'develop']
    headers = {
        "User-Agent": "QuantumReady-PQC-Scanner/2.0",
        "Accept": "*/*"
    }
    auth_token = token or get_github_token()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    for b in candidates:
        if not b:
            continue
        zip_urls = [
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{b}",
            f"https://github.com/{owner}/{repo}/archive/refs/heads/{b}.zip"
        ]
        for url in zip_urls:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200:
                        tmp_zip = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
                        shutil.copyfileobj(resp, tmp_zip)
                        tmp_zip.close()
                        
                        # Extract zip
                        extract_dir = tempfile.mkdtemp(prefix='qr_gh_')
                        with zipfile.ZipFile(tmp_zip.name, 'r') as z:
                            z.extractall(extract_dir)
                        try:
                            os.remove(tmp_zip.name)
                        except Exception:
                            pass
                        return extract_dir, b
            except Exception:
                continue

    raise GitHubRepoNotFoundError(f"Could not download repository archive for {owner}/{repo}. Verify repository visibility.")


def scan_github_repository(
    repo_url: str,
    branch: Optional[str] = None,
    token: Optional[str] = None,
    on_file_scanned: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    End-to-end scanning of a GitHub repository with real-time streaming progress:
      1. Downloads repo archive (fast, zero rate-limit issues) with fallback to REST API tree.
      2. Scans each source file through scanner & auto-remediator.
      3. Invokes on_file_scanned callback per file for real-time progress bar.
      4. Returns structured results summary and analysis.
    """
    owner, repo, resolved_branch = parse_github_url(repo_url, branch=branch)
    files_result = []
    summary = {k: 0 for k in scanner.VULNERABILITY_PATTERNS.keys()}
    all_findings = []
    total_files = 0
    final_branch = resolved_branch or 'main'

    # Strategy 1: High-Speed Zip Streaming
    try:
        extract_dir, final_branch = download_github_zip_archive(owner, repo, resolved_branch, token=token)
        # Find inner repo root folder if present
        root_dir = extract_dir
        inner = os.listdir(extract_dir)
        if len(inner) == 1 and os.path.isdir(os.path.join(extract_dir, inner[0])):
            root_dir = os.path.join(extract_dir, inner[0])

        collected_paths = []
        for dirpath, _, filenames in os.walk(root_dir):
            rel_dir = os.path.relpath(dirpath, root_dir).replace('\\', '/')
            parts = rel_dir.split('/')
            if any(p in IGNORED_DIRECTORIES for p in parts if p and p != '.'):
                continue
            for fn in filenames:
                if fn.lower() in IGNORED_FILENAMES:
                    continue
                _, ext = os.path.splitext(fn)
                if ext.lower() in scanner.SUPPORTED_EXTENSIONS:
                    full_p = os.path.join(dirpath, fn)
                    rel_p = os.path.relpath(full_p, root_dir).replace('\\', '/')
                    collected_paths.append((full_p, rel_p))

        total_files = len(collected_paths)
        for idx, (full_p, rel_p) in enumerate(collected_paths, start=1):
            try:
                with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                res = scan_single_code_content(rel_p, content)
            except Exception as e:
                res = {
                    'path': rel_p,
                    'matches': [],
                    'findings': [],
                    'score': 100,
                    'label': 'SAFE',
                    'original_code': '',
                    'fixed_code': '',
                    'changelog': [],
                    'error': str(e)
                }

            for m in res['matches']:
                summary[m] = summary.get(m, 0) + 1
            all_findings.extend(res['findings'])
            files_result.append(res)

            if on_file_scanned:
                on_file_scanned({
                    'file_path': rel_p,
                    'findings': res['findings'],
                    'score': res['score'],
                    'label': res['label'],
                    'progress': {'scanned': idx, 'total': total_files},
                })

        # Cleanup extracted folder
        try:
            shutil.rmtree(extract_dir, ignore_errors=True)
        except Exception:
            pass

    except Exception:
        # Strategy 2: REST API Tree Fallback
        if not resolved_branch:
            resolved_branch = get_default_branch(owner, repo, token=token)
            final_branch = resolved_branch

        tree = fetch_repository_tree(owner, repo, resolved_branch, token=token)
        source_files = filter_source_files(tree)
        total_files = len(source_files)

        for idx, item in enumerate(source_files, start=1):
            file_path = item['path']
            try:
                content = fetch_raw_file_content(owner, repo, resolved_branch, file_path, token=token)
                result = scan_single_code_content(file_path, content)
            except Exception as e:
                result = {
                    'path': file_path,
                    'matches': [],
                    'findings': [],
                    'score': 100,
                    'label': 'SAFE',
                    'original_code': '',
                    'fixed_code': '',
                    'changelog': [],
                    'error': str(e),
                }

            for m in result['matches']:
                summary[m] = summary.get(m, 0) + 1
            all_findings.extend(result['findings'])
            files_result.append(result)

            if on_file_scanned:
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

    analysis = risk_engine.analyze_findings(raw_findings_dict)

    return {
        'repo': f"{owner}/{repo}",
        'branch': final_branch,
        'total_files': total_files,
        'overall_score': overall_score,
        'overall_label': overall_label,
        'total_findings': len(all_findings),
        'summary': summary,
        'analysis': analysis,
        'files': files_result,
    }
