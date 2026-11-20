"""Each page script declares a top-level function name once.

The page's scripts are classic scripts, so a second `function name` in a file replaces the first without an
error, and every caller of the first then gets the second's result.
"""

import re
from collections import Counter
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "cockpit" / "static"
DECLARED = re.compile(r"^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def test_each_script_declares_a_function_name_once():
    twice = {}
    for script in sorted(STATIC.rglob("*.js")):
        counts = Counter(DECLARED.findall(script.read_text(encoding="utf-8")))
        names = sorted(name for name, count in counts.items() if count > 1)
        if names:
            twice[str(script.relative_to(STATIC))] = names
    assert twice == {}
