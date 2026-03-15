"""Tests for the DiffChunker multi-pass chunking system."""

from __future__ import annotations

import pytest

from review_bot.github.api import PullRequestFile
from review_bot.review.chunker import (
    DEFAULT_CHUNK_MAX_CHARS,
    DEFAULT_CHUNK_MAX_FILES,
    INDIVIDUAL_FILE_MAX_CHARS,
    ChunkingResult,
    DiffChunk,
    DiffChunker,
)


def _make_file(filename: str, patch: str = "+code") -> PullRequestFile:
    """Create a PullRequestFile for testing."""
    return PullRequestFile(
        filename=filename,
        status="modified",
        additions=1,
        deletions=0,
        patch=patch,
    )


def _make_diff(*filenames: str, content_size: int = 100) -> str:
    """Build a fake unified diff for the given filenames."""
    parts = []
    for fn in filenames:
        body = "+" + ("x" * content_size)
        parts.append(
            f"diff --git a/{fn} b/{fn}\n"
            f"--- a/{fn}\n"
            f"+++ b/{fn}\n"
            f"@@ -1,1 +1,1 @@\n"
            f"{body}"
        )
    return "\n".join(parts)


class TestChunkSmallPR:
    """Small PRs should produce a single chunk."""

    def test_chunk_small_pr_returns_single_chunk(self) -> None:
        files = [
            _make_file("review_bot/review/chunker.py"),
            _make_file("review_bot/review/merger.py"),
        ]
        diff = _make_diff(
            "review_bot/review/chunker.py",
            "review_bot/review/merger.py",
        )

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        assert isinstance(result, ChunkingResult)
        assert len(result.chunks) == 1
        assert result.total_files == 2
        assert len(result.skipped_files) == 0
        chunk = result.chunks[0]
        assert chunk.chunk_id == "chunk-1"
        assert "review_bot" in chunk.label
        assert len(chunk.files) == 2
        assert chunk.directory_group == "review_bot"


class TestChunkGroupsByDirectory:
    """Files should be grouped by top-level directory."""

    def test_chunk_groups_by_directory(self) -> None:
        files = [
            _make_file("src/app.py"),
            _make_file("src/utils.py"),
            _make_file("tests/test_app.py"),
            _make_file("tests/test_utils.py"),
        ]
        diff = _make_diff(
            "src/app.py", "src/utils.py",
            "tests/test_app.py", "tests/test_utils.py",
        )

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        assert len(result.chunks) == 2
        dirs = {c.directory_group for c in result.chunks}
        assert dirs == {"src", "tests"}

        # Check labels contain file counts
        for chunk in result.chunks:
            assert "2 files" in chunk.label

    def test_root_level_files_grouped_under_dot(self) -> None:
        files = [_make_file("setup.py"), _make_file("README.md")]
        diff = _make_diff("setup.py", "README.md")

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        assert len(result.chunks) == 1
        assert result.chunks[0].directory_group == "."


class TestChunkSplitsOversizedDirectory:
    """Oversized directory groups should be split into multiple chunks."""

    def test_chunk_splits_oversized_directory(self) -> None:
        # Create files whose diffs exceed max chars
        files = []
        filenames = []
        for i in range(5):
            fn = f"src/file_{i}.py"
            files.append(_make_file(fn))
            filenames.append(fn)

        # Each file gets a large diff, total exceeds limit
        per_file_size = DEFAULT_CHUNK_MAX_CHARS // 3
        diff = _make_diff(*filenames, content_size=per_file_size)

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        # Should produce multiple chunks for the same directory
        assert len(result.chunks) > 1
        for chunk in result.chunks:
            assert chunk.directory_group == "src"
            assert "part" in chunk.label

    def test_chunk_splits_by_file_count(self) -> None:
        # Create more files than max_files_per_chunk
        max_files = 3
        files = []
        filenames = []
        for i in range(7):
            fn = f"src/file_{i}.py"
            files.append(_make_file(fn))
            filenames.append(fn)

        diff = _make_diff(*filenames, content_size=10)

        chunker = DiffChunker(max_files_per_chunk=max_files)
        result = chunker.chunk(diff, files)

        assert len(result.chunks) >= 3
        for chunk in result.chunks:
            assert len(chunk.files) <= max_files


