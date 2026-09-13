from __future__ import annotations

import copy
import re
from typing import Any

from .util import SoloAIError

_HOST_KIND = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_MAX_THREAD_ID_LENGTH = 256


def host_reference(kind: str | None, thread_id: str | None) -> dict[str, str] | None:
    """规范化由宿主提供的任务会话引用，绝不从标题或进程状态推测。"""

    if kind is None and thread_id is None:
        return None
    if not kind or not thread_id:
        raise SoloAIError("Host kind and host thread must be provided together")
    if not _HOST_KIND.fullmatch(kind):
        raise SoloAIError(
            "Host kind must use lowercase letters, digits, dots, underscores, or hyphens"
        )
    if len(thread_id) > _MAX_THREAD_ID_LENGTH or any(
        character.isspace() or ord(character) < 32 for character in thread_id
    ):
        raise SoloAIError(
            "Host thread must be a nonblank single-line identifier up to 256 characters"
        )
    return {"kind": kind, "thread_id": thread_id}


def normalize_host_reference(value: dict[str, Any] | None) -> dict[str, str] | None:
    """拒绝未审查的宿主记录，保证持久化引用可由宿主精确核对。"""

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "thread_id"}:
        raise SoloAIError("Host reference must contain exactly kind and thread_id")
    kind = value.get("kind")
    thread_id = value.get("thread_id")
    if not isinstance(kind, str) or not isinstance(thread_id, str):
        raise SoloAIError("Host reference fields must be strings")
    normalized = host_reference(kind, thread_id)
    if normalized is None:
        raise SoloAIError("Host reference cannot be empty")
    return copy.deepcopy(normalized)
