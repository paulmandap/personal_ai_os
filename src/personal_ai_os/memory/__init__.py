"""Persistent memory.

Structured state lives in SQLite (see docs/decisions.md, ADR-015). Semantic
retrieval is deliberately absent until retrieval is *measured* to be the
bottleneck -- an embedding model would compete for the same 8 GB of VRAM the
reasoning model needs, which is a real cost to pay for a hypothetical benefit.
"""
