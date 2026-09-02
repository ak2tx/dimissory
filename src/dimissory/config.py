"""Settings: one commented file, meant to be edited by hand.

A config a person cannot read is a config they cannot change, so this writes
TOML with the comments included and never rewrites the file behind them. Every
value has a default that works, and `dim config` prints where each one actually
came from -- because "is my setting being used?" is the question a config file
should be able to answer about itself.
"""

from __future__ import annotations

import os
import tomllib

DEFAULT_PATH = os.path.expanduser("~/.dimissory/config.toml")

DEFAULTS = {
    "window": {
        # Issue the letter with budget left, not at the wall. This is the whole
        # difference from every tool that reacts to a 429: at 0.85 the agent can
        # still think, and the half it writes is the valuable half.
        "write_at": 0.85,
        # How long to wait for the agent's half before writing without it. A
        # degraded letter beats no letter, and it is labelled either way.
        "grace": "5m",
    },
    "letters": {
        "dir": "~/.dimissory/letters",
        "keep": 50,
    },
    "agents": {
        "claude": True,
        "codex": True,
        "grok": True,
    },
}

TEMPLATE = '''\
# dimissory -- settings
#
# Everything here has a working default; delete a line to go back to it.
# `dim config` shows the effective value and where it came from.

[window]
# Issue the letter when this much of the plan window is gone (0.0 - 1.0).
# Lower means more warning and a better letter, because the agent still has
# budget to write its half. Higher means fewer letters.
write_at = {write_at}

# How long to wait for the agent's half before writing the letter anyway.
# A letter without it is marked DEGRADED rather than looking finished.
grace = "{grace}"

[letters]
# Where letters are written.
dir = "{dir}"

# How many to keep. Older ones are pruned.
keep = {keep}

[agents]
# Which agent CLIs to write letters for.
claude = {claude}
codex = {codex}
grok = {grok}
'''


def _toml_str(value):
    """A TOML basic string that survives a Windows path.

    `dim setup` on Windows wrote `dir = "C:\\Users\\me\\..."` with the
    backslashes raw. `\\U` is a unicode escape in a TOML basic string, so the
    tool could not parse the config it had just written -- and fell back to
    defaults while the operator's file sat there looking used. A wrong location
    reported as success, which is the defect this project inherited a whole file
    about.

    Escaped rather than emitted as a TOML literal string, because a literal
    string has no escape at all and would break on a path containing a quote.
    """
    out = str(value)
    for bad, good in (("\\", "\\\\"), ('"', '\\"'),
                      ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        out = out.replace(bad, good)
    return out


class Config:
    """Effective settings, and an honest account of where each came from."""

    def __init__(self, values, sources, path):
        self.values, self.sources, self.path = values, sources, path

    def get(self, section, key):
        return self.values.get(section, {}).get(key)

    def source(self, section, key):
        return self.sources.get(f"{section}.{key}", "default")

    @property
    def letters_dir(self):
        return os.path.expanduser(str(self.get("letters", "dir")))

    @classmethod
    def load(cls, path=None):
        """Defaults, overlaid with the file if it parses.

        An unreadable config is REPORTED, not silently replaced with defaults.
        Running on defaults while the operator believes their file is in effect
        is the failure this project's predecessor kept a file about.
        """
        path = os.path.expanduser(path or DEFAULT_PATH)
        values = {k: dict(v) for k, v in DEFAULTS.items()}
        sources, problem = {}, None
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    loaded = tomllib.load(fh)
                for section, items in loaded.items():
                    if not isinstance(items, dict):
                        continue
                    for key, val in items.items():
                        values.setdefault(section, {})[key] = val
                        sources[f"{section}.{key}"] = path
            except (OSError, tomllib.TOMLDecodeError) as e:
                problem = f"{path}: {e}"
        cfg = cls(values, sources, path)
        cfg.problem = problem
        return cfg

    def render(self):
        flat = {}
        for section, items in self.values.items():
            for key, val in items.items():
                if val is True:
                    flat[key] = "true"
                elif val is False:
                    flat[key] = "false"
                elif isinstance(val, str):
                    flat[key] = _toml_str(val)
                else:
                    flat[key] = val
        return TEMPLATE.format(**flat)

    def write(self, path=None, force=False):
        """Write the commented file. Never clobbers without being asked.

        Returns the path written, or None when a file already existed --
        because "wrote your config" and "left your config alone" are different
        outcomes and setup has to be able to tell you which happened.
        """
        path = os.path.expanduser(path or self.path)
        if os.path.exists(path) and not force:
            return None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(self.render())
        os.replace(tmp, path)
        return path
