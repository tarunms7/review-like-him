"""Builds the system prompt combining persona, repo context, and PR diff."""

from __future__ import annotations

import logging

from review_bot.github.api import PullRequestFile
from review_bot.persona.profile import PersonaProfile
from review_bot.review.file_strategy import get_file_strategies, get_strategy
from review_bot.review.repo_scanner import RepoContext

logger = logging.getLogger("review-bot")

# Maximum diff characters to include in a single prompt
MAX_DIFF_CHARS = 80_000

# Priority mapping from persona categories to file extensions
_CATEGORY_FILE_HINTS: dict[str, list[str]] = {
    "error_handling": [".py", ".ts", ".js", ".go", ".rs", ".java"],
    "test_coverage": ["test_", "_test.", ".spec.", ".test."],
    "naming": [".py", ".ts", ".js", ".go", ".rs", ".java"],
    "architecture": [".py", ".ts", ".js", ".go", ".rs", ".java"],
    "security": [".py", ".ts", ".js", ".env", ".yml", ".yaml"],
    "performance": [".py", ".ts", ".js", ".go", ".rs", ".java"],
    "documentation": [".md", ".rst", ".txt"],
    "typing": [".py", ".ts"],
}

CROSS_CHUNK_CONTEXT_TEMPLATE = """\
## Multi-Pass Review Context

You are reviewing **chunk {chunk_index} of {total_chunks}**: {chunk_label}

**Other chunks in this PR:**
{other_chunks_text}

Focus your review on the files in THIS chunk only. Other chunks are being \
reviewed separately and will be merged. Avoid duplicating comments about \
issues visible in other chunks.

"""

SYSTEM_PROMPT_TEMPLATE = """\
You are {persona_name}-bot 🤖, an automated code reviewer that reviews \
exactly like {persona_name}.

## Your Persona

**Tone:** {tone}

**Review Priorities (in order of importance):**
{priorities_text}

**Pet Peeves (things you particularly dislike):**
{pet_peeves_text}

**Severity Pattern:**
- You BLOCK (request changes) on: {blocks_on}
- You NIT on: {nits_on}
- You APPROVE when: {approves_when}

{overrides_text}\
## Repository Context

{repo_context_text}\
{file_strategy_text}\
## Instructions

Review the pull request diff below. Return your review as a JSON object \
with this exact structure:

```json
{{
  "verdict": "approve" | "request_changes" | "comment",
  "summary_sections": [
    {{
      "emoji": "🐛",
      "title": "Bugs",
      "findings": [
        {{
          "text": "Description of the finding",
          "confidence": "high",
          "confidence_reason": "Why you are confident about this finding"
        }}
      ]
    }}
  ],
  "inline_comments": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "body": "Your comment here",
      "confidence": "high",
      "confidence_reason": "Why you are confident about this comment"
    }}
  ]
}}
```

**Rules:**
- Only include categories with actual findings. Valid categories: \
Bugs (🐛), Architecture (🏗️), Testing (🧪), Style (💅), \
Security (🔒), Performance (⚡).
- Write all findings and comments in YOUR persona's voice and tone.
- For inline_comments, use the line number from the diff (+ lines).
- Set verdict based on your severity pattern: if any blocking issues \
exist → "request_changes", if only nits → "comment", \
if everything looks good → "approve".
- Return ONLY the JSON object, no markdown fences or extra text.
- For each finding and inline comment, provide a confidence rating:
  - **high**: You are certain this is a real issue (e.g. clear bug, \
security vulnerability, obvious logic error).
  - **medium**: You believe this is likely an issue but cannot be 100% \
sure without more context (default).
  - **low**: This is a suggestion or stylistic preference, not a \
definite problem.
- Include a brief confidence_reason explaining your rating.

## Pull Request

**Title:** {pr_title}
**Author:** {pr_author}
**Description:** {pr_description}
**Changed Files:** {changed_files_count}

## Diff

{diff_text}
"""


