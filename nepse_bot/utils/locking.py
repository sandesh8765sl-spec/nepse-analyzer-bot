"""Small helper that makes a JSON store safe to use from several threads."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T", bound=type)


def synchronize_public_methods(cls: T) -> T:
    """Class decorator: run each public method while holding ``self._lock``.

    A store method typically loads the file, changes something, and saves it.
    Holding the lock for the whole method stops two threads from interleaving
    those steps and losing one of the updates.
    """
    for name, attribute in list(vars(cls).items()):
        if name.startswith("_") or not callable(attribute) or isinstance(attribute, (staticmethod, classmethod)):
            continue

        def _wrap(method: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(method)
            def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
                with self._lock:
                    return method(self, *args, **kwargs)

            return locked

        setattr(cls, name, _wrap(attribute))
    return cls
