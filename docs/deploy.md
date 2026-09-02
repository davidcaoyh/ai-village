# Deploying the village

The whole system is one Python process that writes a SQLite file and a second
one that reads it. That is the only fact that drives every choice below.

---

## 1. What to rent

**Recommendation: DigitalOcean Basic droplet, Toronto.** 1 vCPU, 1 GB RAM,
25 GB SSD, **$6/month**. This is the machine `architecture.md` §6 describes and
the one this guide assumes.

| Option | Specs | Price/mo | Verdict |
|---|---|---|---|
| **DigitalOcean Basic, Toronto** | 1 vCPU, 1 GB, 25 GB | **$6** | What this guide assumes. Canadian region, simplest console. |
| Hetzner CX22 (ash / hil) | 2 vCPU, 4 GB, 40 GB | $4.59 | More hardware for less money if a US-East region is acceptable. Same steps. |
| DigitalOcean Basic (smallest) | 1 vCPU, 512 MB, 10 GB | $4 | Works, but 512 MB leaves no room to run a session and serve at once. |
| Fly.io / Render / Railway | — | $5–$10+ | A persistent SQLite file is the awkward case on a PaaS, not the normal one. Skip. |

Why so small is enough: this box never runs a model. It sends HTTP requests to
OpenRouter and waits. Peak memory is one Python interpreter plus a SQLite page
cache; peak CPU is JSON parsing. 1 GB covers that with room to serve at the
same time.

Two things you are **not** paying the VPS for, and should budget separately:

- **OpenRouter credit.** A 120-turn session with this cast costs roughly
  **$0.30–$0.60**. `scripts/preflight.py` prints an estimate from live prices.
  Load $10 and set a hard account cap in the dashboard.
- **A domain**, about $12/year, and optional. Caddy will get a real certificate
  for a free wildcard-DNS name like `village.203-0-113-9.sslip.io`, where the
  dashes are your server's IP. Use that until the project is worth a domain.

## 2. What runs on it

```
                    :443  Caddy  ──────────────► systemd: village-web
                    (TLS, auto-renewed)          uvicorn on 127.0.0.1:8000
                                                        │ reads
                                            /opt/village/runs/village.db
                                                        ▲ writes
                                                 systemd: village-session
                                                 oneshot, weekly timer
```

Two units, because the two processes have genuinely different lifecycles:

- `village-web` is **always on**. If it dies, restart it; nothing is lost,
  because it holds no state (D21).
- `village-session` is a **bounded job**. It has a turn cap and a spend cap, and
  ending is the expected outcome. `Restart=always` here would be a machine that
  spends money forever, which is why it is `Type=oneshot` on a timer (D29).

Everything for this lives in `deploy/`. Read the unit files before you install
them — they are short, and each comment says why a line is there.

## 3. Deploy, in stages

Each stage ends with a command and what you should see. Do not go on until you
see it.

### Stage 0 — before you rent anything (on your laptop)

```bash
pytest -q
python -m scripts.run_session --fake --turns 16 --delay 0
uvicorn server.main:app --port 8000     # open http://localhost:8000
```

**Expect:** 70 tests pass; the fake run prints 16 turns and ends with
`turn_cap`; the page shows four columns filling in, a rising cost pill, and a
group chat. If any of that is wrong, fix it here — debugging it over SSH is
strictly worse.

Run `--fake` twice and the header's session picker lists both, newest first and
marked `●`. That is the archive: every run is its own `session_id`, the page
shows one at a time, and picking an older one reloads at `?session=<id>`. Worth
checking locally, because it is the difference between a site that shows three
weeks of work and one that shows today.

### Stage 1 — the box

Rent the droplet with **Ubuntu 24.04** and your SSH key. `bootstrap.sh` works on
any copy of the repo at `/tmp/village`; how it gets there depends on whether the
repo is public yet.

Public repo — clone on the box:

```bash
ssh root@<ip>
git clone https://github.com/<you>/ai-village.git /tmp/village
bash /tmp/village/deploy/bootstrap.sh
```

