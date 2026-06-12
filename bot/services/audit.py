"""Tiny convenience wrapper around db.log_admin_action.

Call `await audit(user, "approve", "scammer", scammer_id, "sev=high ...")`
from any handler — `user` is the telegram User who performed the action
(update.effective_user or query.from_user). Auditing never raises.
"""
from __future__ import annotations

import logging
from typing import Optional

from bot.db import log_admin_action

logger = logging.getLogger(__name__)


async def audit(
    user,
    action: str,
    target_type: Optional[str] = None,
    target_id=None,
    detail: Optional[str] = None,
) -> None:
    uid   = getattr(user, "id", None)
    uname = getattr(user, "username", None)
    await log_admin_action(uid, uname, action, target_type, target_id, detail)
