# Setup

The reference deployment is a Raspberry Pi running the three services natively
(Python venv + systemd), with devices reaching it over Tailscale. Adapt freely.

> Do **[SECURITY.md](SECURITY.md)** first. You are about to trust a CA you
> generate. Understand what that means.

## 1. The box

- A Raspberry Pi (or any always-on Linux box), Debian-based.
- Install dependencies:
  ```bash
  sudo apt update
  sudo apt install -y redis-server python3-venv python3-pip iptables
  ```
- Clone and set up the venv:
  ```bash
  git clone <your-fork> ~/lull && cd ~/lull
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
  ```

## 2. Redis

Enable persistence so state survives reboots (`redis.conf` in this repo is a
reference). At minimum, make sure `redis-server` is running and reachable on
`localhost:6379`.

## 3. Generate and install the CA (the step everyone gets wrong)

- Run mitmproxy once so it generates its CA in `~/.mitmproxy/`:
  ```bash
  venv/bin/mitmdump   # Ctrl-C after it starts; the CA is now generated
  ```
- With a device routed through the proxy, browse to **http://mitm.it** and install
  the certificate for your platform.
- **iOS:** installing the profile is not enough — you must also enable full trust:
  **Settings → General → About → Certificate Trust Settings → toggle it on.**
  Without this, Safari silently fails all HTTPS.

Never commit or share the CA. See SECURITY.md.

## 4. Routing (Tailscale)

- Install Tailscale on the box and your phone/laptop.
- Make the box an **exit node** (`tailscale up --advertise-exit-node`, approve it
  in the admin console) and select it as the exit node on your devices. Now all
  device traffic flows through the box, on cellular too.

## 5. Redirect + QUIC block

`deploy/budget-redirect.sh` transparently redirects `:80/:443` from the Tailscale
interface into mitmproxy and blocks QUIC (UDP/443) so clients fall back to
interceptable TCP. It installs the QUIC block in both the `filter` and (as a
Tailscale-proof backstop) the `mangle` table — Tailscale rewrites `filter/FORWARD`
on restart and would otherwise demote the rule.

## 6. systemd services

The units in `deploy/` run the three processes as the `pi` user from
`/home/pi/lull` (adjust paths/user for your box):

- `budget-app.service` — Flask (`app.py`)
- `budget-proxy.service` — mitmproxy (`addon.py`). Its `--allow-hosts` regex is the
  **TLS-decrypt allowlist**; every gated host must appear here.
- `budget-redirect.service` — runs the iptables script on boot.

```bash
sudo cp deploy/*.service /etc/systemd/system/
sudo cp deploy/budget-redirect.sh /usr/local/sbin/ && sudo chmod +x /usr/local/sbin/budget-redirect.sh
sudo systemctl daemon-reload
sudo systemctl enable --now budget-app budget-proxy budget-redirect
```

## 7. Verify

- `systemctl is-active budget-app budget-proxy budget-redirect redis-server` → all `active`.
- On a routed device, open a gated site in the browser → you should see the gate.
- `curl http://127.0.0.1:5000/remaining` on the box → JSON with per-site budgets.
- Visit `/budget/stats` for the usage dashboard.

## 8. Make it yours

Edit the top of `app.py`: `SITES`, budgets, `COOLDOWN_SECONDS`, refill rate, the
night-curfew hours, `STUDY_PLAYLISTS`. Remember the **three places** a new site
lives (README → Configure). Run `python -m pytest tests/` after changing budget
logic.

## Time zone

The night curfew and daily reset use the box's local time. Set it:
`sudo timedatectl set-timezone <Area/City>`.
