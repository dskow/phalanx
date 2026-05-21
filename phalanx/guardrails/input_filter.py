"""Input filter — neutralizes prompt-injection in repository content.

Scans content the agent is about to ingest for known injection
patterns and replaces hits with a placeholder token. The function is
intentionally non-halting: a hit annotates the audit log and surrounding
text survives, because the agent legitimately needs to read source files
that may *mention* these phrases without acting on them.

The four pattern families correspond to the threat model in
``docs/GUARDRAILS.md``:

- Instruction overrides    — ``ignore previous instructions``
- Role overrides           — ``you are now``, ``act as``, ``from now on``
- Exfiltration prompts     — ``print your environment``, ``reveal your system prompt``
- Tool-call smuggling      — bare ``<tool_use>`` / ``<function_calls>`` wrappers

The filter is deterministic (regex only, no LLM in the decision path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLACEHOLDER = "[FILTERED:{family}]"


@dataclass(frozen=True)
class _Pattern:
    family: str
    description: str
    regex: re.Pattern[str]


_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        family="instruction-override",
        description="ignore (previous|prior|all|above) instructions",
        regex=re.compile(
            r"\bignore\s+(?:the\s+)?"
            r"(?:(?:previous|prior|all|above|earlier|preceding)\s+){1,3}"
            r"(?:instructions?|prompts?|rules?|directives?)\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        family="instruction-override",
        description="disregard/forget prior instructions",
        regex=re.compile(
            r"\b(?:disregard|forget|override)\s+(?:the\s+)?"
            r"(?:previous|prior|all|above|earlier|system)\s+"
            r"(?:instructions?|prompts?|rules?|directives?)\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        family="role-override",
        description="you are now / from now on / act as",
        regex=re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on(?:\s+you(?:\s+will)?)?|"
            r"act\s+as|pretend\s+to\s+be|roleplay\s+as|"
            r"you\s+are\s+no\s+longer)\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        family="role-override",
        description="developer/system/jailbreak mode marker",
        regex=re.compile(
            r"\b(?:developer|system|jailbreak|unrestricted|dan)\s+mode\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        family="exfiltration",
        description="reveal/print/leak system prompt or env",
        regex=re.compile(
            r"\b(?:reveal|print|emit|output|leak|disclose|show|dump)\s+"
            r"(?:your\s+|the\s+|all\s+|any\s+)?"
            r"(?:system\s+prompt|prompt|environment(?:\s+variables?)?|"
            r"env(?:\s+vars?)?|secrets?|api[_\s-]?keys?|credentials?)\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        family="tool-call-smuggling",
        description="bare <tool_use>/<function_calls> wrapper",
        regex=re.compile(
            r"</?(?:tool_use|tool_call|function_calls?|invoke|antml:function_calls?|"
            r"antml:invoke)\b[^>]*>",
            re.IGNORECASE,
        ),
    ),
)


def neutralize(content: str) -> tuple[str, list[str]]:
    """Replace prompt-injection patterns with placeholder tokens.

    Returns the filtered content and a list of human-readable hit
    descriptions suitable for the audit log. The hit list is empty
    when no patterns matched; in that case the content is returned
    byte-for-byte unchanged.
    """
    hits: list[str] = []
    filtered = content
    for pattern in _PATTERNS:
        matches = list(pattern.regex.finditer(filtered))
        if not matches:
            continue
        for match in matches:
            hits.append(
                f"{pattern.family}: {pattern.description} "
                f"(matched {match.group(0)!r})"
            )
        filtered = pattern.regex.sub(
            PLACEHOLDER.format(family=pattern.family), filtered
        )
    return filtered, hits