Private repo — a fresh droplet has no GitHub credentials, so copy it up instead.
The alternative is a deploy key, which is more moving parts for the same result:

```bash
cd ~/Documents/ECE/"Zhijing Lab"/project
rsync -az --exclude .git --exclude .venv --exclude _private --exclude .env \
      --exclude runs --exclude .DS_Store --exclude .pytest_cache \
      --exclude .ruff_cache --exclude '*.egg-info' \
      ./ root@<ip>:/tmp/village/
ssh root@<ip> 'bash /tmp/village/deploy/bootstrap.sh'
```

`--exclude .env` matters: `bootstrap.sh` excludes it too, but a key that never
leaves the laptop cannot leak from the staging directory either.

**Expect:** the script ends with a numbered "Remaining" list. It has created the
`village` user, `/opt/village`, a venv, `/etc/village.env` with a generated
admin token, the systemd units, and a firewall allowing only 22/80/443.

### Stage 2 — secrets

```bash
nano /etc/village.env          # add OPENROUTER_API_KEY, TAVILY_API_KEY
systemctl restart village-web
curl -s localhost:8000/healthz
```

**Expect:** `{"ok":true,"sessions":0}`.

`/etc/village.env` is mode 600 and outside the repo. Keys never enter a unit
file: unit files are world-readable.

### Stage 3 — TLS

You do not need a domain. `<dashed-ip>.sslip.io` resolves to that IP and Caddy
gets a real certificate for it, so `143-198-1-2.sslip.io` works today and a
bought domain is a one-line change later.

Get the IPv4 specifically. The droplet has both, and curl prefers IPv6 when it
can, which hands you a `2604:...` address and an sslip name that IPv4-only
networks cannot reach:

```bash
curl -4 -s ifconfig.me; echo        # 143.198.1.2 -> 143-198-1-2.sslip.io
```

Copy the template first, then put the name in `/etc/caddy/Caddyfile` rather than
in `/opt/village/deploy/Caddyfile` - the app tree is replaced on every redeploy,
`/etc` is not (same reason secrets live in `/etc/village.env`).

```bash
cp /opt/village/deploy/Caddyfile /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile        # village.example.com -> 143-198-1-2.sslip.io
systemctl reload caddy
curl -s https://<your-name>/healthz
```

**Expect:** `{"ok":true,"sessions":0}` — which proves more than a status code
does, since it means the app opened its database. Do not probe this with
`curl -I`: that sends HEAD, and the routes are registered GET-only, so a working
site answers `405 Method Not Allowed`. If Caddy cannot get a certificate you get
a connection error rather than any status, and the answer is almost always DNS
not having propagated yet — `dig +short <your-name>` should return your IP.

Then check the one endpoint that is not public, from your own machine rather
than from the box (D28):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<your-name>/api/stop \
     -H 'content-type: application/json' -d '{}'
```

**Expect:** `401`. A `200` means `VILLAGE_ADMIN_TOKEN` is empty in
`/etc/village.env` and anyone can end a run that is spending money. Send the
header — without it FastAPI rejects the body with a `422` before the token check
runs, which looks like a pass and tests nothing.

### Stage 4 — preflight, before spending anything

A shell does not get two things the units get for free. `scripts/` is not an
installed package (`pyproject.toml` ships `village` and `server` only), so it
imports from the project root and nowhere else; and the key lives in
`/etc/village.env`, which systemd reads for the units and nothing reads for you.
So: cd, source, run. Preflight writes no files, so root is fine here.

```bash
cd /opt/village
set -a; . /etc/village.env; set +a

