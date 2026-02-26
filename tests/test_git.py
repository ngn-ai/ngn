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
