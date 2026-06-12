"""Full web admin panel — view AND act on everything an admin can do in the bot.

Pages:
  /            dashboard (counts, admins, trusted reporters, recent actions)
  /pending     pending reports — approve (high/medium/low) or reject
  /scammers    scammer list — edit fields, remove, or add a new entry
  /admins      manage admins + trusted reporters

Every action here mirrors the matching bot command (/approve, /reject, /edit,
/remove, /addid, /addadmin, /removeadmin, /addtrusted, /removetrusted) —
broadcasts, kicks, and audit-log entries all happen exactly the same way, just
tagged "(via web)" and attributed to the "🌐 Web Panel" / @web-admin actor.

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
from types import SimpleNamespace

from aiohttp import web
from telegram.error import TelegramError

from bot.db import (
    recent_admin_actions, admin_action_counts, list_trusted_reporters,
    list_pending_reports, get_report, update_report_status, count_reports,
    add_scammer, scammer_exists, update_scammer_telegram_id,
    list_scammers, count_scammers, get_scammer_by_id,
    update_scammer_fields, remove_scammer, EDITABLE_FIELDS,
    add_trusted_reporter, remove_trusted_reporter,
)
from bot.services.admins import (
    list_all_admins, add_admin, remove_admin, is_owner,
    resolve_protected_role,
)
from bot.services.audit import audit
from bot.services.broadcaster import broadcast_scammer
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

# Pseudo-actor for audit log entries created from the web panel (no Telegram user).
_WEB_ACTOR = SimpleNamespace(id=0, username="web-admin")

_ROLE_LABEL = {"owner": "👑 Owner", "env": "🔧 Admin (.env)", "db": "🛠 Admin"}

_ACTION_COLOR = {
    "approve": "#16a34a", "addid": "#16a34a", "quickadd": "#16a34a",
    "reject":  "#dc2626", "remove": "#dc2626", "removeadmin": "#dc2626",
    "removetrusted": "#dc2626",
    "edit":    "#d97706",
    "addadmin": "#7c3aed", "addtrusted": "#7c3aed",
}

_SEV_ICON  = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_SEV_COLOR = {"high": "#dc2626", "medium": "#d97706", "low": "#16a34a"}

_ERR_MSG = {
    "invalid":   "Invalid input — check the fields and try again.",
    "dup":       "Already listed as a scammer.",
    "protected": "That account belongs to the bot owner/an admin — can't be added.",
    "notfound":  "Could not resolve that user on Telegram.",
    "already":   "That user is already the owner.",
}

_CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
  background:#0f1115;color:#e6e8eb}
.wrap{max-width:1200px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 12px}
.sub{color:#9aa3ad;font-size:13px;margin-bottom:20px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.card{background:#171a21;border:1px solid #232833;border-radius:10px;
  padding:14px 18px;min-width:130px;text-decoration:none;color:inherit;display:block}
.card .n{font-size:24px;font-weight:700}
.card .l{color:#9aa3ad;font-size:12px;margin-top:2px}
h2{font-size:15px;margin:24px 0 8px;color:#c7ccd3}
table{width:100%;border-collapse:collapse;background:#171a21;
  border:1px solid #232833;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;font-size:13px;border-bottom:1px solid #232833;vertical-align:middle}
th{background:#1c2029;color:#9aa3ad;font-weight:600}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;color:#fff;
  font-size:11px;font-weight:600}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#aab2bd}
.muted{color:#6b7280}
.nav{display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap}
.navlink{color:#9aa3ad;text-decoration:none;padding:6px 14px;border-radius:8px;
  font-size:13px;font-weight:600;background:#171a21;border:1px solid #232833}
.navlink.active{color:#fff;background:#2563eb;border-color:#2563eb}
.btn{display:inline-block;border:none;border-radius:6px;padding:5px 10px;
  font-size:12px;font-weight:600;color:#fff;cursor:pointer;margin:2px}
.btn-red{background:#dc2626}.btn-green{background:#16a34a}.btn-orange{background:#d97706}
.btn-blue{background:#2563eb}.btn-gray{background:#475569}.btn-purple{background:#7c3aed}
.inline{display:inline-block;margin:0}
.actions{white-space:nowrap}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px}
.flash.err{background:#3f1d1d;border:1px solid #7f1d1d;color:#fca5a5}
.formcard{background:#171a21;border:1px solid #232833;border-radius:10px;
  padding:16px;margin-bottom:20px;max-width:480px}
.formcard label{display:block;font-size:12px;color:#9aa3ad;margin:8px 0 4px}
.formcard input,.formcard select,.formcard textarea{width:100%;background:#0f1115;color:#e6e8eb;
  border:1px solid #232833;border-radius:6px;padding:8px;font-size:13px;font-family:inherit;resize:vertical}
.formcard .btn{margin-top:12px}
.pager{margin-top:14px;display:flex;gap:8px}
.pager a{color:#9aa3ad;text-decoration:none;padding:6px 12px;border:1px solid #232833;
  border-radius:6px;background:#171a21}
"""

