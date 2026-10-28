"""When the discard saves a cache target: the one rule the reconciler records and the api reports.

The reconciler's `CacheRunner.save_step` writes `skipped` with this reason; the session view's
`backup` shows the same answer before a discard (deploy-safe-agent-sessions D7, D8). It reads
only `status.cache`, so the api can apply it without importing the reconciler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents_orchestrator.cache.presign import CacheTarget

# A save runs only after a restore that left the workdir trustworthy: it unpacked the archive,
# found none (first use), or emptied the workdir because the archive was unreadable (`reset`, so
# the save overwrites the bad object). Every other outcome, present or future, skips the save.
SAVE_OK_RESTORE_STATES = frozenset({"restored", "miss", "reset"})
# A sessions restore skipped because the session's pods existed when the feature arrived leaves
# the transcripts the pods wrote, so they are saved as after a successful restore.
PODS_EXIST = "pods_exist"
# No sessions restore record and no workspace PVC: the session was discarded before its first
# converge, so no pod ever wrote /sessions and the save Job would wait on a volume never created.
NEVER_CONVERGED = "never_converged"


def restore_key(target: CacheTarget) -> str:
    """The `status.cache` key of the restore run of `target`."""
    return "sessions_restore" if target == "sessions" else "restore"


def _pods_wrote_sessions(restore: dict[str, Any] | None, target: CacheTarget) -> bool:
    """No sessions restore ran: the session predates it, or its pods existed (`pods_exist`)."""
    if target != "sessions":
        return False
    return restore is None or (
        restore.get("state") == "skipped" and restore.get("reason") == PODS_EXIST
    )


def _restore_skip(restore: dict[str, Any] | None) -> str | None:
    """Why a save may not follow this restore run, or None when it may."""
    state = (restore or {}).get("state")
    if restore is None or state == "running":
        return "restore_running"
    if state in SAVE_OK_RESTORE_STATES:
        return None
    if state == "failed":
        head = str(restore.get("reason") or "").split(":")[0].split(" ")[0]
        return head or "restore_failed"
    return f"restore_{state}"


def save_skip(
    cache_available: bool, cache: dict[str, Any], target: CacheTarget, *, converged: bool = True
) -> str | None:
    """Why the discard skips the save of `target` (`status.cache` as `cache`), or None to save.

    `converged` is whether the session's workspace PVC exists. Without a sessions restore record
    it tells a session that predates the restore (its pods ran, the PVC exists) from one
    discarded before its first converge (`never_converged`, nothing to save).
    """
    if not cache_available:
        return "not_configured"
    restore = cache.get(restore_key(target))
    if _pods_wrote_sessions(restore, target):
        return None if converged or restore is not None else NEVER_CONVERGED
    return _restore_skip(restore)
