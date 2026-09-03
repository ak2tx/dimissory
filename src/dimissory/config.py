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
        # How long a letter sent out WITHOUT the agent's half stays eligible
        # to be rewritten once that half arrives.
        #
        # This setting previously described waiting before writing at all,
        # which a hook cannot do: blocking a tool-call hook for five minutes
        # freezes the session, and a session that dies mid-wait leaves no
        # letter -- strictly worse than a degraded one. The letter now goes
        # out immediately and improves, which is the same intent with a
        # better guarantee. It was also read by nothing at all, which is why
        # the description could drift this far from the behaviour.
        "grace": "5m",
        # Crossing the margin is not a one-off event: the window stays past it
        # for the rest of the session. Without an interval, every tool call
        # after the crossing seals another letter and shells out to git to do
        # it. Long enough to be quiet, short enough that the letter still
        # describes the session you are actually in.
        "reseal_after": "10m",
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

# A letter sealed before the agent declared anything is marked DEGRADED. For
# this long afterwards, it is rewritten as soon as the agent does declare.
grace = "{grace}"

# Once past write_at the window stays past it, so the letter is refreshed on
# this interval rather than on every tool call.
reseal_after = "{reseal_after}"

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


def seconds(value, default=None):
    """A duration like "10m" or "90s" as seconds, or `default`.

    Never a guess. An unparseable duration returns the default rather than 0,
    because 0 here means "reseal on every single tool call" -- the setting
    silently inverting into the bug it exists to prevent.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else default
    text = str(value or "").strip().lower()
    if not text:
        return default
    unit = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}.get(text[-1])
    number = text[:-1] if unit else text
    try:
        n = float(number)
    except ValueError:
        return default
    return n * (unit or 1.0) if n >= 0 else default


def write_at(cfg, default=0.85):
    """The seal margin as a fraction. Never a bool, never `or`-defaulted.

    Two bugs in one line, both found in review. `cfg.get(...) or 0.85` turned
    a configured 0 -- "always seal" -- into 0.85. Replacing that with
    `isinstance(v, (int, float))` then admitted TOML `true`/`false`, because
    bool subclasses int, so `write_at = false` became 0.0 and ALSO meant
    "always seal". The only honest reading of a non-number here is that the
    setting was not usable.

    Lives here rather than in the CLI so the hook and `dim status` cannot
    disagree about where the margin is.
    """
    value = cfg.get("window", "write_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if 0.0 <= value <= 1.0 else default


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
        # DIMISSORY_CONFIG exists so a caller can be pointed at a specific
        # file. The hook loads config with no argument from deep inside an
        # event handler, so without this there is no way to exercise a setting
        # except by writing to the operator's real home -- and a test that
        # depends on the developer's own config is a test whose result is not
        # about the code.
        path = os.path.expanduser(path or os.environ.get("DIMISSORY_CONFIG")
                                  or DEFAULT_PATH)
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