_NAV = [
    ("/",         "📊 Dashboard"),
    ("/pending",  "📨 Pending"),
    ("/scammers", "🚫 Scammers"),
    ("/admins",   "👥 Admins"),
]


# ── Auth ─────────────────────────────────────────────────────────────────────

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


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _layout(title: str, active: str, body: str, refresh: bool = False) -> str:
    nav_html = "".join(
        f'<a href="{href}" class="navlink{" active" if href == active else ""}">{label}</a>'
        for href, label in _NAV
    )
    refresh_tag = "<meta http-equiv='refresh' content='30'>" if refresh else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{refresh_tag}"
        f"<title>{_esc(title)} — Scammer Bot Admin</title>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>"
        f"<div class='nav'>{nav_html}</div>"
        f"{body}"
        "</div></body></html>"
    )


def _flash(request: web.Request) -> str:
    err = request.query.get("err")
    if not err:
        return ""
    return f"<div class='flash err'>{_esc(_ERR_MSG.get(err, err))}</div>"


# ── Dashboard ────────────────────────────────────────────────────────────────

async def _index(request: web.Request) -> web.Response:
    actions  = await recent_admin_actions(300)
    counts   = await admin_action_counts()
    admins   = await list_all_admins()
    trusted  = await list_trusted_reporters()
    total    = await count_scammers()
    pending  = await count_reports("pending")

    cards = (
        f'<a class="card" href="/scammers"><div class="n">{total}</div><div class="l">Scammers listed</div></a>'
        f'<a class="card" href="/pending"><div class="n">{pending}</div><div class="l">Pending reports</div></a>'
        f'<a class="card" href="/admins"><div class="n">{len(admins)}</div><div class="l">Admins</div></a>'
        f'<a class="card" href="/admins"><div class="n">{len(trusted)}</div><div class="l">Trusted reporters</div></a>'
    )

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

    body = (
        "<h1>🛡️ Scammer Bot — Admin Dashboard</h1>"
        "<div class='sub'>Auto-refreshes every 30s · click the cards to manage</div>"
        f"<div class='cards'>{cards}</div>"
        f"{admins_html}{actions_html}"
    )
    return web.Response(text=_layout("Dashboard", "/", body, refresh=True), content_type="text/html")


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ── Pending reports ──────────────────────────────────────────────────────────

