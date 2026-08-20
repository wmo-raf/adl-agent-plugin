"""
How much an agent may send in one go.

Both numbers are the server's to state, not the agent's to assume: they are
handed out in the sync response so that a fleet already in the field follows
a change here without being reinstalled. Neither is a tuning knob — they
exist so that one machine having a bad day cannot take the instance with it.
"""

#: Candidate files per manifest call. A cycle with more candidates than this
#: pages (decision #267): the extra round trip is cheap, and an unbounded
#: batch from a folder nobody has looked at in a year is not.
MANIFEST_PAGE_LIMIT = 500

#: Bytes per uploaded file, after decompression. Vendor files are kilobytes
#: to a few megabytes; anything near this is a misconfiguration worth
#: refusing loudly rather than absorbing (decision #266).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
