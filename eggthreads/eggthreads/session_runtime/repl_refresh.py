"""Refresh Egg-owned helpers in a persistent Python REPL without losing user state.

Executed by an Egg-owned guard eval in an isolated namespace. Inputs are
``repl_globals``, ``runtime_dir``, and ``expected_hash``.
"""

import sys
import types
from pathlib import Path

module = sys.modules.get("eggtools")
if module is not None:
    namespace = vars(module)
    old_namespace = dict(namespace)
    old_functions = {
        name: value
        for name, value in old_namespace.items()
        if isinstance(value, types.FunctionType)
        and str(getattr(value, "__module__", "")).startswith("eggtools")
    }
    metadata_names = (
        "__name__",
        "__file__",
        "__package__",
        "__loader__",
        "__spec__",
        "__cached__",
        "__builtins__",
    )
    metadata = {
        name: old_namespace[name]
        for name in metadata_names
        if name in old_namespace
    }
    source_path = Path(runtime_dir) / "eggtools.py"

    fresh_namespace = dict(metadata)
    exec(
        compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec"),
        fresh_namespace,
        fresh_namespace,
    )

    function_updates = []
    generated_globals_updates = {}
    generated_globals_replacements = {}
    for name, old_value in old_functions.items():
        new_value = fresh_namespace.get(name)
        if not isinstance(new_value, types.FunctionType):
            continue
        if old_value.__code__.co_freevars != new_value.__code__.co_freevars:
            continue
        function_updates.append((name, old_value, new_value))
        old_globals = old_value.__globals__
        if old_globals is not namespace and old_globals is not new_value.__globals__:
            generated_globals_updates[id(old_globals)] = (old_globals, new_value.__globals__)

    for old_globals, new_globals in generated_globals_updates.values():
        for name, old_value in old_globals.items():
            new_value = new_globals.get(name)
            if new_value is not None and new_value is not old_value:
                generated_globals_replacements[id(new_value)] = old_value

    def preserve_old_references(value):
        return generated_globals_replacements.get(id(value), value)

    function_snapshots = [
        (
            old_value,
            old_value.__code__,
            old_value.__defaults__,
            old_value.__kwdefaults__,
            dict(old_value.__annotations__),
            old_value.__doc__,
            dict(old_value.__dict__),
        )
        for _name, old_value, _new_value in function_updates
    ]
    generated_globals_snapshots = [
        (old_globals, dict(old_globals))
        for old_globals, _new_globals in generated_globals_updates.values()
    ]

    try:
        namespace.clear()
        namespace.update(fresh_namespace)
        for old_globals, new_globals in generated_globals_updates.values():
            old_globals.clear()
            old_globals.update(
                (name, preserve_old_references(value))
                for name, value in new_globals.items()
            )
        for name, old_value, new_value in function_updates:
            old_value.__code__ = new_value.__code__
            old_value.__defaults__ = (
                tuple(preserve_old_references(value) for value in new_value.__defaults__)
                if new_value.__defaults__ is not None
                else None
            )
            old_value.__kwdefaults__ = (
                {
                    key: preserve_old_references(value)
                    for key, value in new_value.__kwdefaults__.items()
                }
                if new_value.__kwdefaults__ is not None
                else None
            )
            old_value.__annotations__ = dict(new_value.__annotations__)
            old_value.__doc__ = new_value.__doc__
            old_value.__dict__.clear()
            old_value.__dict__.update(new_value.__dict__)
            namespace[name] = old_value
        for name, value in list(namespace.items()):
            namespace[name] = preserve_old_references(value)
    except BaseException:
        for old_globals, snapshot in generated_globals_snapshots:
            old_globals.clear()
            old_globals.update(snapshot)
        for old_value, code, defaults, kwdefaults, annotations, doc, attrs in function_snapshots:
            old_value.__code__ = code
            old_value.__defaults__ = defaults
            old_value.__kwdefaults__ = kwdefaults
            old_value.__annotations__ = annotations
            old_value.__doc__ = doc
            old_value.__dict__.clear()
            old_value.__dict__.update(attrs)
        namespace.clear()
        namespace.update(old_namespace)
        raise

repl_globals["__egg_runtime_code_hash__"] = expected_hash
