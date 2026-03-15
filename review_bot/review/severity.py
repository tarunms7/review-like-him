"""Severity-based filtering for review findings."""

from __future__ import annotations

import logging

from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)

logger = logging.getLogger("review-bot")

# Base severity by review category (0–4 scale)
CATEGORY_SEVERITY: dict[str, int] = {
    "Security": 4,
    "Bugs": 3,
    "Architecture": 2,
    "Performance": 2,
    "Testing": 1,
    "Style": 1,
}

# Confidence level modifies severity
CONFIDENCE_SEVERITY_BOOST: dict[str, int] = {
    "high": 1,
    "medium": 0,
    "low": -1,
}

# Keywords that indicate critical security issues (bypass filtering)
SECURITY_OVERRIDE_KEYWORDS: list[str] = [
    "sql injection",
    "rce",
    "remote code execution",
    "path traversal",
    "directory traversal",
    "command injection",
    "xss",
    "cross-site scripting",
    "ssrf",
    "server-side request forgery",
    "authentication bypass",
    "privilege escalation",
    "insecure deserialization",
    "arbitrary file",
    "code injection",
]


def _get_file_type_severity_boost(file_type: str) -> int:
    """Get severity boost for a file type from file_strategy module.

    Args:
        file_type: FileType string constant (e.g. 'migration', 'test').

    Returns:
        Severity boost integer. Defaults to 0 if unavailable.
    """
    if not file_type or file_type == "unknown":
        return 0
    try:
        from review_bot.review.file_strategy import STRATEGIES

        strategy = STRATEGIES.get(file_type)
        if strategy:
            return strategy.severity_boost
    except (ImportError, AttributeError):
        logger.debug("file_strategy module not available, using default severity_boost=0")
    return 0


def compute_finding_severity(
    category: str,
    confidence: str = "medium",
    file_type: str = "unknown",
) -> int:
    """Compute the effective severity of a finding.

    Combines base category severity, confidence modifier, and file type boost,
    clamped to [0, 4].

    Args:
        category: Review category (e.g. 'Security', 'Bugs', 'Style').
        confidence: Confidence level: 'high', 'medium', or 'low'.
        file_type: FileType string constant for severity_boost lookup.

    Returns:
        Integer severity in [0, 4].
    """
    base = CATEGORY_SEVERITY.get(category, 1)
    conf_boost = CONFIDENCE_SEVERITY_BOOST.get(confidence, 0)
    file_boost = _get_file_type_severity_boost(file_type)
    return max(0, min(4, base + conf_boost + file_boost))


def _is_critical_security(body: str) -> bool:
    """Check if text contains critical security keywords that bypass filtering.

    Args:
        body: Text to check for critical security terms.

    Returns:
        True if any critical security keyword is found.
    """
    lower = body.lower()
    return any(keyword in lower for keyword in SECURITY_OVERRIDE_KEYWORDS)


def _infer_comment_category(body: str) -> str:
    """Infer a review category from inline comment text using keyword heuristics.

    Args:
        body: The inline comment body text.

    Returns:
        Inferred category string (e.g. 'Security', 'Bugs').
    """
    lower = body.lower()

    security_terms = [
        "security", "vulnerability", "injection", "xss", "csrf",
        "authentication", "authorization", "secret", "credential",
        "encrypt", "sanitiz",
    ]
    if any(term in lower for term in security_terms):
        return "Security"

    bug_terms = [
        "bug", "error", "exception", "crash", "null", "undefined",
        "race condition", "deadlock", "off-by-one", "overflow",
        "memory leak", "broken",
    ]
    if any(term in lower for term in bug_terms):
        return "Bugs"

    arch_terms = [
        "architecture", "design", "pattern", "coupling", "cohesion",
        "layer", "abstraction", "dependency", "refactor", "solid",
    ]
    if any(term in lower for term in arch_terms):
        return "Architecture"

    perf_terms = [
        "performance", "slow", "n+1", "cache", "optimize", "latency",
        "memory", "efficient", "complexity", "o(n",
    ]
    if any(term in lower for term in perf_terms):
        return "Performance"

    test_terms = [
        "test", "coverage", "assert", "mock", "fixture", "spec",
    ]
    if any(term in lower for term in test_terms):
        return "Testing"

    return "Style"


