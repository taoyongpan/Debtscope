"""Debtscope Harness — the agent shell around the deterministic analysis core.

Borrows the DeepSeek Harness paradigm ("Agent = Model + Harness"):
model adapters, tool contracts, agent loop, sessions and configuration live
here, while scanning/indexing stays deterministic in :mod:`debtscope.core`.
"""
