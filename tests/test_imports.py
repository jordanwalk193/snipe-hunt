"""Every module imports on its own: removing dead code must not break one."""
import importlib
import pkgutil

import snipehunt


def test_every_module_imports():
    for mod in pkgutil.iter_modules(snipehunt.__path__):
        importlib.import_module(f"snipehunt.{mod.name}")
