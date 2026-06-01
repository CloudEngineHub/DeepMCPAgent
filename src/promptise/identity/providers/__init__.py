"""Provider implementations — one module per identity-provider family.

Each module exposes its concrete provider classes plus a ``from_*``
factory. The factories are wrapped by
:class:`promptise.identity.AgentIdentity` classmethods; advanced users
may also call them directly. Nothing in this package should be
imported by code outside ``promptise.identity`` — use the top-level
``promptise.identity`` namespace.
"""

from __future__ import annotations
