"""Draw.io task dataloader.

Each split directory (train/, val/, test/) contains an ``items.json``
file: a JSON array of diagram-generation task items. Items carry an
``instruction`` (the natural-language ask) and a ``requirements`` dict
(the deterministic scoring spec). The default
:meth:`SplitDataLoader.load_split_items` already reads the single JSON
array per split, so this subclass only needs to exist for registration.
"""
from __future__ import annotations

from skillopt.datasets.base import SplitDataLoader


class DrawioDataLoader(SplitDataLoader):
    """Dataloader for the Draw.io diagram-generation benchmark.

    Items live as a JSON array in ``<split>/items.json`` — the inherited
    ``load_split_items`` handles that layout directly.
    """
