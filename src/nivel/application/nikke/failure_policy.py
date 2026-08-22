"""When to stop trying.

Shared by the board walk and the metadata backfill: both loop over a sequence of
requests to the same host, and both would rather abandon a run than keep hammering
a site that has started refusing.
"""

# Give up on a board after this many consecutive page failures rather than
# aborting the whole run on the first transient error.
MAX_CONSECUTIVE_PAGE_FAILURES = 3
