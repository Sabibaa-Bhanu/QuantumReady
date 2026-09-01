"""
tests/test_github_scanner.py — Test suite for GitHub Live Scanner
"""

import pytest
from unittest.mock import patch, MagicMock
import urllib.error

import github_scanner
from github_scanner import (
    parse_github_url,
    filter_source_files,
    scan_single_code_content,
    scan_github_repository,
    GitHubScanError,
    GitHubRateLimitError,
    GitHubRepoNotFoundError,
)


class TestGitHubUrlParsing:
    def test_standard_https_url(self):
        owner, repo, branch = parse_github_url("https://github.com/torvalds/linux")
        assert owner == "torvalds"
        assert repo == "linux"
        assert branch is None

    def test_url_with_git_extension(self):
        owner, repo, branch = parse_github_url("https://github.com/psf/requests.git")
        assert owner == "psf"
        assert repo == "requests"
        assert branch is None

    def test_url_with_branch(self):
        owner, repo, branch = parse_github_url("https://github.com/owner/repo/tree/feature-pqc")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "feature-pqc"

    def test_url_with_explicit_branch_override(self):
        owner, repo, branch = parse_github_url("https://github.com/owner/repo", branch="develop")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "develop"

    def test_shorthand_url(self):
        owner, repo, branch = parse_github_url("quantum/crypto-demo")
        assert owner == "quantum"
        assert repo == "crypto-demo"
        assert branch is None

    def test_invalid_url(self):
        with pytest.raises(GitHubScanError):
            parse_github_url("invalid-url-with-no-slash")


class TestSourceFileFiltering:
    def test_filters_node_modules_and_lockfiles(self):
        tree = [
            {'type': 'blob', 'path': 'src/index.js', 'size': 500},
            {'type': 'blob', 'path': 'node_modules/express/index.js', 'size': 500},
            {'type': 'blob', 'path': 'vendor/lib.py', 'size': 1200},
            {'type': 'blob', 'path': 'package-lock.json', 'size': 2000},
            {'type': 'blob', 'path': 'yarn.lock', 'size': 1500},
            {'type': 'blob', 'path': 'app/crypto.py', 'size': 800},
            {'type': 'blob', 'path': 'assets/logo.png', 'size': 4000},
            {'type': 'tree', 'path': 'src', 'size': 0},
        ]
        filtered = filter_source_files(tree)
        paths = [item['path'] for item in filtered]
        assert 'src/index.js' in paths
        assert 'app/crypto.py' in paths
        assert 'node_modules/express/index.js' not in paths
        assert 'vendor/lib.py' not in paths
        assert 'package-lock.json' not in paths
        assert 'yarn.lock' not in paths
        assert 'assets/logo.png' not in paths
        assert 'src' not in paths

    def test_filters_large_files(self):
        tree = [
            {'type': 'blob', 'path': 'small.py', 'size': 1000},
            {'type': 'blob', 'path': 'huge.js', 'size': 2 * 1024 * 1024},  # 2MB
        ]
        filtered = filter_source_files(tree)
        assert len(filtered) == 1
        assert filtered[0]['path'] == 'small.py'


class TestCryptoScanningLogic:
    def test_detects_rsa_and_weak_hash(self):
        code = (
            "from Crypto.PublicKey import RSA\n"
            "import hashlib\n"
            "key = RSA.generate(2048)\n"
            "md5_hash = hashlib.md5(b'data').hexdigest()\n"
        )
        res = scan_single_code_content("test.py", code)
        assert "RSA" in res['matches']
        assert "MD5" in res['matches']
        assert res['score'] < 100
        assert len(res['findings']) >= 2

    def test_safe_code(self):
        code = (
            "# Safe modern PQC code\n"
            "import oqs\n"
            "kem = oqs.KeyEncapsulation('Kyber512')\n"
        )
        res = scan_single_code_content("safe.py", code)
        assert res['score'] == 100
        assert res['label'] == "SAFE"


