"""Snapshot storage port. Does NOT persist graphs, locks, events or checkpoints.

Methods are synchronous and must be fast/non-blocking. The memory implementation
is single-process, like WorkflowRunner. A future database-backed service should
introduce async repository methods rather than block the event loop here.
"""
from copy import deepcopy
from typing import Protocol

from .state import RunSnapshot


class RunStore(Protocol):
    def create(self, snapshot: RunSnapshot) -> None:
        """Atomically insert; raise KeyError if run_id already exists."""
        ...

    def save(self, snapshot: RunSnapshot) -> None:
        """Replace an existing snapshot; raise KeyError if missing."""
        ...

    def get(self, run_id: str) -> RunSnapshot:
        """Return a detached snapshot; raise KeyError if missing."""
        ...


class InMemoryRunStore:
    def __init__(self):
        self._snapshots: dict[str, RunSnapshot] = {}

    def create(self, snapshot):
        if snapshot.run_id in self._snapshots:
            raise KeyError(snapshot.run_id)
        self._snapshots[snapshot.run_id] = deepcopy(snapshot)

    def save(self, snapshot):
        if snapshot.run_id not in self._snapshots:
            raise KeyError(snapshot.run_id)
        self._snapshots[snapshot.run_id] = deepcopy(snapshot)

    def get(self, run_id):
        return deepcopy(self._snapshots[run_id])
