"""Git operations using subprocess for cloning repos and generating diffs."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("review-bot")


def clone_repo(url: str, dest: Path, *, depth: int = 1) -> Path:
    """Clone a git repository to the destination path.

    Args:
        url: Repository URL to clone.
        dest: Destination directory for the clone.
        depth: Shallow clone depth. Use 0 for full clone.

    Returns:
        Path to the cloned repository.

    Raises:
        subprocess.CalledProcessError: If git clone fails.
    """
    cmd = ["git", "clone"]
    if depth > 0:
        cmd.extend(["--depth", str(depth)])
    cmd.extend([url, str(dest)])

    logger.info("Cloning %s to %s", url, dest)
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return dest


def get_diff(repo_path: Path, base: str, head: str) -> str:
    """Get the diff between two refs in a repository.

    Args:
        repo_path: Path to the git repository.
        base: Base ref (commit, branch, tag).
        head: Head ref (commit, branch, tag).

    Returns:
        The diff output as a string.

    Raises:
        subprocess.CalledProcessError: If git diff fails.
    """
    cmd = ["git", "diff", f"{base}...{head}"]

    logger.info("Getting diff %s...%s in %s", base, head, repo_path)
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    return result.stdout