class TestGitHubRateLimiting:
    @patch('urllib.request.urlopen')
    def test_rate_limit_detection(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo/git/trees/main",
            code=403,
            msg="Forbidden",
            hdrs={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1725200000"},
            fp=MagicMock(read=lambda: b'{"message": "API rate limit exceeded"}')
        )
        mock_urlopen.side_effect = err

        with pytest.raises(GitHubRateLimitError) as exc_info:
            github_scanner.fetch_repository_tree("owner", "repo", "main")
        assert "rate limit exceeded" in str(exc_info.value).lower()


class TestScanGitHubRepositoryWorkflow:
    @patch('github_scanner.get_default_branch')
    @patch('github_scanner.fetch_repository_tree')
    @patch('github_scanner.fetch_raw_file_content')
    def test_streaming_callback_and_completion(self, mock_fetch_content, mock_fetch_tree, mock_default_branch):
        mock_default_branch.return_value = "main"
        mock_fetch_tree.return_value = [
            {'type': 'blob', 'path': 'crypto.py', 'size': 100},
            {'type': 'blob', 'path': 'util.py', 'size': 80},
        ]
        mock_fetch_content.side_effect = [
            "from Crypto.PublicKey import RSA\nkey = RSA.generate(2048)\n",
            "def add(a, b): return a + b\n"
        ]

        streamed_events = []
        def on_scanned(event_data):
            streamed_events.append(event_data)

        result = scan_github_repository(
            "https://github.com/demo/repo",
            on_file_scanned=on_scanned
        )

        assert len(streamed_events) == 2
        assert streamed_events[0]['file_path'] == 'crypto.py'
        assert streamed_events[0]['progress'] == {'scanned': 1, 'total': 2}
        assert len(streamed_events[0]['findings']) > 0

        assert streamed_events[1]['file_path'] == 'util.py'
        assert streamed_events[1]['progress'] == {'scanned': 2, 'total': 2}
        assert len(streamed_events[1]['findings']) == 0

        assert result['repo'] == "demo/repo"
        assert result['branch'] == "main"
        assert result['total_files'] == 2
        assert "RSA" in result['summary']
        assert result['summary']['RSA'] >= 1


from app import app, socketio


class TestFlaskAppEndpoints:
    @pytest.fixture
    def client(self):
        app.config['TESTING'] = True
        with app.test_client() as client:
            yield client

    def test_api_scan_github_validation(self, client):
        resp = client.post('/api/scan-github', json={})
        assert resp.status_code == 400
        assert 'repo_url is required' in resp.get_json()['error']

    @patch('github_scanner.scan_github_repository')
    def test_api_scan_github_success(self, mock_scan, client):
        mock_scan.return_value = {
            'repo': 'owner/repo',
            'branch': 'main',
            'total_files': 1,
            'overall_score': 80,
            'overall_label': 'SAFE',
            'total_findings': 0,
            'summary': {'RSA': 0, 'ECC': 0, 'MD5': 0, 'SHA1': 0, 'DiffieHellman': 0, 'WeakTLS': 0, 'AES': 0, 'PQC': 0},
            'analysis': {'files': [], 'summary': {}, 'recommendations': [], 'vulnerabilities': []},
            'files': []
        }
        resp = client.post('/api/scan-github', json={'repo_url': 'https://github.com/owner/repo'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['repo'] == 'owner/repo'
        assert 'ml_prediction' in data

    @patch('github_scanner.scan_github_repository')
    def test_api_scan_github_rate_limited(self, mock_scan, client):
        mock_scan.side_effect = github_scanner.GitHubRateLimitError("Rate limited")
        resp = client.post('/api/scan-github', json={'repo_url': 'https://github.com/owner/repo'})
        assert resp.status_code == 429
        data = resp.get_json()
        assert data['rate_limited'] is True


class TestSocketIOStreaming:
    def test_socketio_connect(self):
        client = socketio.test_client(app)
        assert client.is_connected()
        received = client.get_received()
        assert any(event['name'] == 'connected' for event in received)

    @patch('github_scanner.scan_github_repository')
    def test_socketio_github_scan_flow(self, mock_scan):
        def fake_scan(repo_url, branch=None, token=None, on_file_scanned=None):
            if on_file_scanned:
                on_file_scanned({
                    'file_path': 'test.py',
                    'findings': [],
                    'score': 100,
                    'label': 'SAFE',
                    'progress': {'scanned': 1, 'total': 1}
                })
            return {
                'repo': 'test/repo',
                'branch': 'main',
                'total_files': 1,
                'overall_score': 100,
                'overall_label': 'SAFE',
                'total_findings': 0,
                'summary': {'RSA': 0, 'ECC': 0, 'MD5': 0, 'SHA1': 0, 'DiffieHellman': 0, 'WeakTLS': 0, 'AES': 0, 'PQC': 0},
                'analysis': {'files': [], 'summary': {}, 'recommendations': [], 'vulnerabilities': []},
                'files': []
            }
        mock_scan.side_effect = fake_scan

        client = socketio.test_client(app)
        client.get_received()  # Clear connect events

        client.emit('start_github_scan', {'repo_url': 'https://github.com/test/repo'})
        events = client.get_received()
        event_names = [e['name'] for e in events]
        assert 'scan_status' in event_names
        assert 'file_scanned' in event_names
        assert 'scan_complete' in event_names
