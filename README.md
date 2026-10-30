# 🗝️ Competitive Escape Room: a multiplayer Telegram bot

Two sides race through **seven locked rooms**. Each room holds one puzzle, and solving it unlocks the next. Both sides get the same puzzles in the same order. The first side to solve Room 7 escapes and wins.

* **1v1:** two players, each in their own private chat with the bot.
* **2v2:** two teams of two. Each team plays in its **own** Telegram group, and teammates share one progression.

The project is built with Python 3.11+, python-telegram-bot v21/v22 (async), SQLite and python-dotenv. Puzzles are stored as JSON.

---

## Contents

1. [Features](#features)
2. [Quick start](#quick-start)
3. [Creating the bot with BotFather](#1-create-the-bot-with-botfather)
4. [Group permissions and privacy mode](#group-permissions-and-privacy-mode)
5. [How players use it](#how-players-use-it)
6. [Adding or replacing puzzles](#adding-or-replacing-puzzles)
7. [Architecture](#architecture)
8. [Database schema and game-state model](#database-schema-and-game-state-model)
9. [Reliability, security and fairness](#reliability-security-and-fairness)
10. [Testing](#testing)
11. [Deployment](#deployment)
12. [Backups, restarts and updates](#backups-restarts-and-updates)
13. [Known Telegram limitations](#known-telegram-limitations)

---

## Features

* Inline-keyboard main menu: **Create Match · Join Match · How to Play · Help**.
* 1v1 and 2v2 modes. Join codes are generated with `secrets`. Invitations use Telegram deep links: `?start=` for 1v1 and `?startgroup=` for 2v2, which adds the bot to a group.
* The match starts automatically once every seat is filled and both 2v2 groups are linked. The start time is recorded once, and Room 1 goes to both sides at the same time.
* A pool of 21 original puzzles, three for each room, from Easy to Expert: number locks, riddles, ciphers (Caesar, Atbash, shift), word puzzles, logic deductions, multi-step codes (binary, phone keypad) and multi-clue finales. Each match draws one puzzle per room at random, both sides get the identical set, and a player's next match avoids the puzzles from their previous one. Each puzzle's answer and uniqueness is checked by the tests.
* Answer checking ignores case, spacing, punctuation, accents and a leading article, and accepts several answers per room. It uses **no fuzzy matching**.
* Hints: each side has **3 paid hints per game**, and hints within a room are progressive (1 → 2 → 3). Each paid hint gives the opponent a **free hint for the room they are in**. Free hints are tracked separately.
* Real-time opponent alerts ("Your opponent just reached Room 4 🔓"). They reveal progress and nothing else.
* The winner is decided atomically, so a match can never have two winners. Results are stored permanently.
* Forfeits ask for confirmation. In 2v2, **if either teammate quits, the whole team forfeits.**
* Any number of matches can run at the same time, fully isolated from each other.
* Restarts are safe. All state is in SQLite, and a transactional outbox prevents duplicate puzzle messages.
* Duplicate Telegram updates are ignored. Guesses and join attempts are rate-limited. Supergroup migration is handled.

---

## Quick start

```bash
git clone <this repo> escape_room_bot && cd escape_room_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then paste your token into .env
python bot.py --init-db       # creates data/escape_room.db and validates puzzles.json
python bot.py                 # starts the bot (long polling)
```

To run the tests: `pytest`

### 1. Create the bot with BotFather

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a display name, for example *Escape Room*, and a username ending in `bot`, for example `MyEscapeRoomBot`.
3. BotFather replies with a token such as `123456789:AAH...`. **This token is a password.** Put it only in `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:AAH...
   ```
   `.env` is in `.gitignore`. Never commit it. If the token ever leaks, send `/revoke` to BotFather.
4. Recommended BotFather settings (`/mybots` → your bot → *Bot Settings*):
   * **Allow Groups?** → **Turn groups ON**. 2v2 needs this. It is on by default.
   * **Group Privacy** → leave it **ON** (the default). See the next section.
   * You don't need to set the command list. The bot registers it at startup with `setMyCommands`.

### 2. Configure `.env`

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Token from BotFather |
| `DATABASE_PATH` | `data/escape_room.db` | SQLite file. **Must be on persistent storage.** |
| `PUZZLES_PATH` | `puzzles/puzzles.json` | Puzzle file |
| `ASSETS_DIR` | `assets/puzzle_images` | Folder for puzzle images |
| `LOBBY_TTL_HOURS` | `24` | Unstarted lobbies (and their codes) expire after this |
| `ANSWER_RATE_LIMIT_PER_MINUTE` | `15` | Guesses per side per minute (stops brute-forcing the keypads) |
| `JOIN_FAILURE_LIMIT` | `10` | Invalid join codes per user per 10 minutes |
| `WEBHOOK_URL`, `WEBHOOK_SECRET`, `PORT` | *(empty)* | Set `WEBHOOK_URL` to switch from polling to webhook mode |
| `LOG_LEVEL` | `INFO` | Logging level |

### 3. Initialise the database

`python bot.py --init-db` (or `python database.py`) creates the SQLite file and runs any pending migrations. It also validates `puzzles.json`. It does not contact Telegram. The bot runs migrations again on every start, so this step is optional but useful as a check.

---

## The room screen (Telegram Mini App)

When the bot runs in webhook mode (for example on PythonAnywhere), players can also play in a visual room instead of the chat. Tapping **🚪 Enter the room** opens a full-screen page inside Telegram:

* each room is drawn with its own colour and a central object that matches the puzzle (padlock, clock, chest, old phone, blinking lamps and so on),
* tap the object, or **Inspect the puzzle**, to read the puzzle, type an answer and use hints,
* a race track shows your progress and your opponent's, plus a live timer,
* when you solve a room, the door swings open and the next room loads.

When a chat can open the room screen, the bot no longer posts the puzzle text there. The chat gets a short "Room N is ready" message with the **Enter the room** button, and the puzzle is revealed inside the room. Chats without the button (for example a 2v2 group without a direct link) still get the full puzzle as text, as before. Typed answers in the chat keep working either way.

It's the same game: answers and hints from the room screen go through the same rules as chat commands, so free hints, opponent alerts and winner detection all still apply. Players can switch between the room screen and the chat at any time.

**Where the button appears**

* In the bot's private chat: on every room message, in the main menu, under `/status`, and as the **Play** button next to the message box.
* In 2v2 team groups, Telegram only allows Mini App buttons through a direct link. To get one, send `/newapp` to @BotFather, choose your bot, and set the URL to `https://<username>.pythonanywhere.com/app` and a short name such as `room`. Then run `.venv/bin/python pythonanywhere_setup.py --miniapp-link https://t.me/<YourBot>/room` and press **Reload**. Without the link, team players can still open the room from the **Play** button in their private chat with the bot.
* In 2v2, every guess made in the room screen is also posted to the team group, so teammates still see each other's guesses.

**Security:** every request from the room screen carries Telegram's signed login data (`initData`). The server checks the signature with the bot token and rejects anything older than 24 hours, so nobody can pretend to be another player or read the other side's puzzles. The screen only shows the opponent's room number.

**Limits:** the room screen checks for updates every 4 seconds, so opponent progress shows there with a short delay. Chat notifications still arrive instantly. Polling mode (`python bot.py`) has no web server, so the room screen needs webhook hosting.

## Group permissions and privacy mode

**Keep privacy mode ON and don't make the bot an admin.** The bot only needs to **read commands** and **send messages and photos** in the team groups.

How this works:

* In 2v2, answers must be sent as `/answer <guess>`. Ordinary group chat is never treated as an answer, so teammates can talk freely.
* With privacy mode on, Telegram delivers these to the bot in a group: commands addressed to it, replies to its messages, and service messages such as members joining or the group being upgraded. It does **not** deliver ordinary chat, which the bot has no reason to read.
* **Adding a bot to a group does not let it read every message.** That's intended here.
* **If another bot is in the same group**, Telegram may not deliver bare commands like `/answer` to this bot. Players should then use `/answer@YourBotName <guess>`. The simplest fix is to keep each team group to the two teammates and this bot.
* If you turn privacy mode off, or make the bot an admin, it receives every group message. The game ignores them, but it's an unnecessary privacy cost. After changing privacy mode, remove the bot from the group and add it back so Telegram applies the change.
* Members must be allowed to send messages and the bot must be allowed to send messages and media (the defaults). The bot needs no admin rights.

---

## How players use it

### 1v1

1. Player 1 sends `/start` → **Create Match** → **1v1 Individual**.
2. The bot replies with a match ID, a join code and an invite link `https://t.me/<bot>?start=<CODE>`.
3. Player 2 opens the link (or presses Start and sends `/join CODE`).
4. The match starts immediately and both players receive Room 1 privately.
5. Play by typing the answer, or with `/answer <guess>`. `/hint`, `/status` and `/quit` are commands and are never treated as answers.

### 2v2: creating and linking the team groups

1. The creator sends `/start` → **Create Match** → **2v2 Team**. They get a **Team A code**, which they use themselves and give to their teammate, and a **Team B code**, which they give to the other team. The creator is on Team A.
2. **Each team creates its own Telegram group** containing only its two teammates. The two teams must use different groups.
3. **Each of the four players opens a private chat with the bot and presses Start once.** The bot can't register a group player it has never met privately.
4. Add the bot to each group. Either open the bot's `?startgroup=<CODE>` link, which adds the bot and links the group in one step, or add it manually.
5. In the group, **each teammate sends `/join CODE`** with their own team's code.
   * The first `/join` links the group to that team. From then on, that group can only serve that team and the team can only use that group.
   * Each `/join` registers the sender. A team can't have more than two players.
6. Once both teams have two players and a linked group, the match starts and Room 1 is posted in both groups.
7. In the group, either teammate can use `/answer <guess>`. Both see every guess, reply, hint and new room. The other team never does.

### Commands

| Command | Function |
|---|---|
| `/start` | Register and show the main menu. `/start CODE` (deep link) joins a match. |
| `/create` | Create a new match (private chat only) |
| `/join CODE` | Join a match. 1v1: in a private chat. 2v2: in your team group. |
| `/answer GUESS` | Submit an answer. In 1v1 you can also just type it. |
| `/hint` | Use one of your 3 paid hints. Your opponent gets a free hint. |
| `/status` | Match ID, mode, your room, opponent's room, hints, elapsed time |
| `/quit` | Leave a lobby, or forfeit an active match (asks for confirmation) |
| `/help` | Instructions |

---

## Puzzle bank and fair puzzle mixing

The game ships with a bank of **10,000 original puzzle sets of 7 puzzles each (70,000 puzzles)** in `puzzles/bank/bank.jsonl.gz`, plus the hand-written puzzles in `puzzles/puzzles.json`.

**How a match gets its puzzles.** When a match starts (once every player has joined), the bot builds one combination of 7 puzzles for that match:

1. It checks the puzzle history of **every** participant: both players in 1v1, all four in 2v2. Players are recognised by their Telegram account ID, so history follows them across devices, chats and the room screen.
2. For each room it ranks the puzzles of **all** sets: first those that **no** participant has seen, then those seen by the fewest participants, then the least-often seen, with random tie-breaks. Room N always draws from position N of the sets, which keeps the Easy → Expert progression while still mixing puzzles from different sets.
3. It never uses the same puzzle or the same answer twice in one match.
4. It never hands out an exact combination of 7 puzzles that was used before (`match_combinations`; order doesn't matter). If the preferred combination was used, it searches for the next-fairest unused one.
5. The combination is saved with the match. Every player in the match gets exactly these 7 puzzles, in the same order.

If a room has run out of puzzles nobody has seen, it falls back to the least-seen ones, so a match can always start.

**What counts as "seen".** A puzzle is added to a player's history when its room is delivered to that player's side. Rooms a side never reached don't count. Matches played before this feature are added to history by `python bot.py --init-db`.

**Loading the bank.** Run `python bot.py --init-db` (the PythonAnywhere setup script does this too). It imports the bank into the database in a few seconds, adds the database tables, and is safe to run again. Until the bank is loaded, the bot uses only `puzzles.json`, with the same fairness rules.

**The puzzles.** `puzzle_generator.py` builds the bank deterministically from a fixed seed (`python puzzle_generator.py` reproduces the file byte-for-byte). Each of the 7 positions has its own puzzle types:

| Room | Difficulty | Types |
|---|---|---|
| 1 | Easy | fact locks, arithmetic locks |
| 2 | Easy | number sequences (six kinds) |
| 3 | Medium | Caesar and Atbash ciphers |
| 4 | Medium | first-letter, numbered and last-letter word puzzles |
| 5 | Hard | ordering logic puzzles with a unique solution |
| 6 | Hard | number codes, reverse alphabet, binary lamps, phone keypad |
| 7 | Expert | multi-clue keypads, scattered-letter words |

Every puzzle is checked when it's generated, and the tests check all 70,000 again independently: codes and ciphers are decoded back to their answer, and logic puzzles are brute-forced to prove exactly one solution. Puzzles are unique by content. Word answers come from a fixed vocabulary, so the same answer word can appear in different puzzles.

## Adding or replacing puzzles

All puzzle content is in **`puzzles/puzzles.json`**. The Python code contains none. The file is a **pool**: list as many puzzles per room as you like, and each new match draws one per room at random.

```json
{
  "id": "padlock-triangle",
  "room": 1,
  "title": "The Rusty Padlock",
  "difficulty": "Easy",
  "question": "Puzzle text. Telegram HTML such as <b>bold</b> is allowed.",
  "image": null,
  "image_caption": null,
  "accepted_answers": ["369"],
  "hints": ["gentle clue", "more specific", "strong clue"],
  "solution": "369",
  "explanation": "Optional, shown nowhere in-game; for your own reference."
}
```

Rules, which are checked at startup by `puzzle_manager.py` and by `python bot.py --init-db`:

* Every room from **1 to 7** needs **at least one** puzzle. Add more puzzles with the same `room` number to give that room more variety.
* `id` is optional but recommended: a short unique name such as `"padlock-triangle"`. Without one, a puzzle gets `room<N>-<position>`, which changes if you reorder the file.
* Keep each room's options at a similar difficulty, so every match gets a fair progression.
* Each puzzle needs exactly **3 hints**, ordered from least to most revealing.
* `accepted_answers` must be a non-empty list. Matching ignores case, extra spaces, punctuation, accents and a leading *a/an/the*, but the letters and digits must otherwise match exactly. List every legitimate variant, for example `["iron", "iron chest"]`.
* `question` and `hints` use **Telegram HTML**. Allowed tags are `<b> <i> <u> <s> <code> <pre>`. Write a literal `<`, `>` or `&` as `&lt;`, `&gt;` or `&amp;`. If the HTML is broken, the bot falls back to plain text rather than withholding the puzzle.
* `image` is a file name inside `assets/puzzle_images/`, for example `"room4.png"`. The image is sent as a photo with `image_caption` right after the room's text. Paths outside that folder are rejected.

**Changes only affect new matches.** When a match is created, its seven drawn puzzles are copied into its database row. Matches already in progress keep the version they started with, and both sides of a match always see identical content. The bot notices when the file changes, so a restart isn't needed. If an edited file is invalid, the bot logs an error and keeps using the last valid version.

After editing, run `python bot.py --init-db` and `pytest tests/test_puzzles.py`. The puzzle tests check every *sample* puzzle's logic by `id`, so update or remove those specific checks if you replace the puzzles. The general checks (three hints, distinct answers, difficulty order) apply to any puzzles.

---

## Architecture

```
escape_room_bot/
├── bot.py               Telegram handlers, keyboards, startup recovery, polling/webhook
├── config.py            Settings from env/.env + fixed game-rule constants
├── database.py          SQLite connection, transactions, versioned migrations
├── state.py             Shared lookups: who is on which side, chat → team resolution
├── matchmaking.py       Users, match creation, join codes, 2v2 group linking, match start
├── game_engine.py       Answers, room progression, winner detection, forfeits, status, recovery
├── hint_manager.py      Paid hints + automatic free hints
├── answer_validator.py  Answer normalisation and comparison
├── puzzle_manager.py    Load/validate puzzles.json, per-match snapshots
├── puzzle_bank.py       Puzzle bank import, player history, fair per-match selection
├── puzzle_generator.py  Builds and verifies the 10,000-set bank
├── notifications.py     Transactional outbox + reliable Telegram delivery
├── messages.py          All user-facing text (HTML templates)
├── backup.py            Online SQLite backup
├── webhook_app.py       WSGI entry point for webhook hosting (PythonAnywhere) + Mini App routes
├── miniapp.py           Mini App backend: Telegram login check and room API
├── miniapp/index.html   The visual room screen (drawn with SVG, no images)
├── pythonanywhere_setup.py  One-command PythonAnywhere setup
├── puzzles/puzzles.json
├── assets/puzzle_images/
├── tests/               pytest suite (139 tests)
├── requirements.txt  .env.example  .gitignore  Dockerfile  docker-compose.yml
└── data/escape_room.db  (created at runtime, git-ignored)
```

The game services (`matchmaking`, `game_engine`, `hint_manager`) never import Telegram. They take plain IDs and return an `Outcome`: the reply text plus buttons. Anything that has to reach *other* chats, such as the next room, opponent alerts, free hints or results, is written to the **outbox** table in the same transaction. `notifications.Notifier` delivers it. This keeps the rules testable without Telegram, and state changes and their messages commit or roll back together.

---

## Database schema and game-state model

A **side** (table `teams`) is the unit of competition. In 1v1 it holds one player and in 2v2 it holds two. **All progression is stored on the side and never on individual players**, so teammates can't drift apart.

| Table | Purpose / key constraints |
|---|---|
| `users` | Telegram user ID, names, `private_chat_id` (set once the user presses Start privately) |
| `matches` | `match_id` (PK), mode, status `lobby → active → completed` (or `cancelled`/`expired`), creator, **puzzle snapshot**, created/expires/started/ended timestamps, winner, end reason |
| `teams` | One row per side (`UNIQUE(match_id,label)`), **unique join code**, capacity, delivery `chat_id`, `current_room` (8 = escaped), `paid_hints_used` (`CHECK 0–3`), `free_hints_received` |
| `team_members` | Doubles as the participant list. `PRIMARY KEY(team_id,user_id)` and **`UNIQUE(match_id,user_id)`**, so a user can't join twice or join both sides |
| `linked_groups` | Group ↔ team links. **Partial unique indexes** let a live group belong to one team only, and a team to one group only. Released when the match ends so groups can be reused. |
| `hint_progress` | `(team_id, room) → hints_revealed`. Paid and free hints share it, tracked separately per side |
| `hint_events` | Every hint: paid/free, room, index, who requested it, which side triggered a free hint |
| `room_completions` | `UNIQUE(team_id, room)`. Each room can be completed once per side. Timestamp, solver, elapsed time. |
| `guess_attempts` | Every guess, normalised, for auditing and rate limiting. Never shown to the opponent. |
| `match_results` | Permanent result: winner/loser, escaped or forfeit, duration, hints used and received, rooms reached, who forfeited |
| `outbox` | Pending, sent and failed messages with a **unique `dedupe_key`** |
| `processed_updates` | Telegram `update_id`s already handled (duplicate protection) |
| `join_failures` | Rate limiting for invalid join codes |
| `schema_migrations` | Applied migration versions |

**Migrations:** `database.MIGRATIONS` is an append-only list of `(version, name, [statements])`. To change the schema, append a new version. Never edit one that has already shipped. Migrations run automatically on startup, each in its own transaction.

---

## Reliability, security and fairness

**Atomicity and concurrency**

* Every action is one `BEGIN IMMEDIATE` SQLite transaction. SQLite allows only one writer at a time, so simultaneous answers and hint requests are processed one after another. This holds across threads and processes.
* Room advancement is a compare-and-set (`UPDATE … WHERE current_room = ?`), backed by `UNIQUE(team_id, room)`. When two teammates submit the correct answer at the same moment, only one advance happens and the other guess is checked against the next room.
* Winner detection: `UPDATE matches SET status='completed' … WHERE status='active'`. Only the first side to finish changes the row. The second side's transaction rolls back entirely and it is told the match has ended. The tests run this race with real threads on separate connections.
* The paid-hint allowance uses `WHERE paid_hints_used < 3` plus a `CHECK` constraint, so it can't be overspent even when requests run in parallel.

**Restart recovery**

* There is no game state held only in memory. The only in-memory caches are parsed puzzle snapshots, which are rebuilt from the database.
* At startup the bot expires stale lobbies and checks that every active side has its *current* room in the outbox. Because of `dedupe_key`, nothing that was already sent is sent again. It then delivers anything still pending. Match timers come from the stored `started_at`.
* Polling starts with `drop_pending_updates=False`, so messages sent while the bot was down are processed after it comes back.

**Delivery**

* Messages to different chats are sent in parallel, which is how both sides get Room 1 together. Messages to the same chat are sent in order.
* `RetryAfter` (flood control) waits and retries. Network errors and timeouts retry with exponential backoff and survive restarts. If the bot is blocked or removed, or a request is permanently rejected, the message is marked `failed` and logged, and other chats aren't held up. If a group is upgraded to a supergroup, the bot moves the team to the new chat ID.

**Security and privacy**

* Join codes are 8 characters from a 31-symbol alphabet (about 8.5×10¹¹ possibilities), generated by `secrets`. Invalid attempts are rate-limited, and codes stop working when the lobby starts or expires.
* Who a message comes from is always taken from the Telegram `from` user ID, never from message text. In a private chat, the context is the **sender's own** match. In a group, the context is the team **linked to that group**, and only if the sender is a **registered member** of it. Unrelated groups and people outside the team are refused.
* The opponent only ever receives "reached Room N", "used a hint" and the final result. Puzzles, guesses, hints and discussion stay on their own side. Future rooms are never sent early, and wrong answers never reveal the solution.
* All SQL uses bound parameters. Names typed by users are HTML-escaped. Chat IDs and internal errors are never shown to users. The generic error message is *"Something went wrong… your progress is safe."*
* The token is read from the environment and nowhere else, and the tests check that no token appears in the source.
* Guesses are rate-limited per side (default 15 per minute). Without this, a script could brute-force the 3- and 4-digit locks, which would be unfair to the other side.

---

## Testing

```bash
pip install -r requirements.txt
pytest -q          # 139 tests, ~2 seconds
```

The tests use a **real SQLite file**, the **real puzzle file** and the **real services**. `tests/test_e2e.py` also runs real Telegram `Update`s through the actual python-telegram-bot `Application` and handlers, with an in-process fake standing in for Telegram's HTTP API. What's covered:

| File | Covers |
|---|---|
| `test_matchmaking.py` | Create and join 1v1. Create 2v2, link two groups, register four players. No start until complete. Invalid, expired, rate-limited and duplicate joins. Both-teams and same-group rejection. Capacity limits. Leaving and cancelling a lobby. Reusing a group. |
| `test_game_engine.py` | Room 1 delivery. Correct, incorrect and alternative answers. Case and whitespace handling. Room order. No access to future rooms. Opponent alerts. Privacy. Full game with result and duration. No duplicate victory. Answer rate limit. Status. 1v1 and 2v2 forfeits. Team sync. Outsiders and unlinked groups refused. Four simultaneous matches. |
| `test_hints.py` | Deduction. Progressive order. Free hint for the opponent's *current* room. Free hints don't use up paid ones. Shared progression between free and paid hints. Exhaustion. 3 per game, not per room. No free hint for an escaped opponent or one who already has every hint for the room. No hints after the end. |
| `test_persistence.py` | Migrations. Restart mid-match (room, hints, hint progression, timers, group links). No duplicate puzzles after restart. Duplicate update IDs. DB error rolls back the whole action. **Simultaneous final answers (threads) → exactly one winner.** Simultaneous teammate answers → one advance. Parallel hint requests → never more than 3. |
| `test_notifications.py` | Parallel delivery. Order within a chat. Transient retry. `RetryAfter`. Blocked bot. Max attempts. Bad-HTML fallback. Supergroup migration. No re-send after restart. Pending messages delivered after restart. Images. |
| `test_security.py` | People outside the match. Cross-side chat IDs. HTML escaping. SQL injection strings. No chat IDs or answers in status. No solutions leaked. Token not hard-coded. `.env` ignored by git. |
| `test_puzzles.py` | File validation, plus a mechanical check of each sample puzzle: the cipher, the logic puzzle and the 4-digit code are brute-forced to prove a unique answer. |
| `test_bot.py`, `test_e2e.py` | Commands are never treated as answers. Group chat is never an answer. Deep links. Full 1v1 and 2v2 flows through PTB. Duplicate update. Forfeit confirmation can only come from the user who asked. |

**Not covered by the automated tests:** talking to the real Telegram servers. Telegram's HTTP API is faked, so before relying on the bot in production, play one 1v1 and one 2v2 match with real accounts. The checks above don't cover actual group privacy-mode delivery, `startgroup` links and supergroup upgrades on Telegram's side.

---

## Deployment

### Requirements

* Python 3.11+ and outbound HTTPS to `api.telegram.org`.
* **Persistent disk for `DATABASE_PATH`.** The SQLite file *is* the game. If the platform wipes the filesystem on restart or redeploy, every match, result and lobby is lost. Don't use ephemeral-filesystem hosts without a volume: classic Heroku dynos, serverless functions such as AWS Lambda or Cloud Run without a volume, or free tiers that recreate containers.
* **Run exactly one instance.** Telegram allows only one `getUpdates` consumer per token, and SQLite must not be shared over a network filesystem such as NFS or SMB.

### Polling or webhook

* **Long polling (default):** no public URL, TLS certificate or open port is needed. It works behind NAT and is the simplest to operate. This is the right choice for this bot.
* **Webhook:** set `WEBHOOK_URL=https://your.domain` (the bot listens on `PORT` at `/telegram`) and a random `WEBHOOK_SECRET`, which Telegram sends back in a header so forged requests are rejected. It saves idle polling but requires public HTTPS on port 443, 80, 88 or 8443. Use it if your platform charges for always-on outbound connections or requires inbound HTTP.

### Option A: a small VPS with systemd (recommended)

Any 512 MB Linux VPS works, for example Hetzner, DigitalOcean or Lightsail. The disk is persistent by default.

```bash
sudo useradd -m escape && sudo -iu escape
git clone <repo> escape_room_bot && cd escape_room_bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
.venv/bin/python bot.py --init-db
```

`/etc/systemd/system/escape-room-bot.service`:

```ini
[Unit]
Description=Competitive Escape Room Telegram bot
After=network-online.target

[Service]
User=escape
WorkingDirectory=/home/escape/escape_room_bot
ExecStart=/home/escape/escape_room_bot/.venv/bin/python bot.py
Restart=always
RestartSec=5
# graceful stop: PTB finishes in-flight updates on SIGTERM
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now escape-room-bot
journalctl -u escape-room-bot -f
```

### Option B: Docker with a named volume

```bash
cp .env.example .env   # add token
docker compose up -d --build
```

`docker-compose.yml` mounts the named volume `escape_room_data` at `/data` and sets `DATABASE_PATH=/data/escape_room.db`. Rebuilding or recreating the container keeps the volume. Don't run `docker compose down -v`, which deletes it.

### Option C: PaaS with a volume (Fly.io, Railway, Render)

Use the Dockerfile and **attach a persistent volume mounted at `/data`**:

* **Fly.io:** the repo includes a ready `fly.toml`. It mounts the volume `data` at `/data` and deliberately has **no `[http_service]`**, because the bot serves no web traffic and Fly would otherwise auto-stop it. With the CLI: `fly launch --no-deploy` (keep the existing `fly.toml`), `fly secrets set TELEGRAM_BOT_TOKEN=...`, `fly deploy`, then `fly scale count 1`. In the dashboard's *Launch from GitHub*, add `TELEGRAM_BOT_TOKEN` as an environment variable and leave Managed Postgres unchecked. Always run a single machine. Fly volumes are mounted owned by root; `docker-entrypoint.sh` fixes the ownership and then drops to the unprivileged `bot` user.
* **Railway:** add a Volume to the service with mount path `/data`, and set the `TELEGRAM_BOT_TOKEN` variable.
* **Render:** use a *Background Worker* with a *Persistent Disk* mounted at `/data`. Paid plans only; free instances have no persistent disk.

### Option D: PythonAnywhere, free, no card (webhook mode)

PythonAnywhere's free plan can't run a program 24/7, but it can host a small web app for free, without a card. In this mode Telegram sends each update to `https://<username>.pythonanywhere.com/telegram`, and `webhook_app.py` handles it. Everything is done in the browser, so it also works from an iPad or phone.

1. **Create a free account** at [pythonanywhere.com](https://www.pythonanywhere.com/).
2. **Create the web app.** On the **Web** tab: *Add a new web app* → *Next* → **Manual configuration** → **Python 3.11** or newer → *Next*.
3. **Get the code and install it.** On the **Consoles** tab, start a **Bash** console and run:
   ```bash
   git clone https://github.com/<owner>/<repo>.git escaperoom
   cd escaperoom
   python3.11 -m venv .venv          # same Python version as the web app
   .venv/bin/pip install -r requirements.txt
   ```
   For a private repository, use `https://<github-username>:<token>@github.com/<owner>/<repo>.git`, where `<token>` is a GitHub personal access token with read access to the repository.
4. **Run the setup script** in the same console:
   ```bash
   .venv/bin/python pythonanywhere_setup.py
   ```
   Paste the bot token when asked (it isn't shown as you type). The script writes `.env` (the token, a random webhook secret and PythonAnywhere's outbound proxy), creates the database, points the web app's WSGI file at the bot, and registers the webhook with Telegram.
5. **Set the virtualenv.** On the **Web** tab, under *Virtualenv*, enter `/home/<username>/escaperoom/.venv`.
6. Press the green **Reload** button, then send `/start` to the bot.

**Upkeep and limits**

* Free web apps must be renewed every three months: Web tab → **Run until 3 months from today**.
* With no background loop, retries of undelivered messages and lobby expiry run whenever an update arrives. For quiet periods, add a daily task on the **Tasks** tab: `/home/<username>/escaperoom/.venv/bin/python /home/<username>/escaperoom/bot.py --maintenance`.
* Free accounts get a daily CPU allowance (shown on the dashboard). Normal play uses a small fraction of it.
* **Updating:** in a Bash console run `cd escaperoom && git pull && .venv/bin/pip install -r requirements.txt`, then press **Reload**.
* **Errors:** see the *Error log* link on the Web tab.
* **Switching back to polling** elsewhere: run `.venv/bin/python pythonanywhere_setup.py --remove-webhook` first.

On any platform, check that a redeploy keeps the data: create a lobby, redeploy, and run `/status`.

---

## Backups, restarts and updates

**Backup** while the bot is running. This uses SQLite's online-backup API, which is safe with WAL:

```bash
python backup.py                         # -> backups/escape_room-YYYYmmdd-HHMMSS.db
python backup.py /mnt/offsite/escape.db  # custom destination
```

Don't back up with `cp` while the bot is running. You could copy a half-written WAL state. For a daily backup, add this to cron: `17 4 * * * cd /home/escape/escape_room_bot && .venv/bin/python backup.py`. To restore, stop the bot, replace the `DATABASE_PATH` file (and delete any `-wal` and `-shm` files next to it), then start the bot.

**Restarting** is always safe. Active matches resume where they left off, with the same room, hints and timer. Puzzles already delivered are not sent again. Messages that arrived while the bot was down are processed when it comes back.

**Updating the code:**

```bash
python backup.py                  # 1. back up first
git pull                          # 2. update
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py --init-db # 3. applies new migrations + validates puzzles
pytest -q                         # 4. optional but recommended
sudo systemctl restart escape-room-bot
```

Puzzle edits don't need a restart and never affect matches in progress.

---

## Known Telegram limitations

These are things the Bot API can't guarantee, and what the bot does instead:

1. **Exactly-once delivery isn't possible.** `sendMessage` has no idempotency key. The bot uses at-least-once delivery: if the process dies in the instant after Telegram accepted a message but before the bot recorded it as sent, that one message is sent again after restart. A missing puzzle would be worse than a duplicate.
2. **"The same moment" is limited by the network.** Both sides' Room 1 messages are sent in parallel, but Telegram's delivery time can differ by a fraction of a second. Fairness is based on the one `started_at` time stored in the database, not on when messages arrive.
3. **The bot can't see who is in a group** without admin rights or extra API calls, and Telegram doesn't report every member leaving to a non-admin bot in large groups. The bot gets the same guarantee by requiring each player to run `/join` **inside** the group and by allowing only registered members to act. If a teammate later leaves the group, they're still registered. Either player can `/quit`, which forfeits for the team.
4. **Privacy mode and other bots:** see [Group permissions](#group-permissions-and-privacy-mode). With several bots in a group, players may need `/answer@YourBotName`.
5. **Players must press Start privately first.** A bot can't message a user who hasn't started it, so the bot requires this for every 2v2 player.
6. **Blocked or removed bot:** if a player blocks the bot or removes it from a team group, messages to that chat fail. They are recorded as `failed` and logged. The game state stays correct, and a forfeit or `/status` still works from the other chats.
7. **Flood limits:** Telegram allows roughly 30 messages per second per bot and 20 messages per minute per group. The bot keeps messages short, merges each "room unlocked" notice with the next puzzle into one message, and respects `RetryAfter`.
