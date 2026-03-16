"""Git operations using subprocess for cloning repos and generating diffs."""

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("review-bot")

# Only allow https:// and ssh:// (git@) URLs
_URL_PATTERN = re.compile(r'^(https://|git@)[\w.\-]+[:/][\w.\-/]+(\.git)?$')

# Git refs must be alphanumeric with dots, hyphens, underscores, slashes
_REF_PATTERN = re.compile(r'^[a-zA-Z0-9_.\-/]+$')

# Maximum stdout size: 10 MB
_MAX_OUTPUT_BYTES = 10_485_760


def _validate_url(url: str) -> None:
    """Validate that a git URL uses an allowed scheme.

    Only https:// and ssh:// (git@) URLs are accepted.
    Rejects file://, ftp://, and bare paths.

    Raises:
        ValueError: If the URL doesn't match the allowed pattern.
    """
    if not _URL_PATTERN.match(url):
        raise ValueError(
            f"Invalid git URL: {url!r}. "
            f"Only https:// and ssh:// (git@) URLs are allowed."
        )


def _validate_ref(ref: str, name: str = "ref") -> None:
    """Validate that a git ref contains only safe characters.

    Raises:
        ValueError: If the ref contains invalid characters.
    """
    if not _REF_PATTERN.match(ref):
        raise ValueError(
            f"Invalid git {name}: {ref!r}. "
            "Refs must contain only alphanumeric characters, "
            "dots, hyphens, underscores, and slashes."
        )


def clone_repo(url: str, dest: Path, *, depth: int = 1) -> Path:
    """Clone a git repository to the destination path.

    Args:
        url: Repository URL to clone (https:// or git@ only).
        dest: Destination directory for the clone.
        depth: Shallow clone depth. Use 0 for full clone.

    Returns:
        Path to the cloned repository.

    Raises:
        ValueError: If the URL uses a disallowed scheme.
        subprocess.CalledProcessError: If git clone fails.
    """
    _validate_url(url)

    cmd = ["git", "clone", "--no-checkout"]
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
        The diff output as a string, truncated to 10MB max.

    Raises:
        ValueError: If base or head contain invalid characters.
        subprocess.CalledProcessError: If git diff fails.
    """
    _validate_ref(base, "base ref")
    _validate_ref(head, "head ref")

    cmd = ["git", "diff", f"{base}...{head}"]

    logger.info("Getting diff %s...%s in %s", base, head, repo_path)
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_path,
    )

    output = result.stdout
    if len(output.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
        logger.warning(
            "Diff output truncated from %d bytes to %d bytes limit",
            len(output.encode("utf-8", errors="replace")),
            _MAX_OUTPUT_BYTES,
        )
        # Truncate at byte boundary by encoding, slicing, and decoding
        output = output.encode("utf-8", errors="replace")[:_MAX_OUTPUT_BYTES].decode(
            "utf-8", errors="replace"
        )

    return output
