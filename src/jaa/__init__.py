"""Local-first Job Application Assistant."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jaa")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.18.0"
