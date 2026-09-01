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

**Expect:** 33 tests pass; the fake run prints 16 turns and ends with
`turn_cap`; the page shows four columns filling in, a rising cost pill, and a
group chat. If any of that is wrong, fix it here — debugging it over SSH is
strictly worse.

### Stage 1 — the box

Rent the droplet with **Ubuntu 24.04** and your SSH key. Then:

```bash
ssh root@<ip>
git clone https://github.com/<you>/ai-village.git /tmp/village
bash /tmp/village/deploy/bootstrap.sh
```

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

```bash
cp /opt/village/deploy/Caddyfile /etc/caddy/Caddyfile   # put the name in first
systemctl reload caddy
curl -sI https://<your-name>/healthz | head -1
```

**Expect:** `HTTP/2 200`. If Caddy cannot get a certificate, the answer is
almost always DNS not having propagated yet — `dig +short <your-name>` should
return your IP.

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

```bash
sudo -u village /opt/village/.venv/bin/python -m scripts.preflight
```

**Expect:** every line `ok`, and an estimated session cost. This makes one real
tool-calling request per model (well under a cent total) because catalogue
metadata is a claim and a tool call is proof. A model that answers with prose
here would answer with prose for 30 turns in a real session while still being
billed.

### Stage 5 — one supervised session

```bash
systemctl start village-session
journalctl -fu village-session
```

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

**Updating.** `git -C /opt/village pull && systemctl restart village-web`. The
schema is `CREATE TABLE IF NOT EXISTS`, so old sessions keep replaying.

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
