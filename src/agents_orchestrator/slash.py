"""Whether a message text is a Claude Code slash command (design D2).

Mirrors claude's own parser, and the claude-code-wrapper turn guard applies the same rule: after
leading whitespace the text starts with "/"; the name runs up to the first space, tab or newline
and the args are the rest, stripped, newlines kept. An empty name is a command (claude answers
"Commands are in the form /command [args]" and the wrapper refuses it invalid_command); a name
matching the command regex is a command with no length cap; any other name (a path such as
"/etc/hosts") is prose that claude sends to the model.
"""

import re

# A trailing agents-common run marker, as built by agents_common.runs.marker
# (f"<!-- agents-{run_kind}:{run_id} -->") and posted by runs.py:_post_once as
# f"{text}\n\n{marker}". The orchestrator does not depend on agents-common.
_RUN_MARKER = re.compile(r"\n\n<!-- agents-[^\s:]+:\S+ -->\s*\Z")
_SLASH = re.compile(r"/([^ \t\n]*)(.*)", re.DOTALL)
_NAME = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9:_-]*")


def parse(text: str) -> tuple[str, str] | None:
    """Return (name, args) when text is a slash command, else None; name is "" when empty."""
    match = _SLASH.fullmatch(_RUN_MARKER.sub("", text).lstrip())
    if match is None:
        return None
    name, rest = match.groups()
    if name and not _NAME.fullmatch(name):
        return None
    return name, rest.strip()
