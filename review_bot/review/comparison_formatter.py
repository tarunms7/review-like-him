"""Formatters for multi-persona comparison output (CLI text and API dict)."""

from __future__ import annotations

from review_bot.review.comparator import ComparisonEntry, ComparisonResult


def format_comparison_cli(result: ComparisonResult) -> str:
    """Format a ComparisonResult as human-readable CLI text.

    Shows a header with the PR URL, then a section per persona with
    verdict, summary findings, inline comment count, and timing.

    Args:
        result: The comparison result to format.

    Returns:
        Formatted multi-line string for terminal output.
    """
    lines: list[str] = []
    lines.append("═" * 60)
    lines.append(f"  Persona Comparison: {result.pr_url}")
    lines.append("═" * 60)
    lines.append("")

    for entry in result.entries:
        lines.append(_format_entry_cli(entry))

    lines.append("─" * 60)
    lines.append(f"  Total time: {result.total_duration_ms}ms")
    lines.append("─" * 60)

    return "\n".join(lines)


def _format_entry_cli(entry: ComparisonEntry) -> str:
    """Format a single ComparisonEntry for CLI output."""
    lines: list[str] = []
    lines.append(f"── {entry.persona_name} ──")

    if entry.error:
        lines.append(f"  ERROR: {entry.error}")
        lines.append(f"  Duration: {entry.duration_ms}ms")
        lines.append("")
        return "\n".join(lines)

    verdict_icons = {
        "approve": "✅",
        "request_changes": "❌",
        "comment": "💬",
    }
    icon = verdict_icons.get(entry.result.verdict, "❓")
    lines.append(f"  Verdict: {icon} {entry.result.verdict}")

    if entry.result.summary_sections:
        lines.append("  Findings:")
        for section in entry.result.summary_sections:
            lines.append(f"    {section.emoji} {section.title}")
            for finding in section.findings:
                lines.append(f"      • {finding.text}")

    comment_count = len(entry.result.inline_comments)
    lines.append(f"  Inline comments: {comment_count}")
    lines.append(f"  Duration: {entry.duration_ms}ms")
    lines.append("")

    return "\n".join(lines)


def format_comparison_api(result: ComparisonResult) -> dict:
    """Format a ComparisonResult as a JSON-serializable dict.

    Args:
        result: The comparison result to format.

    Returns:
        Dict suitable for JSON serialization / API response.
    """
    return {
        "pr_url": result.pr_url,
        "total_duration_ms": result.total_duration_ms,
        "entries": [_format_entry_api(e) for e in result.entries],
    }


def _format_entry_api(entry: ComparisonEntry) -> dict:
    """Format a single ComparisonEntry as a dict."""
    return {
        "persona_name": entry.persona_name,
        "duration_ms": entry.duration_ms,
        "error": entry.error,
        "result": {
            "verdict": entry.result.verdict,
            "persona_name": entry.result.persona_name,
            "pr_url": entry.result.pr_url,
            "summary_sections": [
                {
                    "emoji": s.emoji,
                    "title": s.title,
                    "findings": [
                        {
                            "text": f.text,
                            "confidence": f.confidence,
                            "confidence_reason": f.confidence_reason,
                        }
                        for f in s.findings
                    ],
                }
                for s in entry.result.summary_sections
            ],
            "inline_comments": [
                {
                    "file": c.file,
                    "line": c.line,
                    "body": c.body,
                    "confidence": c.confidence,
                    "confidence_reason": c.confidence_reason,
                }
                for c in entry.result.inline_comments
            ],
        },
    }
