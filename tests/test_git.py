from unittest.mock import MagicMock, patch

import pytest

from ngn_agent.git import clone_repo, find_resume_branch


def _make_result(returncode=0, stderr="", stdout=""):
    r = MagicMock()
    r.returncode = returncode
    r.stderr = stderr
    r.stdout = stdout
    return r


def test_clone_repo_calls_git(tmp_path):
    dest = tmp_path / "repo"
    with patch("subprocess.run", return_value=_make_result()) as mock_run:
        clone_repo("https://github.com/example/repo.git", dest)
    mock_run.assert_called_once_with(
        ["git", "clone", "https://github.com/example/repo.git", str(dest)],
        capture_output=True,
        text=True,
    )


def test_clone_repo_creates_parent(tmp_path):
    dest = tmp_path / "nested" / "dir" / "repo"
    with patch("subprocess.run", return_value=_make_result()):
        clone_repo("https://github.com/example/repo.git", dest)
    assert dest.parent.exists()


def test_clone_repo_raises_on_nonzero_exit(tmp_path):
    dest = tmp_path / "repo"
    with patch("subprocess.run", return_value=_make_result(returncode=128, stderr="fatal: repository not found")):
        with pytest.raises(RuntimeError, match="git clone failed"):
            clone_repo("https://github.com/example/repo.git", dest)


def test_clone_repo_removes_existing_dest_and_reclones(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "stale_file.txt").write_text("old")
    with patch("subprocess.run", return_value=_make_result()) as mock_run:
        clone_repo("https://github.com/example/repo.git", dest)
    assert not (dest / "stale_file.txt").exists()
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# find_resume_branch
# ---------------------------------------------------------------------------

def test_find_resume_branch_returns_true_when_branch_exists():
    """Returns True when git ls-remote output contains a ref for the branch."""
    ref_output = "abc123\trefs/heads/ngn/PROJ-1\n"
    with patch("subprocess.run", return_value=_make_result(stdout=ref_output)):
        result = find_resume_branch("https://github.com/example/repo.git", "ngn/PROJ-1")
    assert result is True


def test_find_resume_branch_returns_false_when_branch_absent():
    """Returns False when git ls-remote output is empty (branch not found)."""
    with patch("subprocess.run", return_value=_make_result(stdout="")):
        result = find_resume_branch("https://github.com/example/repo.git", "ngn/PROJ-1")
    assert result is False


def test_find_resume_branch_returns_false_on_error():
    """Returns False (without propagating) when subprocess.run raises an exception."""
    with patch("subprocess.run", side_effect=Exception("git not found")):
        result = find_resume_branch("https://github.com/example/repo.git", "ngn/PROJ-1")
    assert result is False


# ---------------------------------------------------------------------------
# clone_repo — URL scheme validation
# ---------------------------------------------------------------------------

def test_clone_repo_accepts_https_url(tmp_path):
    """clone_repo must not raise for a well-formed https:// URL."""
    dest = tmp_path / "repo"
    with patch("subprocess.run", return_value=_make_result()):
        # Should not raise ValueError.
        clone_repo("https://github.com/example/repo.git", dest)


def test_clone_repo_accepts_git_at_url(tmp_path):
    """clone_repo must not raise for a well-formed git@ SSH URL."""
    dest = tmp_path / "repo"
    with patch("subprocess.run", return_value=_make_result()):
        clone_repo("git@github.com:example/repo.git", dest)


def test_clone_repo_rejects_file_url(tmp_path):
    """clone_repo must raise ValueError for a file:// URL."""
    dest = tmp_path / "repo"
    with pytest.raises(ValueError, match="Unsafe repository URL"):
        clone_repo("file:///etc/passwd", dest)


def test_clone_repo_rejects_bare_path(tmp_path):
    """clone_repo must raise ValueError for a bare filesystem path."""
    dest = tmp_path / "repo"
    with pytest.raises(ValueError, match="Unsafe repository URL"):
        clone_repo("/tmp/local-repo", dest)


def test_clone_repo_rejects_http_url(tmp_path):
    """clone_repo must raise ValueError for plain http:// (non-TLS) URLs."""
    dest = tmp_path / "repo"
    with pytest.raises(ValueError, match="Unsafe repository URL"):
        clone_repo("http://example.com/repo.git", dest)


def test_clone_repo_rejects_option_injection(tmp_path):
    """clone_repo must raise ValueError for strings that look like git options."""
    dest = tmp_path / "repo"
    with pytest.raises(ValueError, match="Unsafe repository URL"):
        clone_repo("--upload-pack=evil", dest)


def test_clone_repo_rejects_ssh_url_without_at(tmp_path):
    """clone_repo must raise ValueError for ssh:// scheme (only git@ is allowed)."""
    dest = tmp_path / "repo"
    with pytest.raises(ValueError, match="Unsafe repository URL"):
        clone_repo("ssh://git@github.com/example/repo.git", dest)


def test_clone_repo_does_not_call_subprocess_for_bad_url(tmp_path):
    """Subprocess must never be invoked when the URL scheme is rejected."""
    dest = tmp_path / "repo"
    with patch("subprocess.run") as mock_run:
        with pytest.raises(ValueError):
            clone_repo("file:///etc/passwd", dest)
    mock_run.assert_not_called()
