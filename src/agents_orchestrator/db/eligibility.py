"""The pick's predicate, shared by the leadership pass, `set_replicas` and the claim hold.

It lives apart from `db/leadership.py` because that module imports `db/queue.py`, and the
claim hold in the queue needs the same SQL.
"""

from __future__ import annotations

# An agent stays when its ordinal is below its session's `spec.replicas` (a spec without one
# never removes anyone). It can lead when it also is ready, not gone and has a wrapper session.
# Both take the agent alias `a`; `ELIGIBLE` is the pick's predicate and `sessions.set_replicas`
# and the claim hold share it. `REPLICAS` takes the session alias `c`.
NO_LIMIT = 2147483647
REPLICAS = "coalesce((c.spec->>'replicas')::int, 2147483647)"
STAYS = (
    "a.ordinal < coalesce((SELECT (cs.spec->>'replicas')::int FROM sessions cs"
    f" WHERE cs.id = a.session_id), {NO_LIMIT})"
)
ELIGIBLE = f"a.ready AND a.gone_at IS NULL AND a.wrapper_session_id IS NOT NULL AND {STAYS}"
