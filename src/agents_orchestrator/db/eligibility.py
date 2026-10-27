"""The pick's predicate and the handover trigger, shared by the pass, `set_replicas` and the hold.

They live apart from `db/leadership.py` because that module imports `db/queue.py`, and the
claim hold in the queue needs the same SQL. Every eligibility and handover-pending predicate is
spelled once here; `leadership`, `queue` and `sessions` embed these fragments and never respell
them.
"""

from __future__ import annotations

# An agent stays when its ordinal is below its session's `spec.replicas` (a spec without one
# never removes anyone). It can lead when it also is ready, not gone and has a wrapper session.
# `STAYS`, `READY_STAYS` and `ELIGIBLE` take the agent alias `a`; `ELIGIBLE` is the pick's
# predicate and `sessions.set_replicas` and the claim hold share it, `READY_STAYS` is the manual
# make-leader target's (no wrapper session required). `REPLICAS` takes the session alias `c`.
# Used inside a subquery, an inner `a` shadows an outer one, which is what the callers rely on.
NO_LIMIT = 2147483647
REPLICAS = f"coalesce((c.spec->>'replicas')::int, {NO_LIMIT})"
STAYS = (
    "a.ordinal < coalesce((SELECT (cs.spec->>'replicas')::int FROM sessions cs"
    f" WHERE cs.id = a.session_id), {NO_LIMIT})"
)
READY_STAYS = f"a.ready AND a.gone_at IS NULL AND {STAYS}"
ELIGIBLE = f"{READY_STAYS} AND a.wrapper_session_id IS NOT NULL"

# The handover trigger: the leader compacted in its term, or its pod is leaving. Aliases: `l`
# the `session_leaders` row, `a` the leader's agent, `c` the session. `LEAVING` is the ordinal
# test alone (the pass reads it as a column); the trigger also requires the agent not gone.
LEAVING = f"a.ordinal >= {REPLICAS}"
COMPACTED = (
    "coalesce((SELECT s.compaction_at > l.since FROM agent_status s"
    " WHERE s.agent_id = l.agent_id), false)"
)
HANDOVER_TRIGGER = f"({COMPACTED} OR (a.gone_at IS NULL AND {LEAVING}))"
