# Userbot Member-List Harvester

A read-only helper that massively widens the bot's `id → username` coverage.

## Why this exists

The Telegram **Bot** API has two hard limits:

- It **cannot** list a group's members.
- It **cannot** turn an arbitrary numeric user-id into a username.

So when an admin adds a scammer by id with `/addid <id>`, the username/name
can only be filled in if the bot has already *seen* that person (they started
the bot, or sent a message / joined a group the bot is in).

A regular **user** account (a "userbot", driven by Telethon) *can* read full
member lists. This harvester logs in as such an account, scrapes
`{id, username, name}` for every member of every group it's in, and writes it
into the shared `bot_users` table. The main bot then uses that table as a
fallback — so `/addid <id>` and `/refreshusername` can resolve people the bot
itself has never interacted with.

**It only reads.** It never sends messages, never adds members. That keeps the
ban-risk low — but it is still a real account doing automation, so read the
safety notes below.

---

## What it does NOT do

- It can't resolve an id that **no group it's in** has ever contained. Coverage
  goes up a lot, but never to 100%.
- It won't make you immune to Telegram's rules. Scrape gently (defaults already
  do this).

---

## Step 1 — Get a phone number

Use a **spare / secondary number**, never your personal account. If Telegram
ever limits the account for automation, you don't want it to be your main one.
A second SIM or a number you don't mind losing is ideal.

## Step 2 — Get API credentials (API ID + API hash)

1. Open <https://my.telegram.org> in a browser.
2. Log in with the **spare phone number** (Telegram sends a login code to that
   account — read it in the Telegram app).
3. Click **"API development tools"**.
4. Fill the short form:
   - **App title:** anything, e.g. `scammer-harvester`
   - **Short name:** anything, e.g. `harvester`
   - Platform / URL: leave default / blank.
5. Click **Create application**.
6. You'll now see **`App api_id`** (a number) and **`App api_hash`** (a long
   hex string). Copy both. *Keep the api_hash secret — treat it like a
   password.*

## Step 3 — Put them in `.env`

On the server, edit `/home/botuser/scammer-list-bot/.env` and fill:

```ini
USERBOT_API_ID=1234567
USERBOT_API_HASH=0123456789abcdef0123456789abcdef
USERBOT_SESSION=userbot
USERBOT_SCRAPE_INTERVAL_HOURS=12
USERBOT_PER_REQUEST_DELAY=3
```

## Step 4 — Install Telethon

```bash
cd /home/botuser/scammer-list-bot
source venv/bin/activate
pip install -r requirements.txt      # installs telethon
```

## Step 5 — First login (interactive, ONE time)

This creates the session file so future runs need no password.

```bash
cd /home/botuser/scammer-list-bot
venv/bin/python -m userbot.harvester
```

It will ask for:
- the **phone number** (with country code, e.g. `+91...`),
- the **login code** Telegram sends to that account,
- and your **2FA password** if the account has one.

Once you see `Userbot logged in as ...` and it starts a harvest pass, it's
working. Press `Ctrl+C` to stop — a `userbot.session` file now exists.

## Step 6 — Join the groups you want covered

Log into the **same account** in a normal Telegram app and **join the groups**
where the scammers are active. The harvester only scrapes groups the account
is a member of. (You can keep joining more over time — they'll be picked up on
the next pass.)

## Step 7 — Run it as a service (head-less, auto-restart)

```bash
sudo cp deploy/userbot.service /etc/systemd/system/userbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now userbot
sudo journalctl -u userbot -f          # watch it work
```

That's it. The bot's `/addid` and `/refreshusername` automatically benefit —
no bot changes or restart needed.

---

## Safety / staying un-banned

- **Spare number only.** Never your personal account.
- **Age the account.** Don't scrape the day you create it. Use it like a normal
  person for a week or two first — old accounts are far less suspicious.
- **Go slow.** The defaults wait between groups and let Telethon back off on
  `FloodWait`. Don't lower `USERBOT_PER_REQUEST_DELAY` or join hundreds of
  groups in a day.
- **Read-only.** This tool never messages or adds anyone; don't bolt that on.
- **Telegram Premium** (optional) raises some limits and gets limited less.
- If the account gets limited: stop the service for a day or two, or switch to
  a new number (`rm userbot.session`, redo Step 5).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `USERBOT_API_ID / API_HASH not set` | Fill them in `.env` (Step 3). |
| `not logged in ... no terminal` | Run Step 5 in a real shell first. |
| A group logs `members not readable` | Normal for some channels (admin-only member list) — it's skipped. |
| `FloodWait Ns` in logs | Expected; it sleeps and continues. Raise the delay if frequent. |
