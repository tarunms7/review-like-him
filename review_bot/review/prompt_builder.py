"""Builds the system prompt combining persona, repo context, and PR diff."""

from __future__ import annotations

import logging

from review_bot.github.api import PullRequestFile
from review_bot.persona.profile import PersonaProfile
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
      "findings": ["Finding 1", "Finding 2"]
    }}
  ],
  "inline_comments": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "body": "Your comment here"
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
            pr_title=pr_data.get("title", ""),
            pr_author=pr_data.get("user", {}).get("login", "unknown"),
            pr_description=pr_data.get("body", "") or "(no description)",
            changed_files_count=pr_data.get("changed_files", len(files)),
            diff_text=diff,
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
        return "\n".join(lines) + "\n\n" if lines else ""

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
