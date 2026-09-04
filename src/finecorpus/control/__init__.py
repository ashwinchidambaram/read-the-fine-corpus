"""Control-plane persistence for the index lifecycle.

The control-plane DB is the authority for alias records, which the retrieval
service reads for model-identity mismatch checks (§15, index-lifecycle.md §2.3).
"""
