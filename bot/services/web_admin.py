"""Tiny web admin panel — shows the admin-action audit log in a browser.

A read-only dashboard so the owner can see, at a glance, which admin is doing
what (approve / reject / edit / remove / addid / admin & trusted management),
plus who the admins and trusted reporters are.

Opt-in: does nothing unless BOTH WEB_ADMIN_PORT and WEB_ADMIN_PASS are set in
.env. Protected by HTTP Basic auth (WEB_ADMIN_USER / WEB_ADMIN_PASS).

Security note: Basic auth over plain HTTP sends the password in (base64, not
encrypted) on every request. For anything public, put it behind nginx + HTTPS
or reach it over an SSH tunnel. Use a long random WEB_ADMIN_PASS.
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import secrets

from aiohttp import web

from bot.db import recent_admin_actions, admin_action_counts, list_trusted_reporters
from bot.services.admins import list_all_admins

logger = logging.getLogger(__name__)

_ROLE_LABEL = {"owner": "👑 Owner", "env": "🔧 Admin (.env)", "db": "🛠 Admin"}

_ACTION_COLOR = {
    "approve": "#16a34a", "addid": "#16a34a", "quickadd": "#16a34a",
    "reject":  "#dc2626", "remove": "#dc2626", "removeadmin": "#dc2626",
    "removetrusted": "#dc2626",
    "edit":    "#d97706",
    "addadmin": "#7c3aed", "addtrusted": "#7c3aed",
}

_CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
  background:#0f1115;color:#e6e8eb}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#9aa3ad;font-size:13px;margin-bottom:20px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.card{background:#171a21;border:1px solid #232833;border-radius:10px;
  padding:14px 18px;min-width:130px}
.card .n{font-size:24px;font-weight:700}
.card .l{color:#9aa3ad;font-size:12px;margin-top:2px}
h2{font-size:15px;margin:24px 0 8px;color:#c7ccd3}
table{width:100%;border-collapse:collapse;background:#171a21;
  border:1px solid #232833;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;font-size:13px;border-bottom:1px solid #232833}
th{background:#1c2029;color:#9aa3ad;font-weight:600}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;color:#fff;
  font-size:11px;font-weight:600}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#aab2bd}
.muted{color:#6b7280}
"""


def _authorized(request: web.Request) -> bool:
    user = os.getenv("WEB_ADMIN_USER", "admin")
    pw   = os.getenv("WEB_ADMIN_PASS", "")
    if not pw:
        return False
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(hdr[6:]).decode("utf-8")
        u, _, p = raw.partition(":")
    except Exception:
        return False
    return secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)


@web.middleware
async def _auth_mw(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    if not _authorized(request):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Scammer Bot Admin"'},
            text="401 Unauthorized",
        )
    return await handler(request)


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _actor(a: dict) -> str:
    if a.get("actor_username"):
        return "@" + _esc(a["actor_username"])
    return f'<span class="mono">{_esc(a.get("actor_id") or "—")}</span>'


def _when(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return _esc(dt)


def _render(actions, counts, admins, trusted) -> str:
    # Summary cards
    cards = (
        f'<div class="card"><div class="n">{len(admins)}</div><div class="l">Admins</div></div>'
        f'<div class="card"><div class="n">{len(trusted)}</div><div class="l">Trusted reporters</div></div>'
        f'<div class="card"><div class="n">{len(actions)}</div><div class="l">Recent actions</div></div>'
    )

    # Admins (with their activity)
    count_map = {c["actor_id"]: c for c in counts}
    arows = []
    for a in admins:
        c = count_map.get(a["telegram_id"]) or {}
        arows.append(
            "<tr>"
            f'<td>{_ROLE_LABEL.get(a.get("source"), "Admin")}</td>'
            f'<td class="mono">{_esc(a["telegram_id"])}</td>'
            f'<td>{_esc(c.get("total") or 0)}</td>'
            f'<td>{_when(c["last_action"]) if c.get("last_action") else "<span class=muted>never</span>"}</td>'
            "</tr>"
        )
    admins_html = (
        "<h2>Admins</h2><table><tr><th>Role</th><th>Telegram ID</th>"
        "<th>Actions</th><th>Last action</th></tr>" + "".join(arows) + "</table>"
    )

    # Trusted reporters
    if trusted:
        trows = "".join(
            "<tr>"
            f'<td>{("@" + _esc(t["username"])) if t.get("username") else "—"}</td>'
            f'<td class="mono">{_esc(t["user_id"])}</td>'
            f'<td>{_esc(str(t.get("added_at",""))[:10])}</td>'
            "</tr>"
            for t in trusted
        )
        trusted_html = (
            "<h2>Trusted reporters (auto-approve)</h2><table>"
            "<tr><th>Username</th><th>ID</th><th>Added</th></tr>" + trows + "</table>"
        )
    else:
        trusted_html = "<h2>Trusted reporters</h2><p class='muted'>None.</p>"

    # Recent actions
    rows = []
    for a in actions:
        color = _ACTION_COLOR.get(a.get("action"), "#475569")
        rows.append(
            "<tr>"
            f"<td>{_when(a.get('created_at'))}</td>"
            f"<td>{_actor(a)}</td>"
            f'<td><span class="badge" style="background:{color}">{_esc(a.get("action"))}</span></td>'
            f'<td>{_esc(a.get("target_type") or "")} '
            f'<span class="mono">{_esc(a.get("target_id") or "")}</span></td>'
            f'<td>{_esc(a.get("detail") or "")}</td>'
            "</tr>"
        )
    actions_html = (
        "<h2>Recent admin actions</h2><table>"
        "<tr><th>When</th><th>Admin</th><th>Action</th><th>Target</th><th>Detail</th></tr>"
        + ("".join(rows) if rows else "<tr><td colspan=5 class='muted'>No actions logged yet.</td></tr>")
        + "</table>"
    )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta http-equiv='refresh' content='30'>"
        "<title>Scammer Bot — Admin Activity</title>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>"
        "<h1>🛡️ Scammer Bot — Admin Activity</h1>"
        "<div class='sub'>Auto-refreshes every 30s · read-only</div>"
        f"<div class='cards'>{cards}</div>"
        f"{admins_html}{trusted_html}{actions_html}"
        "</div></body></html>"
    )


async def _index(request: web.Request) -> web.Response:
    actions = await recent_admin_actions(300)
    counts  = await admin_action_counts()
    admins  = await list_all_admins()
    trusted = await list_trusted_reporters()
    return web.Response(text=_render(actions, counts, admins, trusted), content_type="text/html")


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_web_admin() -> None:
    """Background task — serves the panel if WEB_ADMIN_PORT + WEB_ADMIN_PASS set."""
    port = int(os.getenv("WEB_ADMIN_PORT", "0") or "0")
    if not port or not os.getenv("WEB_ADMIN_PASS"):
        logger.info("Web admin disabled (set WEB_ADMIN_PORT + WEB_ADMIN_PASS to enable)")
        return

    app = web.Application(middlewares=[_auth_mw])
    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Web admin panel listening on :%d", port)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
