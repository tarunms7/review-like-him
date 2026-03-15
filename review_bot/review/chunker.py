"""Multi-pass diff chunker for large PR reviews."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from review_bot.github.api import PullRequestFile
from review_bot.review.file_strategy import FileType, classify_file

logger = logging.getLogger("review-bot")

# Chunk size limits
DEFAULT_CHUNK_MAX_CHARS = 70_000
DEFAULT_CHUNK_MAX_FILES = 50
INDIVIDUAL_FILE_MAX_CHARS = 30_000

# Patterns for generated/vendored files to skip
_GENERATED_PATTERNS: tuple[str, ...] = (
    ".min.js",
    ".min.css",
    ".generated.",
    "/generated/",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
    "/dist/",
    "/build/",
    "vendor/",
    "node_modules/",
    ".lock",
    ".pb.go",
    "_pb2.py",
    ".snap",
)


@dataclass
class DiffChunk:
    """A chunk of diff for multi-pass review.

    Args:
        chunk_id: Unique identifier for the chunk (e.g. 'chunk-1').
        label: Human-readable label (e.g. 'review_bot/review (5 files)').
        files: PullRequestFile objects included in this chunk.
        diff_text: The unified diff text for files in this chunk.
        directory_group: Top-level directory that groups these files.
        file_type_group: Dominant FileType classification for the chunk.
    """

    chunk_id: str
    label: str
    files: list[PullRequestFile]
    diff_text: str
    directory_group: str
    file_type_group: str


@dataclass
class ChunkingResult:
    """Result of chunking a diff.

    Args:
        chunks: List of DiffChunk objects.
        skipped_files: Filenames that were skipped (generated/vendored).
        total_files: Total number of files before filtering.
    """

    chunks: list[DiffChunk] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    total_files: int = 0


class DiffChunker:
    """Chunks a large PR diff into reviewable pieces.

    Groups files by top-level directory, splits oversized groups,
    filters generated files, and assigns human-readable labels.

    Args:
        max_chars_per_chunk: Maximum characters of diff text per chunk.
        max_files_per_chunk: Maximum number of files per chunk.
    """

    def __init__(
        self,
        max_chars_per_chunk: int = DEFAULT_CHUNK_MAX_CHARS,
        max_files_per_chunk: int = DEFAULT_CHUNK_MAX_FILES,
    ) -> None:
        self._max_chars = max_chars_per_chunk
        self._max_files = max_files_per_chunk

    def chunk(self, diff: str, files: list[PullRequestFile]) -> ChunkingResult:
        """Chunk a PR diff into reviewable pieces.

        Algorithm:
        1. Parse diff into per-file sections.
        2. Filter out generated/vendored files.
        3. Group remaining files by top-level directory.
        4. Split oversized groups into smaller chunks.
        5. Assign labels and metadata to each chunk.

        Args:
            diff: The full unified diff text for the PR.
            files: List of PullRequestFile objects from the PR.

        Returns:
            A ChunkingResult with chunks, skipped files, and total count.
        """
        total_files = len(files)

        if not diff or not files:
            return ChunkingResult(chunks=[], skipped_files=[], total_files=total_files)

        # Step 1: Parse diff into per-file sections
        file_diffs = self._split_diff_by_file(diff)

        # Step 2: Filter generated/vendored files
        skipped: list[str] = []
        reviewable: list[PullRequestFile] = []
        for f in files:
            if self._is_generated_or_vendored(f.filename):
                skipped.append(f.filename)
            else:
                reviewable.append(f)

        if not reviewable:
            return ChunkingResult(
                chunks=[],
                skipped_files=skipped,
                total_files=total_files,
            )

        # Step 3: Group by top-level directory
        dir_groups = self._group_files_by_directory(reviewable)

        # Step 4: Build chunks, splitting oversized groups
        all_chunks: list[DiffChunk] = []
        chunk_counter = 0

        for directory, group_files in sorted(dir_groups.items()):
            # Collect diff text for this group
            group_diff_parts: list[str] = []
            group_diff_len = 0
            for f in group_files:
                fd = file_diffs.get(f.filename, "")
                fd = self._truncate_large_file_diff(fd)
                group_diff_parts.append(fd)
                group_diff_len += len(fd)

            # Check if group fits in a single chunk
            if (
                group_diff_len <= self._max_chars
                and len(group_files) <= self._max_files
            ):
                chunk_counter += 1
                file_type = self._dominant_file_type(group_files)
                n = len(group_files)
                label = f"{directory} ({n} file{'s' if n != 1 else ''})"
                all_chunks.append(
                    DiffChunk(
                        chunk_id=f"chunk-{chunk_counter}",
                        label=label,
                        files=group_files,
                        diff_text="\n".join(group_diff_parts),
                        directory_group=directory,
                        file_type_group=file_type,
                    )
                )
            else:
                # Split oversized group
                sub_chunks = self._split_oversized_group(
                    directory, group_files, file_diffs
                )
                for sc in sub_chunks:
                    chunk_counter += 1
                    sc.chunk_id = f"chunk-{chunk_counter}"
                    all_chunks.append(sc)

        return ChunkingResult(
            chunks=all_chunks,
            skipped_files=skipped,
            total_files=total_files,
        )

    def _split_diff_by_file(self, diff: str) -> dict[str, str]:
        """Split a unified diff into per-file sections.

        Args:
            diff: The full unified diff text.

        Returns:
            Dict mapping filename to its diff section text.
        """
        file_diffs: dict[str, str] = {}
        current_file: str | None = None
        current_lines: list[str] = []

        for line in diff.split("\n"):
            if line.startswith("diff --git"):
                if current_file is not None and current_lines:
                    file_diffs[current_file] = "\n".join(current_lines)
                # Extract filename from "diff --git a/path b/path"
                parts = line.split(" b/", 1)
                current_file = parts[1] if len(parts) > 1 else line
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_file is not None and current_lines:
            file_diffs[current_file] = "\n".join(current_lines)

        return file_diffs

    def _group_files_by_directory(
        self, files: list[PullRequestFile]
    ) -> dict[str, list[PullRequestFile]]:
        """Group files by their top-level directory.

        Files at the root level are grouped under '.'.

        Args:
            files: List of PullRequestFile objects.

        Returns:
            Dict mapping top-level directory to list of files.
        """
        groups: dict[str, list[PullRequestFile]] = {}
        for f in files:
            parts = f.filename.split("/", 1)
            directory = parts[0] if len(parts) > 1 else "."
            groups.setdefault(directory, []).append(f)
        return groups

    def _split_oversized_group(
        self,
        directory: str,
        files: list[PullRequestFile],
        file_diffs: dict[str, str],
    ) -> list[DiffChunk]:
        """Split an oversized directory group into smaller chunks.

        Greedily packs files into chunks respecting size and count limits.

        Args:
            directory: The directory name for labeling.
            files: Files in the oversized group.
            file_diffs: Mapping of filename to diff text.

        Returns:
            List of DiffChunk objects (chunk_id is placeholder, set by caller).
        """
        chunks: list[DiffChunk] = []
        current_files: list[PullRequestFile] = []
        current_parts: list[str] = []
        current_size = 0
        part_num = 0

        for f in files:
            fd = file_diffs.get(f.filename, "")
            fd = self._truncate_large_file_diff(fd)
            fd_len = len(fd)

            # If adding this file would exceed limits, finalize current chunk
            if current_files and (
                current_size + fd_len > self._max_chars
                or len(current_files) >= self._max_files
            ):
                part_num += 1
                n = len(current_files)
                file_type = self._dominant_file_type(current_files)
                label = (
                    f"{directory} part {part_num} "
                    f"({n} file{'s' if n != 1 else ''})"
                )
                chunks.append(
                    DiffChunk(
                        chunk_id="",  # Will be set by caller
                        label=label,
                        files=current_files,
                        diff_text="\n".join(current_parts),
                        directory_group=directory,
                        file_type_group=file_type,
                    )
                )
                current_files = []
                current_parts = []
                current_size = 0

            current_files.append(f)
            current_parts.append(fd)
            current_size += fd_len

        # Finalize last chunk
        if current_files:
            part_num += 1
            n = len(current_files)
            file_type = self._dominant_file_type(current_files)
            # Don't add "part N" if there's only one part
            if part_num == 1 and len(chunks) == 0:
                label = f"{directory} ({n} file{'s' if n != 1 else ''})"
            else:
                label = (
                    f"{directory} part {part_num} "
                    f"({n} file{'s' if n != 1 else ''})"
                )
            chunks.append(
                DiffChunk(
                    chunk_id="",
                    label=label,
                    files=current_files,
                    diff_text="\n".join(current_parts),
                    directory_group=directory,
                    file_type_group=file_type,
                )
            )

        return chunks

    def _truncate_large_file_diff(self, file_diff: str) -> str:
        """Truncate an individual file diff if it exceeds the limit.

        Keeps the first and last portions with a truncation marker.

        Args:
            file_diff: The diff text for a single file.

        Returns:
            The original or truncated diff text.
        """
        if len(file_diff) <= INDIVIDUAL_FILE_MAX_CHARS:
            return file_diff

        keep = INDIVIDUAL_FILE_MAX_CHARS // 2
        head = file_diff[:keep]
        tail = file_diff[-keep:]
        omitted = len(file_diff) - (keep * 2)
        marker = (
            f"\n\n... [truncated {omitted} characters] ...\n\n"
        )
        return head + marker + tail

    @staticmethod
    def _classify_file_type(filename: str) -> str:
        """Classify a file using the FileType strategy system.

        Args:
            filename: File path to classify.

        Returns:
            A FileType string constant.
        """
        return classify_file(filename)

    @staticmethod
    def _is_generated_or_vendored(filename: str) -> bool:
        """Check if a file is generated or vendored and should be skipped.

        Args:
            filename: File path to check.

        Returns:
            True if the file should be skipped.
        """
        name_lower = filename.lower()
        return any(pattern in name_lower for pattern in _GENERATED_PATTERNS)

    def _dominant_file_type(self, files: list[PullRequestFile]) -> str:
        """Determine the most common file type in a group of files.

        Args:
            files: List of PullRequestFile objects.

        Returns:
            The most common FileType string constant.
        """
        if not files:
            return FileType.UNKNOWN
        counts: Counter[str] = Counter()
        for f in files:
            counts[self._classify_file_type(f.filename)] += 1
        return counts.most_common(1)[0][0]