async def _pending_page(request: web.Request) -> web.Response:
    reports = await list_pending_reports()

    if not reports:
        body = _flash(request) + "<h1>📨 Pending Reports</h1><p class='muted'>No pending reports. 🎉</p>"
        return web.Response(text=_layout("Pending Reports", "/pending", body), content_type="text/html")

    rows = []
    for r in reports:
        target   = f"@{r['target_username']}" if r.get("target_username") else (str(r["target_id"]) if r.get("target_id") else "—")
        name     = r.get("target_full_name") or "—"
        reporter = f"@{r['reporter_username']}" if r.get("reporter_username") else str(r["reporter_id"])
        actions = (
            f"<form method=post action=/pending/approve class='inline'>"
            f"<input type=hidden name=report_id value='{r['id']}'>"
            f"<button name=severity value=high class='btn btn-red'>🔴 High</button>"
            f"<button name=severity value=medium class='btn btn-orange'>🟡 Med</button>"
            f"<button name=severity value=low class='btn btn-green'>🟢 Low</button>"
            f"</form>"
            f"<form method=post action=/pending/reject class='inline'>"
            f"<input type=hidden name=report_id value='{r['id']}'>"
            f"<button class='btn btn-gray'>❌ Reject</button>"
            f"</form>"
        )
        rows.append(
            "<tr>"
            f"<td>#{r['id']}</td>"
            f"<td>{_esc(target)}</td>"
            f"<td>{_esc(name)}</td>"
            f"<td>{_esc((r.get('reason') or '')[:80])}</td>"
            f"<td>{_esc((r.get('payment_info') or '—')[:40])}</td>"
            f"<td>{_esc((r.get('proof') or '—')[:40])}</td>"
            f"<td>{_esc(reporter)}</td>"
            f"<td>{_when(r.get('reported_at'))}</td>"
            f"<td class='actions'>{actions}</td>"
            "</tr>"
        )

    table = (
        "<table><tr><th>ID</th><th>Target</th><th>Name</th><th>Reason</th><th>Payment</th><th>Proof</th>"
        "<th>Reporter</th><th>When</th><th>Actions</th></tr>" + "".join(rows) + "</table>"
    )
    body = _flash(request) + f"<h1>📨 Pending Reports ({len(reports)})</h1>" + table
    return web.Response(text=_layout("Pending Reports", "/pending", body), content_type="text/html")