class PromptBuilder:
    """Combines persona profile, repo context, and PR data into a prompt."""

    def build(
        self,
        persona: PersonaProfile,
        repo_context: RepoContext,
        pr_data: dict,
        diff: str,
        files: list[PullRequestFile],
    ) -> str:
        """Build the full system prompt for the LLM reviewer.

        Args:
            persona: The persona profile to review as.
            repo_context: Detected repo conventions.
            pr_data: Raw PR data from GitHub API.
            diff: Unified diff text.
            files: List of changed files.

        Returns:
            The complete prompt string.
        """
        # Handle large diffs by prioritizing files
        if len(diff) > MAX_DIFF_CHARS:
            diff = self._prioritize_diff(diff, files, persona)

        priorities_text = self._format_priorities(persona)
        pet_peeves_text = self._format_pet_peeves(persona)
        repo_context_text = self._format_repo_context(repo_context)
        overrides_text = self._format_overrides(persona)
        file_strategy_text = self._format_file_strategies(files)

        sp = persona.severity_pattern
        blocks_on = ", ".join(sp.blocks_on) if sp.blocks_on else "nothing specific"
        nits_on = ", ".join(sp.nits_on) if sp.nits_on else "nothing specific"
        approves_when = sp.approves_when or "code is generally acceptable"

        return SYSTEM_PROMPT_TEMPLATE.format(
            persona_name=persona.name,
            tone=persona.tone or "professional and constructive",
            priorities_text=priorities_text,
            pet_peeves_text=pet_peeves_text,
            blocks_on=blocks_on,
            nits_on=nits_on,
            approves_when=approves_when,
            overrides_text=overrides_text,
            repo_context_text=repo_context_text,
            file_strategy_text=file_strategy_text,
            pr_title=pr_data.get("title", ""),
            pr_author=pr_data.get("user", {}).get("login", "unknown"),
            pr_description=pr_data.get("body", "") or "(no description)",
            changed_files_count=pr_data.get("changed_files", len(files)),
            diff_text=diff,
        )

    def build_chunked(
        self,
        persona: PersonaProfile,
        repo_context: RepoContext,
        pr_data: dict,
        chunk: object,
        all_chunks: list,
    ) -> str:
        """Build a prompt for a single chunk in a multi-pass review.

        Injects cross-chunk context header before the diff section so the
        LLM knows it is reviewing part of a larger PR.

        Args:
            persona: The persona profile to review as.
            repo_context: Detected repo conventions.
            pr_data: Raw PR data from GitHub API.
            chunk: A DiffChunk object with diff_text, label, files, etc.
            all_chunks: All DiffChunk objects for cross-reference.

        Returns:
            The complete prompt string for this chunk.
        """
        from review_bot.review.chunker import DiffChunk

        chunk: DiffChunk = chunk  # type: ignore[no-redef]

        # Build the cross-chunk context header
        chunk_index = next(
            (i + 1 for i, c in enumerate(all_chunks) if c.chunk_id == chunk.chunk_id),
            1,
        )
        other_lines: list[str] = []
        for c in all_chunks:
            if c.chunk_id != chunk.chunk_id:
                other_lines.append(f"- {c.label}")
        other_chunks_text = "\n".join(other_lines) if other_lines else "- (none)"

        cross_chunk_header = CROSS_CHUNK_CONTEXT_TEMPLATE.format(
            chunk_index=chunk_index,
            total_chunks=len(all_chunks),
            chunk_label=chunk.label,
            other_chunks_text=other_chunks_text,
        )

        # Build the base prompt with chunk's diff and files
        diff_text = chunk.diff_text
        if len(diff_text) > MAX_DIFF_CHARS:
            diff_text = self._prioritize_diff(diff_text, chunk.files, persona)

        priorities_text = self._format_priorities(persona)
        pet_peeves_text = self._format_pet_peeves(persona)
        repo_context_text = self._format_repo_context(repo_context)
        overrides_text = self._format_overrides(persona)
        file_strategy_text = self._format_file_strategies(chunk.files)

        sp = persona.severity_pattern
        blocks_on = ", ".join(sp.blocks_on) if sp.blocks_on else "nothing specific"
        nits_on = ", ".join(sp.nits_on) if sp.nits_on else "nothing specific"
        approves_when = sp.approves_when or "code is generally acceptable"

        # Prepend cross-chunk context to the diff section
        combined_diff = cross_chunk_header + diff_text

        return SYSTEM_PROMPT_TEMPLATE.format(
            persona_name=persona.name,
            tone=persona.tone or "professional and constructive",
            priorities_text=priorities_text,
            pet_peeves_text=pet_peeves_text,
            blocks_on=blocks_on,
            nits_on=nits_on,
            approves_when=approves_when,
            overrides_text=overrides_text,
            repo_context_text=repo_context_text,
            file_strategy_text=file_strategy_text,
            pr_title=pr_data.get("title", ""),
            pr_author=pr_data.get("user", {}).get("login", "unknown"),
            pr_description=pr_data.get("body", "") or "(no description)",
            changed_files_count=pr_data.get("changed_files", len(chunk.files)),
            diff_text=combined_diff,
        )

    def _format_priorities(self, persona: PersonaProfile) -> str:
        """Format persona priorities as a numbered list."""
        if not persona.priorities:
            return "- No specific priorities defined\n"
        lines = []
        for i, p in enumerate(persona.priorities, 1):
            lines.append(f"{i}. [{p.severity.upper()}] {p.category}: {p.description}")
        return "\n".join(lines) + "\n"

    def _format_pet_peeves(self, persona: PersonaProfile) -> str:
        """Format pet peeves as a bullet list."""
        if not persona.pet_peeves:
            return "- None specified\n"
        return "\n".join(f"- {pp}" for pp in persona.pet_peeves) + "\n"

    def _format_repo_context(self, ctx: RepoContext) -> str:
        """Format repo context as readable text."""
        lines = []
        if ctx.languages:
            lines.append(f"- Languages: {', '.join(ctx.languages)}")
        if ctx.frameworks:
            lines.append(f"- Frameworks: {', '.join(ctx.frameworks)}")
        if ctx.has_tests:
            fws = f" ({', '.join(ctx.test_frameworks)})" if ctx.test_frameworks else ""
            lines.append(f"- Tests: Yes{fws}")
        else:
            lines.append("- Tests: No test infrastructure detected")
        if ctx.has_ci:
            lines.append(f"- CI: {', '.join(ctx.ci_systems)}")
        if ctx.has_linting:
            lines.append(f"- Linting: {', '.join(ctx.linters)}")

        # Extended repo context fields
        if ctx.project_type and ctx.project_type != "unknown":
            lines.append(f"- Project type: {ctx.project_type}")

        if ctx.modules:
            mod_lines = [
                f"  - {m.path} ({m.purpose})"
                for m in ctx.modules[:10]
            ]
            lines.append("- Modules:")
            lines.extend(mod_lines)
            if len(ctx.modules) > 10:
                lines.append(f"  - ... and {len(ctx.modules) - 10} more")

        if ctx.api_contracts:
            contract_lines = [
                f"  - {c.description}"
                for c in ctx.api_contracts[:8]
            ]
            lines.append("- API contracts:")
            lines.extend(contract_lines)
            if len(ctx.api_contracts) > 8:
                lines.append(f"  - ... and {len(ctx.api_contracts) - 8} more")

        if ctx.ownership:
            owner_lines = [
                f"  - {o.pattern}: {', '.join(o.owners)}"
                for o in ctx.ownership[:5]
            ]
            lines.append("- Code ownership:")
            lines.extend(owner_lines)
            if len(ctx.ownership) > 5:
                lines.append(f"  - ... and {len(ctx.ownership) - 5} more")

        if ctx.architecture_notes:
            notes = ctx.architecture_notes[:5]
            lines.append("- Architecture notes:")
            for note in notes:
                # Truncate long notes
                truncated = note[:200] + "..." if len(note) > 200 else note
                lines.append(f"  - {truncated}")

        if ctx.import_graph_summary:
            lines.append(f"- Import graph: {ctx.import_graph_summary[:300]}")

        return "\n".join(lines) + "\n\n" if lines else ""

    def _format_file_strategies(self, files: list[PullRequestFile]) -> str:
        """Format file-type-aware review strategies for changed files.

        Groups files by type and generates strategy instructions for the LLM.

        Args:
            files: List of changed PR files.

        Returns:
            Formatted strategy text, or empty string if no strategies apply.
        """
        if not files:
            return ""

        groups = get_file_strategies(files)
        if not groups:
            return ""

        lines = ["## File-Type Review Strategies\n"]
        for file_type, grouped_files in sorted(groups.items()):
            strategy = get_strategy(file_type)
            if strategy is None:
                continue
            filenames = [f.filename for f in grouped_files]
            file_list = ", ".join(filenames[:5])
            if len(filenames) > 5:
                file_list += f", ... (+{len(filenames) - 5} more)"
            lines.append(
                f"**{strategy.display_name}** ({len(filenames)} files: {file_list}):"
            )
            lines.append(strategy.prompt_instructions)
            lines.append("")

        return "\n".join(lines) + "\n" if len(lines) > 1 else ""

    def _format_overrides(self, persona: PersonaProfile) -> str:
        """Format manual override notes."""
        if not persona.overrides:
            return ""
        lines = ["**Manual Overrides:**"]
        for o in persona.overrides:
            lines.append(f"- {o}")
        return "\n".join(lines) + "\n\n"

    def _prioritize_diff(
        self,
        diff: str,
        files: list[PullRequestFile],
        persona: PersonaProfile,
    ) -> str:
        """Truncate diff to fit within limits, prioritizing important files.

        Prioritizes files based on persona priorities and excludes
        generated/lock files.
        """
        # Split diff into per-file sections
        file_diffs = self._split_diff(diff)

        # Score each file for priority
        scored: list[tuple[float, str, str]] = []
        skip_patterns = (
            "package-lock.json",
            "yarn.lock",
            "poetry.lock",
            "Pipfile.lock",
            ".min.js",
            ".min.css",
            ".generated.",
        )

        for filename, file_diff in file_diffs.items():
            # Skip generated/lock files
            if any(p in filename for p in skip_patterns):
                continue
            score = self._score_file(filename, persona)
            scored.append((score, filename, file_diff))

        # Sort by score descending, take files up to char limit
        scored.sort(key=lambda x: x[0], reverse=True)

        result_parts: list[str] = []
        char_count = 0
        included = 0
        total = len(scored)

        for _score, filename, file_diff in scored:
            if char_count + len(file_diff) > MAX_DIFF_CHARS:
                break
            result_parts.append(file_diff)
            char_count += len(file_diff)
            included += 1

        if included < total:
            omitted = total - included
            result_parts.append(
                f"\n... ({omitted} lower-priority files omitted due to size constraints) ...\n"
            )

        return "\n".join(result_parts)

    def _split_diff(self, diff: str) -> dict[str, str]:
        """Split a unified diff into per-file sections."""
        file_diffs: dict[str, str] = {}
        current_file: str | None = None
        current_lines: list[str] = []

        for line in diff.split("\n"):
            if line.startswith("diff --git"):
                if current_file and current_lines:
                    file_diffs[current_file] = "\n".join(current_lines)
                # Extract filename from "diff --git a/path b/path"
                parts = line.split(" b/", 1)
                current_file = parts[1] if len(parts) > 1 else line
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_file and current_lines:
            file_diffs[current_file] = "\n".join(current_lines)

        return file_diffs

    def _score_file(
        self,
        filename: str,
        persona: PersonaProfile,
    ) -> float:
        """Score a file based on persona priorities."""
        score = 1.0

        # Boost source code files
        if any(filename.endswith(ext) for ext in (".py", ".ts", ".js", ".go", ".rs", ".java")):
            score += 1.0

        # Boost test files if persona cares about testing
        is_test = any(marker in filename for marker in ("test_", "_test.", ".spec.", ".test."))
        for priority in persona.priorities:
            if priority.category == "test_coverage" and is_test:
                score += 2.0
            hints = _CATEGORY_FILE_HINTS.get(priority.category, [])
            if any(h in filename for h in hints):
                weight = {
                    "critical": 3.0,
                    "strict": 2.0,
                    "moderate": 1.0,
                    "opinionated": 0.5,
                }.get(priority.severity, 1.0)
                score += weight

        return score
