"""Hermes observer — Milestone 3 (thin re-export shim).

Fix Round 5 (FR5-01): the implementation moved into
``hermes_plugin/observer.py`` so the plugin is self-contained when staged
alone (``hermes plugins doctor`` loads ONLY the hermes_plugin directory).
This module re-exports it so the existing test suite, examples, and any
top-level ``from hermes_observer import HermesObserver`` import keep
working unchanged. New code should import from ``hermes_plugin.observer``
directly.
"""

from __future__ import annotations

from hermes_plugin.observer import (
    ERROR_TYPES,
    EVENT_TYPES,
    OUTCOMES,
    HermesObserver,
)

__all__ = ["HermesObserver", "EVENT_TYPES", "OUTCOMES", "ERROR_TYPES"]
