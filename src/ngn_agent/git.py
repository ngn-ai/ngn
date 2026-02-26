import shutil
import subprocess
from pathlib import Path


def clone_repo(repo_url: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", repo_url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{result.stderr.strip()}")


def find_resume_branch(repo_url: str, branch: str) -> bool:
    """Check whether a branch exists on the remote repository.

    Runs ``git ls-remote --heads <repo_url> refs/heads/<branch>`` and returns
    True if the remote lists any output for that ref (i.e. the branch exists),
    False if the output is empty or if any subprocess error occurs.

    Args:
        repo_url: URL of the remote git repository to query.
        branch: Branch name to look up (e.g. ``"ngn/NGN-17"``).

    Returns:
        True if the branch exists on the remote, False otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", repo_url, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        # A non-empty stdout means the ref was found on the remote.
        return bool(result.stdout.strip())
    except Exception:
        # Treat any subprocess error (e.g. network failure, git not found) as
        # "branch not found" so callers can safely fall back to a fresh start.
        return False