.venv/bin/python -m scripts.preflight --dry     # free, skips the paid call
.venv/bin/python -m scripts.preflight           # the full six, ~$0.001
```

**Expect:** every line `ok`, and an estimated session cost. This makes one real
tool-calling request per model (well under a cent total) because catalogue
metadata is a claim and a tool call is proof. A model that answers with prose
here would answer with prose for 30 turns in a real session while still being
billed.

### Stage 5 — one supervised session

Prove the box can run eight turns before paying for 120. The unit takes no
`--turns` flag, so this one is by hand, **as `village` and never as root**: a
session run as root leaves a root-owned `village.db` plus its `-wal` and `-shm`
files in `runs/`, and the service then cannot write to its own database.

```bash
cd /opt/village
set -a; . /etc/village.env; set +a
sudo -E -u village .venv/bin/python -m scripts.run_session --turns 8
```

Then the real one, through systemd, which supplies the env itself:

`--no-block` matters: the unit is `Type=oneshot`, so a plain `systemctl start`
waits for the whole session to finish while printing nothing, because a unit's
output goes to the journal and not to your terminal.

```bash
journalctl -fu village-session               # terminal 1, start this first

systemctl start --no-block village-session   # terminal 2, 120 turns, $0.30-0.60
free -m                                      # a few times during the run, for run-notes.md
```

systemd owns the process, so the run survives your SSH connection dropping.
Ctrl-C in the journal window only stops watching; to end the run use the UI
button (it stops at a turn boundary and writes `session_end`) or
`systemctl stop village-session`.

**Expect:** turn lines with a rising spend figure. Open the site in a browser
while it runs: events should appear within a second or two. Then read the
transcript end to end — `python -m scripts.replay <session-id>` — before you
trust it unattended.

### Stage 6 — only now, the weekly timer

The site shows the newest session, so enabling the timer hands the homepage to
whatever runs next. Leave it off until there is a run you are happy to be judged
on; a session started by hand writes the same events.

```bash
systemctl enable --now village-session.timer
systemctl list-timers village-session
```

**Expect:** a `NEXT` time on the coming Monday.

## 4. Operating it

```bash
journalctl -u village-web -n 50          # site logs
journalctl -u village-session -n 200     # last session
systemctl start village-session          # run one now
sqlite3 /opt/village/runs/village.db "select count(*) from events"
```

**Backups.** One file matters: `/opt/village/runs/village.db`. Use SQLite's own
backup command, not `cp` — copying a WAL-mode database while it is being
written gives you a file that may not open.

```bash
sqlite3 /opt/village/runs/village.db ".backup /root/village-$(date +%F).db"
```

**Updating.** From your laptop, not from the box:

```bash
bash deploy/redeploy.sh root@<ip>
```

It rsyncs the working tree, reinstalls the package, and restarts `village-web`,
leaving `/etc/village.env` and `runs/` untouched. There is no `git pull` on the
server: `bootstrap.sh` copies the tree with `--exclude .git`, so `/opt/village`
is a plain directory rather than a checkout.

If you only have SSH and the repo is public, fetch a fresh copy and re-run
bootstrap, which is idempotent and keeps the existing `/etc/village.env`:

```bash
rm -rf /tmp/village && git clone https://github.com/<you>/ai-village.git /tmp/village
bash /tmp/village/deploy/bootstrap.sh
```

While the repo is private a fresh droplet has no credentials for that clone, so
the laptop path is the only one. The schema is `CREATE TABLE IF NOT EXISTS`, so
old sessions keep replaying across updates either way.

**Stopping a run.** The button in the UI now asks for the admin token from
`/etc/village.env` (D28). From the shell: `systemctl stop village-session` — but
prefer the button, because it stops at a turn boundary and writes a proper
`session_end` event, and `systemctl stop` sends a signal mid-turn.

## 5. What is deliberately not here

- **No Docker.** One process and one file. A container adds a build step and a
  volume mount to arrive at the same place. Reversible: a `Dockerfile` would be
  ten lines if a second deployment target ever appears.
- **No database server.** A few writes per second, single writer (D3), one file
  to back up. Postgres would be operations work bought with nothing.
- **No login.** Watching is the point. The one control that is gated is the one
  that spends or destroys — see D28.
- **No autoscaling, no CDN, no queue.** The site's whole payload is one HTML
  file and a WebSocket carrying a few events per second.
