"""The connectors that ship with jaa, by policy key."""

from jaa.connectors.base import Connector
from jaa.connectors.solid_jobs import SolidJobs

_CONNECTORS: tuple[Connector, ...] = (SolidJobs(),)


def available_connectors() -> dict[str, Connector]:
    """Every shipped connector, keyed by `SourcePolicy.key`."""
    return {c.policy.key: c for c in _CONNECTORS}
