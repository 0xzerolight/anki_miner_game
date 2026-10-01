"""Protocols for every outside seam, from OBS and the text sources to the add-ons and the GUI.

A fixed contract, like ``models``: change it only as a deliberate, reviewed contract change, never
as a side effect of a feature. ``tests/test_contracts.py`` pins each Protocol's members, and mypy
checks this package strictly (``pyproject.toml``).
"""
