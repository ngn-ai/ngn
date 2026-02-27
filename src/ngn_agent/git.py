import shutil
import subprocess
from pathlib import Path

# URL schemes that are safe to pass to git clone.  Only HTTPS and SSH (git@)
# URLs are accepted; bare paths, file:// URLs, and option-injection strings
# (e.g. --upload-pack=...) are all rejected.
_ALLOWED_URL_PREFIXES = ("https://", "git@")


def clone_repo(repo_url: str, dest: Path) -> None:
    """Clone a Git repository into *dest*, removing any pre-existing directory first.

    Only ``https://`` and ``git@`` URLs are accepted.  Any other scheme (e.g.
    ``file://``, a bare filesystem path, or an option-injection string) raises
    ``ValueError`` before the subprocess is started, so the caller can block
    the ticket without running arbitrary git sub-commands.

    Args:
        repo_url: Remote URL of the repository to clone.  Must start with
            ``https://`` or ``git@``.
        dest: Local path where the repository should be cloned.

    Raises:
        ValueError: If *repo_url* does not start with an allowed scheme prefix.
        RuntimeError: If git clone exits with a non-zero return code.
    """
    # Validate the URL scheme before touching the filesystem or spawning a
    # subprocess so that dangerous URLs (file://, bare paths, flag injection)
    # are rejected early.
    if not any(repo_url.startswith(prefix) for prefix in _ALLOWED_URL_PREFIXES):
        raise ValueError(
            f"Unsafe repository URL '{repo_url}': only https:// and git@ URLs are allowed"
        )

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