def _create_lgtm_result(original: ReviewResult, min_severity: int) -> ReviewResult:
    """Create an approve result when all findings are filtered out.

    Args:
        original: The original ReviewResult before filtering.
        min_severity: The minimum severity threshold that was applied.

    Returns:
        A new ReviewResult with approve verdict and LGTM message.
    """
    return ReviewResult(
        verdict="approve",
        summary_sections=[
            CategorySection(
                emoji="✅",
                title="LGTM",
                findings=[
                    Finding(
                        text=(
                            f"All findings were below severity threshold "
                            f"(min_severity={min_severity}). Looks good!"
                        ),
                        confidence="high",
                    ),
                ],
            ),
        ],
        inline_comments=[],
        persona_name=original.persona_name,
        pr_url=original.pr_url,
    )


def _recompute_verdict(
    original_verdict: str,
    sections: list[CategorySection],
    inline_comments: list[InlineComment],
) -> str:
    """Recompute verdict after filtering — never upgrades severity.

    If filtering removed findings, the verdict can only stay the same or
    downgrade (request_changes → comment → approve). It never upgrades.

    Args:
        original_verdict: The verdict before filtering.
        sections: Remaining summary sections after filtering.
        inline_comments: Remaining inline comments after filtering.

    Returns:
        The recomputed verdict string.
    """
    if original_verdict == "approve":
        return "approve"

    if not sections and not inline_comments:
        return "approve"

    # A section is blocking if its category severity is >= 3 (Bugs, Security)
    has_blocking = any(CATEGORY_SEVERITY.get(s.title, 2) >= 3 for s in sections)

    if has_blocking:
        # Never upgrade: 'comment' stays 'comment' even with blocking findings
        if original_verdict == "request_changes":
            return "request_changes"
        return "comment"

    # Non-blocking findings only warrant a comment at most
    if original_verdict in ("request_changes", "comment"):
        return "comment"

    return original_verdict


def filter_result_by_severity(
    result: ReviewResult,
    min_severity: int,
) -> ReviewResult:
    """Filter review findings below the minimum severity threshold.

    Critical security findings bypass the filter entirely.
    If all findings are filtered out, returns an LGTM approve result.

    Args:
        result: The ReviewResult to filter.
        min_severity: Minimum severity threshold (0=all, 4=critical only).

    Returns:
        A new ReviewResult with low-severity findings removed.
    """
    if min_severity <= 0:
        return result

    filtered_sections: list[CategorySection] = []
    for section in result.summary_sections:
        kept_findings: list[Finding] = []
        for finding in section.findings:
            # Critical security findings always pass
            if _is_critical_security(finding.text):
                kept_findings.append(finding)
                continue

            severity = compute_finding_severity(
                category=section.title,
                confidence=finding.confidence,
            )
            if severity >= min_severity:
                kept_findings.append(finding)

        if kept_findings:
            filtered_sections.append(
                CategorySection(
                    emoji=section.emoji,
                    title=section.title,
                    findings=kept_findings,
                )
            )

    filtered_inline: list[InlineComment] = []
    for comment in result.inline_comments:
        # Critical security findings always pass
        if _is_critical_security(comment.body):
            filtered_inline.append(comment)
            continue

        category = _infer_comment_category(comment.body)
        severity = compute_finding_severity(
            category=category,
            confidence=comment.confidence,
        )
        if severity >= min_severity:
            filtered_inline.append(comment)

    # If everything was filtered, return LGTM
    if not filtered_sections and not filtered_inline:
        return _create_lgtm_result(result, min_severity)

    new_verdict = _recompute_verdict(
        result.verdict,
        filtered_sections,
        filtered_inline,
    )

    return ReviewResult(
        verdict=new_verdict,
        summary_sections=filtered_sections,
        inline_comments=filtered_inline,
        persona_name=result.persona_name,
        pr_url=result.pr_url,
    )
