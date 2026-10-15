"""The connectors that ship with jaa, by policy key."""

from jaa.connectors.arbeitnow import Arbeitnow
from jaa.connectors.base import Connector
from jaa.connectors.greenhouse import Greenhouse
from jaa.connectors.lever import Lever
from jaa.connectors.personio import Personio
from jaa.connectors.remotive import Remotive
from jaa.connectors.solid_jobs import SolidJobs
from jaa.connectors.teamtailor import Teamtailor
from jaa.connectors.weworkremotely import WeWorkRemotely

_CONNECTORS: tuple[Connector, ...] = (
    SolidJobs(),
    Greenhouse(),
    Lever(),
    Teamtailor(),
    Personio(),
    Remotive(),
    Arbeitnow(),
    WeWorkRemotely(),
)


def available_connectors() -> dict[str, Connector]:
    """Every shipped connector, keyed by `SourcePolicy.key`."""
    return {c.policy.key: c for c in _CONNECTORS}
