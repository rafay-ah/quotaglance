"""Environment helpers for child processes.

The AppImage's AppRun points Python and GTK at bundled libraries and saves
the host's original values as ``QUOTAGLANCE_HOST_<NAME>``. Anything we spawn
(provider CLIs, the Extensions app) must get the host's environment back,
or it could load our bundled libraries instead of its own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

PREFIX = "QUOTAGLANCE_HOST_"
UNSET = "__QG_UNSET__"


def host_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    for key in [k for k in result if k.startswith(PREFIX)]:
        name = key[len(PREFIX):]
        value = result.pop(key)
        if value == UNSET:
            result.pop(name, None)
        else:
            result[name] = value
    return result


def overridden_names(env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Variables to reset in a launch context: name -> host value (None = unset)."""
    env = os.environ if env is None else env
    return {key[len(PREFIX):]: (None if value == UNSET else value)
            for key, value in env.items() if key.startswith(PREFIX)}
