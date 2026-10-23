"""App discovery for the G2 launcher: every g2/apps/<name>.py that defines an
App subclass (or APP = ...) is a menu entry. Files starting with "_" and
base.py are skipped, so _template.py is safe to copy.

    python g2/dev.py list        # what the launcher will show
"""
from __future__ import annotations

import importlib
import inspect
import logging
import sys
from pathlib import Path

from g2.apps.base import App, AppContext  # noqa: F401

HERE = Path(__file__).resolve().parent
log = logging.getLogger("g2.apps")
_mtimes: dict[str, float] = {}


def module_names() -> list[str]:
    return sorted(p.stem for p in HERE.glob("*.py")
                  if not p.name.startswith("_") and p.stem not in ("base",) and not p.stem.startswith("test_"))


def _instantiate(mod) -> App | None:
    app = getattr(mod, "APP", None)
    if isinstance(app, App):
        return app
    classes = [c for _, c in inspect.getmembers(mod, inspect.isclass)
               if issubclass(c, App) and c is not App and c.__module__ == mod.__name__]
    if not classes:
        return None
    classes.sort(key=lambda c: inspect.getsourcelines(c)[1])
    return classes[-1]()           # the last class defined in the file wins


def discover(reload: bool = False, only: set[str] | None = None) -> list[App]:
    apps: list[App] = []
    for name in module_names():
        if only is not None and name not in only:
            continue
        qual = f"g2.apps.{name}"
        try:
            if qual in sys.modules and reload:
                mod = importlib.reload(sys.modules[qual])
            else:
                mod = importlib.import_module(qual)
            _mtimes[name] = (HERE / f"{name}.py").stat().st_mtime
            app = _instantiate(mod)
            if app is None:
                log.warning("%s defines no App; skipped", name)
                continue
            app.module = name  # type: ignore[attr-defined]
            apps.append(app)
        except Exception:
            log.exception("app %s failed to load; skipped", name)
    apps.sort(key=lambda a: (a.order, a.name.lower()))
    return apps


def changed_modules() -> set[str]:
    """Modules whose file changed since discover() loaded them (for hot reload)."""
    out = set()
    names = module_names()
    for name in names:
        try:
            m = (HERE / f"{name}.py").stat().st_mtime
        except OSError:
            continue
        if name not in _mtimes or m != _mtimes[name]:
            out.add(name)
    for name in list(_mtimes):            # deleted files count as a change too
        if name not in names:
            out.add(name)
            del _mtimes[name]
    return out
