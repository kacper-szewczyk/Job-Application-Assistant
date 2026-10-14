"""The connectors that ship with jaa, by policy key. The first real one arrives with T10 (#11)."""

from jaa.connectors.base import Connector

_CONNECTORS: tuple[Connector, ...] = ()


def available_connectors() -> dict[str, Connector]:
    """Every shipped connector, keyed by `SourcePolicy.key`."""
    return {c.policy.key: c for c in _CONNECTORS}
