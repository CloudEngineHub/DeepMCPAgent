"""Internal core of the Agent Identity subsystem.

This package holds the abstractions that every provider builds on:
the cache primitive, the exchange engine, the abstract base class,
and the two concrete bases (file-backed and callable-backed). Code
outside ``promptise.identity`` should import from the top-level
``promptise.identity`` namespace rather than reaching into ``_core``.
"""

from __future__ import annotations
