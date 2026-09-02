"""Reading an agent transcript without becoming the reason it is slow.

Bounded on purpose. Transcripts reach tens of megabytes, and this runs on the
user's critical path: reading the whole file here would make a monitoring tool
the slowest thing in the loop. The tail is also the part that matters, because
a letter is written when the window is closing and what happened just before
that is the whole question.

Arguments are hashed, never copied. The letter is designed to be handed to
another vendor, so it must not carry file contents out of the machine.
"""

from __future__ import annotations

import hashlib
import json
import os

TAIL_BYTES = 1_000_000
LIMIT = 50


def args_hash(args) -> str:
    """A stable digest of a call's arguments. Never the arguments themselves."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return "(unhashable)"
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def recent_calls(path, limit=LIMIT, tail_bytes=TAIL_BYTES):
    """The last tool calls, or None when the transcript cannot be read.

    None, not []. An empty list is a measurement ("this session made no tool
    calls") and an unreadable file is not one. The caller turns None into an
    omitted line rather than into a zero.
    """
    if not path or not os.path.exists(path):
        return None
    out = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()                  # discard a partial line
            for raw in fh:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue                   # a torn tail line is normal
                content = (d.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    args = c.get("input")
                    step = ""
                    if isinstance(args, dict):
                        for key in ("description", "file_path", "command",
                                    "pattern", "prompt"):
                            v = args.get(key)
                            if isinstance(v, str) and v.strip():
                                step = v.strip().splitlines()[0][:48]
                                break
                    out.append((c.get("name"), step,
                                args_hash(args) if args is not None else "(none)"))
    except OSError:
        return None
    return tuple(out[-limit:])