async def _do_approve(bot, report: dict, severity: str) -> int | None:
    """Approve a report — same effects as the ✅ button / /approve command.

    Returns the new scammer's ID, or None if the target was already listed
    (the report is auto-rejected as a duplicate instead, no new entry made).
    """
    from bot.handlers.callbacks import _kick_from_all_groups, _broadcast_resolution, _target_str

    dup = await scammer_exists(report.get("target_id"), report.get("target_username"))
    if dup:
        await update_report_status(report["id"], "rejected")
        await _broadcast_resolution(
            SimpleNamespace(bot=bot),
            actor="🌐 Web Panel",
            actor_id=0,
            headline=(
                f"♻️ <b>Submission #{report['id']} auto-rejected</b> — duplicate of Scammer #{dup['id']}\n"
                f"🎯 {_target_str(report)}"
            ),
        )
        _tgt = f"@{report['target_username']}" if report.get("target_username") else (str(report.get("target_id")) if report.get("target_id") else "—")
        await audit(_WEB_ACTOR, "auto_reject_dup", "report", report["id"], f"dup_of=#{dup['id']} target={_tgt} (via web)")
        return None

    scammer_id = await add_scammer(
        telegram_id   = report.get("target_id"),
        username      = report.get("target_username"),
        name          = report.get("target_full_name") or report.get("target_username") or "Unknown",
        reason        = report["reason"],
        proof         = report.get("proof"),
        added_by      = 0,
        severity      = severity,
        proof_file_id = report.get("proof_file_id"),
        payment_info  = report.get("payment_info"),
    )
    await update_report_status(report["id"], "approved")

    group_chat_id = report.get("group_chat_id")
    sev_icon = _SEV_ICON.get(severity, "🟡")
    if group_chat_id:
        uname = f"@{report['target_username']}" if report.get("target_username") else "—"
        tid   = f"<code>{report['target_id']}</code>" if report.get("target_id") else "—"
        try:
            await bot.send_message(
                group_chat_id,
                em(
                    f"✅ <b>Scammer Confirmed — #{scammer_id}</b>\n\n"
                    f"📝 Username : {uname}\n"
                    f"🔑 Tele ID  : {tid}\n"
                    f"{sev_icon} Severity  : {severity.capitalize()}\n"
                    f"⚠️ Reason   : {report['reason']}\n\n"
                    f"📋 Use /scammer_list to see the full list."
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not notify group %s: %s", group_chat_id, exc)

    await broadcast_scammer(
        bot, scammer_id, report.get("target_username"), report.get("target_id"),
        report["reason"], severity=severity, skip_group_id=group_chat_id,
        payment_info=report.get("payment_info"),
    )

    target_tg_id = report.get("target_id")
    if not target_tg_id and report.get("target_username"):
        try:
            chat = await bot.get_chat(f"@{report['target_username']}")
            target_tg_id = chat.id
            await update_scammer_telegram_id(scammer_id, chat.id, chat.username)
        except Exception as e:
            logger.warning("Could not resolve ID for @%s at kick time: %s", report.get("target_username"), e)

    if target_tg_id:
        await _kick_from_all_groups(
            bot, target_tg_id,
            username=report.get("target_username"),
            reason=report["reason"],
            scammer_id=scammer_id,
        )

    await _broadcast_resolution(
        SimpleNamespace(bot=bot),
        actor="🌐 Web Panel",
        actor_id=0,
        headline=(
            f"✅ <b>Submission #{report['id']} approved</b> — {sev_icon} {severity.capitalize()}\n"
            f"🎯 {_target_str(report)} → Scammer #{scammer_id}"
        ),
    )

    _tgt = f"@{report['target_username']}" if report.get("target_username") else (str(report.get("target_id")) if report.get("target_id") else "—")
    await audit(_WEB_ACTOR, "approve", "scammer", scammer_id,
                f"sev={severity} target={_tgt} report#{report['id']} (via web)")
    return scammer_id


async def _pending_approve(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        rid = int(data.get("report_id", ""))
    except ValueError:
        raise web.HTTPFound("/pending")

    severity = (data.get("severity") or "medium").lower()
    if severity not in ("high", "medium", "low"):
        severity = "medium"

    report = await get_report(rid)
    if not report or report["status"] != "pending":
        raise web.HTTPFound("/pending")

    scammer_id = await _do_approve(request.app["bot"], report, severity)
    if scammer_id is None:
        raise web.HTTPFound("/pending?err=dup")
    raise web.HTTPFound("/pending")


async def _pending_reject(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        rid = int(data.get("report_id", ""))
    except ValueError:
        raise web.HTTPFound("/pending")

    report = await get_report(rid)
    if not report or report["status"] != "pending":
        raise web.HTTPFound("/pending")

    await update_report_status(rid, "rejected")

    from bot.handlers.callbacks import _broadcast_resolution, _target_str
    await _broadcast_resolution(
        SimpleNamespace(bot=request.app["bot"]),
        actor="🌐 Web Panel",
        actor_id=0,
        headline=f"❌ <b>Submission #{rid} rejected</b>\n🎯 {_target_str(report)}",
    )

    _tgt = f"@{report['target_username']}" if report.get("target_username") else (str(report.get("target_id")) if report.get("target_id") else "—")
    await audit(_WEB_ACTOR, "reject", "report", rid, f"target={_tgt} (via web)")
    raise web.HTTPFound("/pending")


# ── Scammers ─────────────────────────────────────────────────────────────────

async def _scammers_page(request: web.Request) -> web.Response:
    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 20

    entries = await list_scammers(limit=per_page, offset=(page - 1) * per_page)
    total   = await count_scammers()
    total_pages = max(1, (total + per_page - 1) // per_page)

    rows = []
    for e in entries:
        uname = f"@{e['username']}" if e.get("username") else "—"
        tid   = str(e["telegram_id"]) if e.get("telegram_id") else "—"
        sev   = e.get("severity", "medium")
        sev_badge = (
            f"<span class='badge' style='background:{_SEV_COLOR.get(sev, '#475569')}'>"
            f"{_SEV_ICON.get(sev, '🟡')} {_esc(sev)}</span>"
        )
        edit_link = f"<a class='btn btn-gray' href='/scammers/{e['id']}/edit'>✏️ Edit</a>"
        remove_form = (
            f"<form method=post action=/scammers/remove class='inline' "
            f"onsubmit=\"return confirm('Remove #{e['id']}?')\">"
            f"<input type=hidden name=scammer_id value='{e['id']}'>"
            f"<button class='btn btn-red'>🗑 Remove</button>"
            f"</form>"
        )
        rows.append(
            "<tr>"
            f"<td>#{e['id']}</td>"
            f"<td>{_esc(e.get('name') or '—')}</td>"
            f"<td>{_esc(uname)}</td>"
            f"<td class='mono'>{_esc(tid)}</td>"
            f"<td>{sev_badge}</td>"
            f"<td>{_esc((e.get('reason') or '')[:60])}</td>"
            f"<td>{_esc((e.get('payment_info') or '—')[:40])}</td>"
            f"<td>{_when(e.get('added_at'))}</td>"
            f"<td class='actions'>{edit_link}{remove_form}</td>"
            "</tr>"
        )

    table = (
        "<table><tr><th>#</th><th>Name</th><th>Username</th><th>Telegram ID</th>"
        "<th>Severity</th><th>Reason</th><th>Payment</th><th>Added</th><th>Actions</th></tr>"
        + ("".join(rows) if rows else "<tr><td colspan=9 class='muted'>No scammers yet.</td></tr>")
        + "</table>"
    )

    pager = "<div class='pager'>"
    if page > 1:
        pager += f"<a href='/scammers?page={page-1}'>← Prev</a>"
    pager += f"<span class='muted'>Page {page} / {total_pages}</span>"
    if page < total_pages:
        pager += f"<a href='/scammers?page={page+1}'>Next →</a>"
    pager += "</div>"

    body = (
        _flash(request)
        + f"<h1>🚫 Scammers ({total})</h1>"
        + "<p><a class='btn btn-blue' href='/scammers/add'>➕ Add Scammer</a></p>"
        + table + pager
    )
    return web.Response(text=_layout("Scammers", "/scammers", body), content_type="text/html")


async def _scammers_edit_get(request: web.Request) -> web.Response:
    try:
        scammer_id = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPFound("/scammers")

    entry = await get_scammer_by_id(scammer_id)
    if not entry:
        raise web.HTTPFound("/scammers")

    sev = (entry.get("severity") or "medium").lower()

    def sev_option(value: str, label: str) -> str:
        selected = " selected" if sev == value else ""
        return f"<option value='{value}'{selected}>{label}</option>"

    body = (
        _flash(request)
        + f"<h1>✏️ Edit Scammer #{entry['id']}</h1>"
        + f"<form method=post action='/scammers/{entry['id']}/edit' class='formcard'>"
        + "<label>Telegram ID</label>"
        + f"<input type=text name=telegram_id value='{_esc(entry.get('telegram_id') or '')}' placeholder='123456789'>"
        + "<label>Username (without @)</label>"
        + f"<input type=text name=username value='{_esc(entry.get('username') or '')}' placeholder='username'>"
        + "<label>Name</label>"
        + f"<input type=text name=name value='{_esc(entry.get('name') or '')}' placeholder='Display name'>"
        + "<label>Reason</label>"
        + f"<textarea name=reason rows=2 required>{_esc(entry.get('reason') or '')}</textarea>"
        + "<label>Severity</label>"
        + "<select name=severity>"
        + sev_option("high", "🔴 High") + sev_option("medium", "🟡 Medium") + sev_option("low", "🟢 Low")
        + "</select>"
        + "<label>Payment info (Binance ID / UPI / wallet address)</label>"
        + f"<input type=text name=payment_info value='{_esc(entry.get('payment_info') or '')}' placeholder='e.g. Binance ID 123456789'>"
        + "<label>Proof</label>"
        + f"<textarea name=proof rows=2>{_esc(entry.get('proof') or '')}</textarea>"
        + "<label>Notes</label>"
        + f"<textarea name=notes rows=2>{_esc(entry.get('notes') or '')}</textarea>"
        + "<button class='btn btn-blue'>💾 Save Changes</button>"
        + " <a class='btn btn-gray' href='/scammers'>Cancel</a>"
        + "</form>"
    )
    return web.Response(text=_layout(f"Edit Scammer #{entry['id']}", "/scammers", body), content_type="text/html")


async def _scammers_edit_post(request: web.Request) -> web.Response:
    try:
        scammer_id = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPFound("/scammers")

    entry = await get_scammer_by_id(scammer_id)
    if not entry:
        raise web.HTTPFound("/scammers")

    data = await request.post()

    raw_id   = (data.get("telegram_id") or "").strip().lstrip("@")
    username = (data.get("username") or "").strip().lstrip("@")
    name     = (data.get("name") or "").strip()
    reason   = (data.get("reason") or "").strip()
    severity = (data.get("severity") or "medium").strip().lower()
    payment  = (data.get("payment_info") or "").strip()
    proof    = (data.get("proof") or "").strip()
    notes    = (data.get("notes") or "").strip()

    if raw_id and not raw_id.lstrip("-").isdigit():
        raise web.HTTPFound(f"/scammers/{scammer_id}/edit?err=invalid")
    if not reason:
        raise web.HTTPFound(f"/scammers/{scammer_id}/edit?err=invalid")
    if severity not in ("high", "medium", "low"):
        severity = "medium"

    fields = {
        "telegram_id":  int(raw_id) if raw_id else None,
        "username":     username or None,
        "name":         name or "Unknown",
        "reason":       reason,
        "severity":     severity,
        "payment_info": payment or None,
        "proof":        proof or None,
        "notes":        notes or None,
    }

    changes = []
    for field, new_value in fields.items():
        column    = EDITABLE_FIELDS[field]
        old_value = entry.get(column)
        if old_value != new_value:
            old_disp = old_value if old_value not in (None, "") else "—"
            new_disp = new_value if new_value not in (None, "") else "—"
            changes.append(f"{field}: {old_disp} → {new_disp}")

    if changes:
        await update_scammer_fields(scammer_id, fields)
        await audit(_WEB_ACTOR, "edit", "scammer", scammer_id, "; ".join(changes) + " (via web)")

    raise web.HTTPFound("/scammers")


async def _scammers_remove(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        scammer_id = int(data.get("scammer_id", ""))
    except ValueError:
        raise web.HTTPFound("/scammers")

    entry = await get_scammer_by_id(scammer_id)
    if entry:
        ok = await remove_scammer(scammer_id)
        if ok:
            uname = f"@{entry['username']}" if entry.get("username") else f"ID {entry.get('telegram_id') or '—'}"
            await audit(_WEB_ACTOR, "remove", "scammer", scammer_id, f"{uname} (via web)")
    raise web.HTTPFound("/scammers")


async def _scammers_add_get(request: web.Request) -> web.Response:
    body = (
        _flash(request)
        + "<h1>➕ Add Scammer</h1>"
        + "<form method=post action=/scammers/add class='formcard'>"
        + "<label>Telegram ID (optional if username given)</label>"
        + "<input type=text name=telegram_id placeholder='123456789'>"
        + "<label>Username (optional, without @)</label>"
        + "<input type=text name=username placeholder='username'>"
        + "<label>Reason</label>"
        + "<input type=text name=reason placeholder='Fraud / scam reason' required>"
        + "<label>Payment info (Binance ID / UPI / wallet address, optional)</label>"
        + "<input type=text name=payment_info placeholder='e.g. Binance ID 123456789'>"
        + "<label>Severity</label>"
        + "<select name=severity>"
        + "<option value=medium selected>🟡 Medium</option>"
        + "<option value=high>🔴 High</option>"
        + "<option value=low>🟢 Low</option>"
        + "</select>"
        + "<button class='btn btn-blue'>Add &amp; Broadcast</button>"
        + "</form>"
        + "<p class='muted'>Bot will auto-fetch username/name from Telegram, kick the user "
        + "from all groups, and broadcast the alert to every group — same as /addid.</p>"
    )
    return web.Response(text=_layout("Add Scammer", "/scammers", body), content_type="text/html")


async def _scammers_add_post(request: web.Request) -> web.Response:
    data = await request.post()
    raw_id       = (data.get("telegram_id") or "").strip().lstrip("@")
    raw_uname    = (data.get("username") or "").strip().lstrip("@")
    reason       = (data.get("reason") or "").strip() or "No reason provided"
    payment_info = (data.get("payment_info") or "").strip() or None
    severity     = (data.get("severity") or "medium").lower()
    if severity not in ("high", "medium", "low"):
        severity = "medium"

    telegram_id = int(raw_id) if raw_id.isdigit() else None
    username    = raw_uname or None

    if not telegram_id and not username:
        raise web.HTTPFound("/scammers/add?err=invalid")

    bot = request.app["bot"]

    role = await resolve_protected_role(telegram_id, username, bot=bot)
    if role:
        raise web.HTTPFound("/scammers/add?err=protected")

    dup = await scammer_exists(telegram_id, username)
    if dup:
        raise web.HTTPFound("/scammers/add?err=dup")

    full_name = "Unknown"
    if telegram_id:
        try:
            chat = await bot.get_chat(telegram_id)
            username  = chat.username or username
            full_name = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
        except TelegramError as e:
            logger.info("get_chat(%s) failed (will track by ID only): %s", telegram_id, e)
    elif username:
        try:
            chat = await bot.get_chat(f"@{username}")
            telegram_id = chat.id
            username    = chat.username or username
            full_name   = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
        except TelegramError as e:
            logger.info("get_chat(@%s) failed: %s", username, e)

    scammer_id = await add_scammer(
        telegram_id  = telegram_id,
        username     = username,
        name         = full_name,
        reason       = reason,
        proof        = None,
        added_by     = 0,
        severity     = severity,
        payment_info = payment_info,
    )

    from bot.handlers.callbacks import _kick_from_all_groups
    if telegram_id:
        await _kick_from_all_groups(
            bot, telegram_id,
            username=username,
            reason=reason,
            scammer_id=scammer_id,
        )

    await broadcast_scammer(bot, scammer_id, username, telegram_id, reason, severity=severity, payment_info=payment_info)

    uname_str = f"@{username}" if username else (str(telegram_id) if telegram_id else "—")
    await audit(_WEB_ACTOR, "addid", "scammer", scammer_id, f"{uname_str} (via web)")

    raise web.HTTPFound("/scammers")


# ── Admins & trusted reporters ──────────────────────────────────────────────

async def _admins_page(request: web.Request) -> web.Response:
    admins = await list_all_admins()
    arows = []
    for a in admins:
        tag = _ROLE_LABEL.get(a.get("source"), "Admin")
        if a.get("source") == "owner":
            remove_cell = "<span class='muted'>—</span>"
        elif a.get("source") == "env":
            remove_cell = "<span class='muted'>via .env</span>"
        else:
            remove_cell = (
                f"<form method=post action=/admins/remove class='inline' "
                f"onsubmit=\"return confirm('Remove admin {a['telegram_id']}?')\">"
                f"<input type=hidden name=telegram_id value='{a['telegram_id']}'>"
                f"<button class='btn btn-red'>Remove</button></form>"
            )
        arows.append(
            "<tr>"
            f"<td>{tag}</td>"
            f"<td class='mono'>{a['telegram_id']}</td>"
            f"<td>{remove_cell}</td>"
            "</tr>"
        )
    admins_table = (
        "<table><tr><th>Role</th><th>Telegram ID</th><th>Action</th></tr>"
        + "".join(arows) + "</table>"
    )
    add_admin_form = (
        "<form method=post action=/admins/add class='formcard'>"
        "<label>Telegram ID to make admin</label>"
        "<input type=text name=telegram_id placeholder='123456789' required>"
        "<button class='btn btn-purple'>➕ Add Admin</button>"
        "</form>"
    )

    trusted = await list_trusted_reporters()
    trows = []
    for t in trusted:
        uname = f"@{t['username']}" if t.get("username") else "—"
        trows.append(
            "<tr>"
            f"<td>{_esc(uname)}</td>"
            f"<td class='mono'>{t['user_id']}</td>"
            f"<td>{_esc(str(t.get('added_at',''))[:10])}</td>"
            f"<td><form method=post action=/trusted/remove class='inline' "
            f"onsubmit=\"return confirm('Remove trusted {t['user_id']}?')\">"
            f"<input type=hidden name=user_id value='{t['user_id']}'>"
            f"<button class='btn btn-red'>Remove</button></form></td>"
            "</tr>"
        )
    trusted_table = (
        "<table><tr><th>Username</th><th>ID</th><th>Added</th><th>Action</th></tr>"
        + ("".join(trows) if trows else "<tr><td colspan=4 class='muted'>None.</td></tr>")
        + "</table>"
    )
    add_trusted_form = (
        "<form method=post action=/trusted/add class='formcard'>"
        "<label>Username or Telegram ID</label>"
        "<input type=text name=target placeholder='@username or 123456789' required>"
        "<button class='btn btn-purple'>➕ Add Trusted Reporter</button>"
        "</form>"
    )

    body = (
        _flash(request)
        + "<h1>👥 Admins &amp; Trusted Reporters</h1>"
        + "<h2>Admins</h2>" + admins_table + add_admin_form
        + "<h2>Trusted reporters (auto-approve)</h2>" + trusted_table + add_trusted_form
    )
    return web.Response(text=_layout("Admins", "/admins", body), content_type="text/html")


async def _admins_add(request: web.Request) -> web.Response:
    data = await request.post()
    raw  = (data.get("telegram_id") or "").strip().lstrip("@")
    if not raw.lstrip("-").isdigit():
        raise web.HTTPFound("/admins?err=invalid")

    target_id = int(raw)
    if is_owner(target_id):
        raise web.HTTPFound("/admins?err=already")

    added = await add_admin(target_id, 0)
    if added:
        await audit(_WEB_ACTOR, "addadmin", "admin", target_id, "via web")
        try:
            await request.app["bot"].send_message(
                target_id,
                em("🎉 You've been made an <b>admin</b> of Scammer List Bot!\n\nSend /start to see admin tools."),
                parse_mode="HTML",
            )
        except Exception:
            pass
    raise web.HTTPFound("/admins")


async def _admins_remove(request: web.Request) -> web.Response:
    data = await request.post()
    raw  = (data.get("telegram_id") or "").strip().lstrip("@")
    if raw.lstrip("-").isdigit():
        target_id = int(raw)
        result = await remove_admin(target_id)
        if result == "removed":
            await audit(_WEB_ACTOR, "removeadmin", "admin", target_id, "via web")
    raise web.HTTPFound("/admins")


async def _trusted_add(request: web.Request) -> web.Response:
    data   = await request.post()
    target = (data.get("target") or "").strip()
    if not target:
        raise web.HTTPFound("/admins?err=invalid")

    bot = request.app["bot"]
    user_id: int | None  = None
    username: str | None = None

    if target.lstrip("@").isdigit():
        user_id = int(target.lstrip("@"))
    else:
        username = target.lstrip("@")
        try:
            chat     = await bot.get_chat(f"@{username}")
            user_id  = chat.id
            username = chat.username or username
        except TelegramError:
            raise web.HTTPFound("/admins?err=notfound")

    await add_trusted_reporter(user_id, username, 0)
    uname_display = f"@{username}" if username else str(user_id)
    await audit(_WEB_ACTOR, "addtrusted", "user", user_id, f"{uname_display} (via web)")
    raise web.HTTPFound("/admins")


async def _trusted_remove(request: web.Request) -> web.Response:
    data = await request.post()
    raw  = (data.get("user_id") or "").strip()
    if raw.isdigit():
        user_id   = int(raw)
        reporters = await list_trusted_reporters()
        match     = next((r for r in reporters if r["user_id"] == user_id), None)
        ok = await remove_trusted_reporter(user_id)
        if ok:
            uname = f"@{match['username']}" if match and match.get("username") else str(user_id)
            await audit(_WEB_ACTOR, "removetrusted", "user", user_id, f"{uname} (via web)")
    raise web.HTTPFound("/admins")


# ── Entry point ──────────────────────────────────────────────────────────────

async def run_web_admin(bot) -> None:
    """Background task — serves the panel if WEB_ADMIN_PORT + WEB_ADMIN_PASS set."""
    port = int(os.getenv("WEB_ADMIN_PORT", "0") or "0")
    if not port or not os.getenv("WEB_ADMIN_PASS"):
        logger.info("Web admin disabled (set WEB_ADMIN_PORT + WEB_ADMIN_PASS to enable)")
        return

    app = web.Application(middlewares=[_auth_mw])
    app["bot"] = bot

    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)

    app.router.add_get("/pending", _pending_page)
    app.router.add_post("/pending/approve", _pending_approve)
    app.router.add_post("/pending/reject", _pending_reject)

    app.router.add_get("/scammers", _scammers_page)
    app.router.add_get("/scammers/add", _scammers_add_get)
    app.router.add_post("/scammers/add", _scammers_add_post)
    app.router.add_get("/scammers/{id}/edit", _scammers_edit_get)
    app.router.add_post("/scammers/{id}/edit", _scammers_edit_post)
    app.router.add_post("/scammers/remove", _scammers_remove)

    app.router.add_get("/admins", _admins_page)
    app.router.add_post("/admins/add", _admins_add)
    app.router.add_post("/admins/remove", _admins_remove)
    app.router.add_post("/trusted/add", _trusted_add)
    app.router.add_post("/trusted/remove", _trusted_remove)

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
