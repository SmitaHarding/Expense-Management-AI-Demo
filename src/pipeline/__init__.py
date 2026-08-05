"""Deterministic + AI pipeline stages for the expense management demo.

Design principle (unchanged from the parent project): AI reads, rules decide,
humans judge exceptions. Everything in this package that touches money or
routing is a pure, deterministic function driven by config. The only AI
touchpoints are in `extraction.py` (dual-path receipt reading) and
`categorize.py` (category proposal) — both treated as untrusted data until
validated.
"""