class TestChunkExcludesGeneratedFiles:
    """Generated and vendored files should be skipped."""

    def test_chunk_excludes_generated_files(self) -> None:
        files = [
            _make_file("src/app.py"),
            _make_file("package-lock.json"),
            _make_file("dist/bundle.min.js"),
            _make_file("vendor/lib.js"),
        ]
        diff = _make_diff(
            "src/app.py",
            "package-lock.json",
            "dist/bundle.min.js",
            "vendor/lib.js",
        )

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        assert result.total_files == 4
        assert len(result.skipped_files) == 3
        assert "package-lock.json" in result.skipped_files
        assert len(result.chunks) == 1
        assert result.chunks[0].files[0].filename == "src/app.py"

    def test_all_generated_returns_empty_chunks(self) -> None:
        files = [
            _make_file("package-lock.json"),
            _make_file("yarn.lock"),
        ]
        diff = _make_diff("package-lock.json", "yarn.lock")

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        assert len(result.chunks) == 0
        assert len(result.skipped_files) == 2


class TestChunkTruncatesLargeFile:
    """Large individual file diffs should be truncated."""

    def test_chunk_truncates_large_individual_file(self) -> None:
        files = [_make_file("src/big_file.py")]
        # Create a diff larger than INDIVIDUAL_FILE_MAX_CHARS
        big_content = "x" * (INDIVIDUAL_FILE_MAX_CHARS + 5000)
        diff = (
            f"diff --git a/src/big_file.py b/src/big_file.py\n"
            f"--- a/src/big_file.py\n"
            f"+++ b/src/big_file.py\n"
            f"@@ -1,1 +1,1 @@\n"
            f"+{big_content}"
        )

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        assert len(result.chunks) == 1
        chunk_diff = result.chunks[0].diff_text
        assert "[truncated" in chunk_diff
        assert len(chunk_diff) < len(diff)


class TestChunkHandlesEmptyDiff:
    """Empty diffs should return an empty result."""

    def test_chunk_handles_empty_diff(self) -> None:
        chunker = DiffChunker()
        result = chunker.chunk("", [])

        assert isinstance(result, ChunkingResult)
        assert len(result.chunks) == 0
        assert len(result.skipped_files) == 0
        assert result.total_files == 0

    def test_chunk_handles_empty_diff_with_files(self) -> None:
        files = [_make_file("src/app.py")]
        chunker = DiffChunker()
        result = chunker.chunk("", files)

        assert len(result.chunks) == 0
        assert result.total_files == 1


class TestDiffSplitting:
    """Test internal diff splitting logic."""

    def test_split_diff_by_file(self) -> None:
        diff = _make_diff("src/a.py", "src/b.py")
        chunker = DiffChunker()
        result = chunker._split_diff_by_file(diff)

        assert "src/a.py" in result
        assert "src/b.py" in result
        assert result["src/a.py"].startswith("diff --git")
        assert result["src/b.py"].startswith("diff --git")


class TestFileTypeClassification:
    """Test that chunk file_type_group is set correctly."""

    def test_classify_file_type_uses_file_strategy(self) -> None:
        assert DiffChunker._classify_file_type("src/app.py") == "business_logic"
        assert DiffChunker._classify_file_type("tests/test_app.py") == "test"
        assert DiffChunker._classify_file_type("README.md") == "documentation"

    def test_is_generated_or_vendored(self) -> None:
        assert DiffChunker._is_generated_or_vendored("package-lock.json") is True
        assert DiffChunker._is_generated_or_vendored("yarn.lock") is True
        assert DiffChunker._is_generated_or_vendored("dist/bundle.min.js") is True
        assert DiffChunker._is_generated_or_vendored("src/app.py") is False


class TestChunkIdAssignment:
    """Chunk IDs should be sequential across all chunks."""

    def test_chunk_ids_are_sequential(self) -> None:
        files = [
            _make_file("src/a.py"),
            _make_file("tests/test_a.py"),
            _make_file("docs/guide.md"),
        ]
        diff = _make_diff("src/a.py", "tests/test_a.py", "docs/guide.md")

        chunker = DiffChunker()
        result = chunker.chunk(diff, files)

        ids = [c.chunk_id for c in result.chunks]
        for i, cid in enumerate(ids, 1):
            assert cid == f"chunk-{i}"
