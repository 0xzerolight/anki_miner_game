"""Frozen dataclasses and enums that every other package shares.

A fixed contract: change it only as a deliberate, reviewed contract change, never as a side effect
of a feature. ``tests/test_contracts.py`` pins the contract names, and mypy checks this package
strictly (``pyproject.toml``).
"""
