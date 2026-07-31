"""
Sticker description cache for Telegram.

When users send stickers, we describe them via the vision tool and cache
the descriptions keyed by file_unique_id so we don't re-analyze the same
sticker image on every send. Descriptions are concise (1-2 sentences).

Cache location: ~/.hermes/sticker_cache.json
"""

import json
import time
from pathlib import Path
from typing import Optional

from hermes_runtime.config import get_hermes_home
from utils import advisory_file_lock, atomic_json_write


CACHE_PATH = get_hermes_home() / "sticker_cache.json"
_IMPORT_CACHE_PATH = CACHE_PATH

# Vision prompt for describing stickers -- kept concise to save tokens
STICKER_VISION_PROMPT = (
    "Describe this sticker in 1-2 sentences. Focus on what it depicts -- "
    "character, action, emotion. Be concise and objective."
)


def _cache_path() -> Path:
    if CACHE_PATH != _IMPORT_CACHE_PATH:
        return CACHE_PATH
    return get_hermes_home() / "sticker_cache.json"


def _load_cache(path: Optional[Path] = None) -> dict:
    """Load the sticker cache from disk."""
    path = path or _cache_path()
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
            return cache if isinstance(cache, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict, path: Optional[Path] = None) -> None:
    """Save the sticker cache to disk atomically."""
    atomic_json_write(path or _cache_path(), cache)


def get_cached_description(file_unique_id: str) -> Optional[dict]:
    """
    Look up a cached sticker description.

    Returns:
        dict with keys {description, emoji, set_name, cached_at} or None.
    """
    cache = _load_cache()
    entry = cache.get(file_unique_id)
    return entry if isinstance(entry, dict) else None


def cache_sticker_description(
    file_unique_id: str,
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> None:
    """
    Store a sticker description in the cache.

    Args:
        file_unique_id: Telegram's stable sticker identifier.
        description:    Vision-generated description text.
        emoji:          Associated emoji (e.g. "😀").
        set_name:       Sticker set name if available.
    """
    path = _cache_path()
    with advisory_file_lock(path.with_suffix(".lock")):
        cache = _load_cache(path)
        cache[file_unique_id] = {
            "description": description,
            "emoji": emoji,
            "set_name": set_name,
            "cached_at": time.time(),
        }
        _save_cache(cache, path)


def build_sticker_injection(
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> str:
    """
    Build the warm-style injection text for a sticker description.

    Returns a string like:
      [The user sent a sticker 😀 from "MyPack"~ It shows: "A cat waving" (=^.w.^=)]
    """
    context = ""
    if set_name and emoji:
        context = f" {emoji} from \"{set_name}\""
    elif emoji:
        context = f" {emoji}"

    return f"[The user sent a sticker{context}~ It shows: \"{description}\" (=^.w.^=)]"


def build_animated_sticker_injection(emoji: str = "") -> str:
    """
    Build injection text for animated/video stickers we can't analyze.
    """
    if emoji:
        return (
            f"[The user sent an animated sticker {emoji}~ "
            f"I can't see animated ones yet, but the emoji suggests: {emoji}]"
        )
    return "[The user sent an animated sticker~ I can't see animated ones yet]"
