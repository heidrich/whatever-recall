"""whatever-recall — AI-native project memory.

The intelligence is stamped at write-time (when the AI already knows why), and a
dumb, token-free reader recalls it in microseconds. The code is the wiki.

Public surface:
    from recall import Index
    idx = Index.open(".recall/index.db")
    idx.stamp(title=..., anchors=[...], ...)
    idx.recall("rls cutover workspace_id")   # -> 3 levels
"""

from recall.engine import Index
from recall.db import SCHEMA_VERSION

__version__ = "0.1.0"
__all__ = ["Index", "SCHEMA_VERSION", "__version__"]
