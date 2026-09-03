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
    path = resolve(path)
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
                out.extend(_anthropic_shape(d))
                out.extend(_codex_shape(d))
                out.extend(_grok_chat_shape(d))
                out.extend(_grok_updates_shape(d))
    except OSError:
        return None
    return tuple(out[-limit:])


def resolve(path):
    """The file to actually read, given whatever the host handed us.

    Defensive, not load-bearing, and the difference is worth recording.

    A real Grok session sealed a letter whose Observed block said `calls 0
    observed in the transcript tail` -- a MEASUREMENT, "this session made no
    tool calls", about a session that had just made six. Reporting zero where
    the answer was six is the exact defect this project exists to prevent, and
    four rounds of source review never saw it; running the thing on a real
    Grok box did.

    I then diagnosed it from a DIRECTORY LISTING: the session id named a
    directory full of jsonl files, so I concluded Grok passes the directory
    and wrote this to pick chat_history.jsonl out of it. The count stayed
    zero. Dumping a real hook payload showed Grok passes
    `<session>/updates.jsonl` -- a file, in a shape neither reader understood.
    See `_grok_updates_shape`, which is the actual fix.

    This stays because it is correct if a directory ever does arrive. It is
    kept as a note that reading a listing is not measuring an interface.
    """
    if not path:
        return None
    if os.path.isdir(path):
        for name in ("chat_history.jsonl", "events.jsonl", "transcript.jsonl"):
            candidate = os.path.join(path, name)
            if os.path.isfile(candidate):
                return candidate
        return None
    return path


def _step_from(args):
    """A one-line human hint at what a call was for. Never the arguments."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return ""
    if not isinstance(args, dict):
        return ""
    for key in ("description", "file_path", "target_file", "command",
                "pattern", "prompt", "path"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:48]
    return ""


def _anthropic_shape(d):
    """Claude Code and Codex: message.content[] entries of type tool_use."""
    content = (d.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        args = c.get("input")
        yield (c.get("name"), _step_from(args),
               args_hash(args) if args is not None else "(none)")


def _codex_shape(d):
    """Codex rollouts: response_item payloads of type custom_tool_call.

    The third transcript shape and the second silent zero. A real Codex
    session made fifteen tool calls and the letter's Observed block said
    `calls 0 observed in the transcript tail` -- a MEASUREMENT, about a
    session that had done plenty. dimissory had never read a Codex tool call.

        {"type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec",
                     "input": "const r = await tools.exec_command({...})"}}

    `input` is a JavaScript source string rather than JSON, so it is hashed as
    the string it is and the human hint is its first line. Hashing it still
    matters: the letter must never carry the command itself out of the machine.
    """
    if d.get("type") != "response_item":
        return
    p = d.get("payload")
    if not isinstance(p, dict) or p.get("type") != "custom_tool_call":
        return
    name = p.get("name")
    if not name:
        return
    args = p.get("input")
    step = ""
    if isinstance(args, str):
        first = args.strip().splitlines()[0] if args.strip() else ""
        step = first[:48]
    yield (name, step, args_hash(args) if args is not None else "(none)")


def _grok_updates_shape(d):
    """Grok, as the hook ACTUALLY hands it over: a session update stream.

    This is what measurement corrected. I inferred from a directory listing
    that Grok passes the session DIRECTORY, wrote `resolve` to pick
    chat_history.jsonl out of it, and the letter still said `calls 0`. Dumping
    a real payload showed the truth: Grok 1.0.13 passes
    `<session>/updates.jsonl`, a file, in a third shape again --

        {"params": {"update": {"sessionUpdate": "tool_call",
                               "title": "read_file",
                               "rawInput": {"target_file": "calc.py"},
                               "_meta": {"x.ai/tool": {"name": "read_file"}}}}}

    -- and my fix had been aimed at a path the hook never receives. The
    directory handling stays because it is correct if a directory ever does
    arrive, but it was never the reason the count was zero. Reading a listing
    is not measuring an interface.
    """
    update = (d.get("params") or {}).get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call":
        return
    meta = (update.get("_meta") or {}).get("x.ai/tool") or {}
    name = meta.get("name") or update.get("title")
    if not name:
        return
    args = update.get("rawInput")
    yield (name, _step_from(args),
           args_hash(args) if args is not None else "(none)")


def _grok_chat_shape(d):
    """Grok's chat_history.jsonl: {"type": "assistant", "tool_calls": [...]}.

    `arguments` is a JSON STRING rather than an object, which is why the hash
    is taken of the parsed value where possible -- two identical calls should
    hash the same regardless of key order in the serialised form.
    """
    if d.get("type") != "assistant":
        return
    calls = d.get("tool_calls")
    if not isinstance(calls, list):
        return
    for c in calls:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        args = c.get("arguments")
        parsed = args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except ValueError:
                parsed = args
        yield (c.get("name"), _step_from(args),
               args_hash(parsed) if parsed is not None else "(none)")
