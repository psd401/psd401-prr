"""Import a gate runner from `scripts/`, which is not an importable package.

The three merge gates that are plain scripts rather than Django code --
`patch_coverage.py`, `mutation_score.py` and `pyright_baseline.py` -- are each
tested as a module, and each needed the same seven lines of `importlib`
plumbing to get there. This is that, once.

Deliberately not named `test_*`: the Django runner collects by that prefix, and
this file holds no tests of its own.
"""
import importlib.util
from pathlib import Path
from types import ModuleType

from django.conf import settings


def load_gate_script(filename: str) -> ModuleType:
    """The module in `scripts/<filename>`, imported under a `prr_` alias."""
    path = Path(settings.BASE_DIR) / "scripts" / filename
    spec = importlib.util.spec_from_file_location(f"prr_{path.stem}", path)
    # Without this, a renamed or moved script fails as
    # `AttributeError: 'NoneType' has no attribute 'loader'` rather than
    # saying which file it could not find.
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
