"""Filesystem utility functions shared across modules."""

import re


def sanitize_path_component(name: str, default: str = "_", max_len: int = 255) -> str:
    """Remove filesystem-unsafe characters, strip dots/spaces, truncate.

    Args:
        name: Raw string to sanitize.
        default: Fallback if result is empty.
        max_len: Maximum length of returned string.
    """
    sanitized = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name)
    sanitized = sanitized.strip(". ")
    return (sanitized or default)[:max_len]
