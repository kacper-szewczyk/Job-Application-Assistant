"""Local review UI (T18, ADR 0022): server-rendered pages over the same services as the CLI.

The server binds to the loopback interface only, needs the one-time link `jaa ui` prints, and
loads nothing from the network. Routes carry ids, never personal values.
"""
