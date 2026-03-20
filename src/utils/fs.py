"""Filesystem utility functions shared across modules."""

import os
import re
import shutil


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


def resolve_dir(parent: str, name: str) -> str:
    """Case-insensitive directory matching within parent.

    If a directory with the same name (case-insensitive) exists in parent,
    return its actual name. Otherwise return sanitized name.
    """
    sanitized = sanitize_path_component(name)
    if os.path.isdir(parent):
        lower = sanitized.lower()
        for entry in os.listdir(parent):
            if entry.lower() == lower and os.path.isdir(os.path.join(parent, entry)):
                return entry
    return sanitized


def move_to_music_dir(
    src_path: str,
    music_dir: str,
    artist: str,
    album: str,
    filename: str,
) -> str:
    """Move file from src_path to music_dir/{artist}/{album}/{filename}.

    Uses resolve_dir for case-insensitive folder matching.
    Creates directories if they don't exist.
    Returns the final file path.
    """
    artist_dir_name = resolve_dir(music_dir, artist)
    artist_dir = os.path.join(music_dir, artist_dir_name)
    album_dir_name = resolve_dir(artist_dir, album)
    album_dir = os.path.join(artist_dir, album_dir_name)
    os.makedirs(album_dir, exist_ok=True)
    dest_path = os.path.join(album_dir, sanitize_path_component(filename))
    shutil.move(src_path, dest_path)
    return dest_path
