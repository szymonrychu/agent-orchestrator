"""Loads tests/e2e/test_e2e_kind.py once for the default suite's checks of its helpers.

The kind e2e module is not importable as a package module (tests/e2e has no __init__.py), so
it is loaded from its path. It is registered in `sys.modules` before it runs: its dataclasses
resolve their string annotations through `sys.modules[cls.__module__]`, which fails on an
unregistered module (CI run 40676464845, test_e2e_client).
"""

from __future__ import annotations

import importlib.util
import sys
from functools import cache
from pathlib import Path
from types import ModuleType

E2E_KIND_PATH = Path(__file__).resolve().parent / "e2e" / "test_e2e_kind.py"


@cache
def e2e_kind() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_kind_under_test", E2E_KIND_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
