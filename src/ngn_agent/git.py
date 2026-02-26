import shutil
import subprocess
from pathlib import Path


def clone_repo(repo_url: str, dest: Path) -> None:
    """Clone a Git repository into *dest*, removing any pre-existing directory first.

    Args:
        repo_url: Remote URL of the repository to clone.
        dest: Local path where the repository should be cloned.

    Raises:
        RuntimeError: If git clone exits with a non-zero return code.
    """
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
    """Check whether *branch* already exists on the remote at *repo_url*.

    Runs ``git ls-remote --heads <repo_url> refs/heads/<branch>`` and returns
    ``True`` when the output is non-empty (i.e. the branch exists on the
    remote), ``False`` when the output is empty or a subprocess error occurs.

    Args:
        repo_url: Remote URL of the repository to query.
        branch: Branch name to look up (without the ``refs/heads/`` prefix).

    Returns:
        ``True`` if the branch exists on the remote, ``False`` otherwise.
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
        # Any subprocess error (e.g. network failure, git not found) is treated
        # as "branch not found" so the caller can safely fall back to a fresh start.
        return False
