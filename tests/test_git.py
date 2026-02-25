import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ngn_agent.git import clone_repo


def _make_result(returncode=0, stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stderr = stderr
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


def test_clone_repo_raises_if_dest_exists(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        clone_repo("https://github.com/example/repo.git", dest)
