#!/usr/bin/env bash
# Cooldown installer — guided setup for a Debian-based always-on box (Raspberry Pi).
#
#   ./install.sh            install (asks before anything that matters)
#   ./install.sh --check    dry run: report what WOULD happen, change nothing
#   ./install.sh --yes      non-interactive (accepts the security consent; for re-runs)
#   ./install.sh --uninstall  remove services/accounts (your CA + code are kept)
#
# Safe to re-run: every step checks before it acts.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER=cooldownapp
PROXY_USER=cooldownproxy
CA_DIR=/var/lib/cooldown/mitmproxy
UNITS=(cooldown-app cooldown-proxy cooldown-redirect)

DRY=0; ASSUME_YES=0; MODE=install
for a in "$@"; do case "$a" in
  --check|--dry-run) DRY=1 ;;
  --yes|-y)          ASSUME_YES=1 ;;
  --uninstall)       MODE=uninstall ;;
  -h|--help)         sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $a (try --help)"; exit 2 ;;
esac; done

if [ -t 1 ]; then B=$'\e[1m'; DIM=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else B=""; DIM=""; G=""; Y=""; R=""; C=""; N=""; fi

step()  { printf "\n${B}${C}==>${N} ${B}%s${N}\n" "$*"; }
ok()    { printf "  ${G}✓${N} %s\n" "$*"; }
info()  { printf "  ${DIM}·${N} %s\n" "$*"; }
warn()  { printf "  ${Y}!${N} %s\n" "$*"; }
die()   { printf "\n${R}✗ %s${N}\n" "$*" >&2; exit 1; }
would() { printf "  ${Y}would:${N} %s\n" "$*"; }
did()   { [ "$DRY" = 1 ] || ok "$*"; }   # only claim success when we really acted

# Run a privileged command, or just describe it in --check mode.
run() { if [ "$DRY" = 1 ]; then would "$*"; else sudo "$@"; fi; }
runu() { if [ "$DRY" = 1 ]; then would "$*"; else "$@"; fi; }

ask() {  # ask "Question?" [default_yes]
  [ "$ASSUME_YES" = 1 ] && return 0
  local q="$1" def="${2:-y}" p ans
  [ "$def" = y ] && p="[Y/n]" || p="[y/N]"
  read -r -p "  ${B}${q}${N} $p " ans </dev/tty || true
  ans="${ans:-$def}"; [[ "$ans" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------- uninstall
if [ "$MODE" = uninstall ]; then
  step "Uninstalling Cooldown"
  echo "  This removes the services, the firewall rules, the service accounts and the"
  echo "  sudo rule. It ${B}keeps${N} your CA ($CA_DIR) and this code."
  ask "Continue?" n || die "Cancelled."
  for u in "${UNITS[@]}"; do
    systemctl list-unit-files 2>/dev/null | grep -q "^$u.service" && \
      { run systemctl disable --now "$u" >/dev/null 2>&1 || true; ok "stopped $u"; }
  done
  [ -x /usr/local/sbin/cooldown-redirect.sh ] && \
    { run /usr/local/sbin/cooldown-redirect.sh down || true; ok "removed firewall rules"; }
  for u in "${UNITS[@]}"; do run rm -f "/etc/systemd/system/$u.service"; done
  run rm -f /usr/local/sbin/cooldown-redirect.sh /etc/sudoers.d/cooldown
  run systemctl daemon-reload
  for u in "$APP_USER" "$PROXY_USER"; do
    id "$u" >/dev/null 2>&1 && { run userdel "$u" || true; ok "removed account $u"; }
  done
  echo
  ok "Uninstalled. Your CA is still at $CA_DIR"
  echo "  ${DIM}To fully undo, also remove the Cooldown certificate from your phone/laptop.${N}"
  exit 0
fi

# ---------------------------------------------------------------- preflight
cat <<EOF

${B}Cooldown installer${N}
${DIM}Sets up the gateway on this machine: dependencies, service accounts, the
certificate, the firewall rules and the background services.${N}
EOF
[ "$DRY" = 1 ] && printf "\n${Y}${B}DRY RUN${N} — nothing will be changed.\n"

step "Checking this machine"
[ "$(id -u)" -eq 0 ] && die "Don't run this as root — run it as your normal user (it will use sudo when needed)."
command -v apt-get >/dev/null || die "This installer targets Debian/Ubuntu/Raspberry Pi OS (no apt-get found)."
command -v sudo    >/dev/null || die "sudo is required."
sudo -v || die "Could not get sudo. Run as a user with administrator rights."
ok "Debian-based, sudo available"

# Catch the "downloaded just this script" case with a useful message instead of a
# confusing failure three steps later.
for f in app.py addon.py requirements.txt deploy/cooldown-app.service; do
  [ -e "$REPO/$f" ] || die "This doesn't look like a full Cooldown checkout ($f is missing).
  Download the whole repository, then run the installer from inside it:
    git clone https://github.com/Chauhan-Siddharth-git/cooldown.git
    cd cooldown && ./install.sh"
done
ok "complete checkout"

# Check the Python version BEFORE touching anything: too old and pip would fail partway
# through, after apt had already run, leaving a half-finished install.
if ! command -v python3 >/dev/null; then
  die "python3 isn't installed. Install it first (sudo apt install python3), then re-run."
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if [ "$(printf '%s\n3.11\n' "$PYV" | sort -V | head -1)" != "3.11" ]; then
  cat <<EOF

${R}${B}✗ Python $PYV is too old.${N} Cooldown needs Python 3.11 or newer.

  Raspberry Pi OS ${B}Bookworm${N} (3.11) and ${B}Trixie${N} (3.13) both work. If you're on
  something older, flashing a current Raspberry Pi OS is the easy fix.

  ${DIM}Nothing has been changed.${N}
EOF
  exit 1
fi
ok "Python $PYV"

if ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
  HAVE_SYSTEMD=0
  warn "systemd not running (container?) — services won't be installed or started."
else
  HAVE_SYSTEMD=1; ok "systemd present"
fi

# Service accounts must be able to reach the code. /home/<you> is 700 by default.
HOME_DIR="$(getent passwd "$USER" | cut -d: -f6)"
case "$REPO" in
  /root/*) die "The code is under /root, which service accounts can never read. Move it to your home directory." ;;
esac
ok "Code at $REPO"
info "Timezone: $(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unknown) ${DIM}(night curfew + daily reset use this)${N}"

# ---------------------------------------------------------------- consent
step "What you're agreeing to"
cat <<EOF
  Cooldown works by ${B}reading your own internet traffic${N}. To do that it creates a
  ${B}certificate authority${N} — a master key — on this machine, and you tell your phone
  and laptop to trust it.

  ${Y}Anyone holding that key can read the traffic of every device that trusts it${N} —
  all of it, not just the sites you budget. That's the trade you're making.

  This installer ${B}generates your own key here${N}. It never downloads one, and you
  should never install a certificate somebody else gives you.

  ${DIM}Full explanation: SECURITY.md${N}
EOF
if [ "$ASSUME_YES" != 1 ] && [ "$DRY" != 1 ]; then
  consent=""
  read -r -p "  ${B}Type 'yes' to continue:${N} " consent </dev/tty || true
  [ "$consent" = yes ] || die "Cancelled — nothing was changed."
fi

# ---------------------------------------------------------------- packages
step "Installing dependencies"
MISSING=()
# inotify-tools is for cooldown-cawatch: it alerts when the CA private key is read.
# The alternative was hand-rolling inotify buffer parsing in Python to avoid one
# 50KB package; the parsing is the part that goes subtly wrong, so the package wins.
for p in redis-server python3-venv python3-pip iptables openssl inotify-tools; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ ${#MISSING[@]} -eq 0 ]; then ok "all present"
else
  info "need: ${MISSING[*]}"
  run apt-get update -qq
  run apt-get install -y "${MISSING[@]}"
  did "installed"
fi

# F11 in the case study: an always-on box with 18 pending security updates and nothing
# installing them. SECURITY.md tells people this is handled at install, so it has to
# actually be handled at install rather than by hand on one machine.
step "Automatic security updates"
if dpkg -s unattended-upgrades >/dev/null 2>&1 \
   && [ -f /etc/apt/apt.conf.d/20auto-upgrades ] \
   && grep -q '"1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null; then
  ok "already enabled"
elif [ "$DRY" = 1 ]; then
  would "install unattended-upgrades and enable daily security updates"
else
  echo "  ${DIM}A box you never log into again is exactly the thing that needs to patch"
  echo "  itself. Kernel updates need a restart to take effect, so one is scheduled for"
  echo "  04:00 — a gateway restarting mid-browse only matters while someone browses.${N}"
  if ask "Enable automatic security updates?"; then
    dpkg -s unattended-upgrades >/dev/null 2>&1 || { run apt-get update -qq; run apt-get install -y unattended-upgrades; }
    # AutocleanInterval too: without it the downloaded .deb files just accumulate
    # (426 MB on a box that had been patching itself for a few weeks).
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\nAPT::Periodic::AutocleanInterval "7";\n' \
      | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
    # The periodic flags alone are not enough. Debian's stock origins allow origin=Debian
    # only, so on a Pi the kernel, firmware, rpi-eeprom -- and tailscaled, which this
    # box's remote access depends on -- were silently never upgraded. Found ten days
    # after shipping "automatic security updates are on", which was true and useless.
    sudo tee /etc/apt/apt.conf.d/51cooldown-unattended >/dev/null <<'APTEOF'
// APT appends to an existing list rather than replacing it, so this is additive.
Unattended-Upgrade::Origins-Pattern {
        "origin=Raspberry Pi Foundation,codename=${distro_codename}";
        "origin=Tailscale";
};
// Kernel updates need a restart to take effect. 04:00 sits inside the night window and
// after the 03:00 upgrade run below.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
// Automatic kernel updates without this fill a small /boot within a few releases.
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
APTEOF
    # Upgrade at 03:00 so a required reboot lands at 04:00 the same night; stock 06:00
    # +/-60m can miss the slot and leave the flag sitting for 22 hours.
    sudo mkdir -p /etc/systemd/system/apt-daily-upgrade.timer.d /etc/systemd/system/apt-daily.timer.d
    sudo tee /etc/systemd/system/apt-daily-upgrade.timer.d/cooldown-schedule.conf >/dev/null <<'TEOF'
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:00
RandomizedDelaySec=30m
TEOF
    # Ordering after time-sync is ordinary hygiene for Persistent= timers on a board
    # with no battery-backed RTC. It is NOT what fixed the 2026-08 outage on the
    # reference box -- that was a first-boot wizard blocking multi-user.target, so the
    # timers fired and their jobs queued forever. Kept because it is correct, not
    # because it was the cure.
    for t in apt-daily apt-daily-upgrade; do
      sudo tee /etc/systemd/system/$t.timer.d/cooldown-timesync.conf >/dev/null <<'TSEOF'
[Unit]
Wants=time-sync.target
After=time-sync.target
TSEOF
    done
    # Patching a library on disk does nothing for a process holding the old copy in
    # memory. needrestart restarts what actually needs it -- except ssh, the tailnet and
    # dbus, where losing the service costs more than the delay to the 04:00 reboot.
    dpkg -s needrestart >/dev/null 2>&1 || run apt-get install -y needrestart
    sudo mkdir -p /etc/needrestart/conf.d
    sudo tee /etc/needrestart/conf.d/50-cooldown.conf >/dev/null <<'NREOF'
$nrconf{restart} = 'a';
$nrconf{override_rc} = { qr(^(ssh|sshd|tailscaled|dbus)$) => 0 };
NREOF
    sudo systemctl daemon-reload
    ok "security updates install themselves; kernel reboots at 04:00"
  else
    warn "skipped — run 'sudo unattended-upgrades --dry-run' occasionally, or the box rots"
  fi
fi

# A Pi ships avahi so it can be found as raspberrypi.local. This box is reached over the
# tailnet by address, so mDNS buys nothing -- and it listens on UDP 5353 on every
# interface, which on a connection with native IPv6 means a globally routable one. The
# firewall here is an allowlist of a few TCP ports over a default-ACCEPT policy, so a
# UDP listener is not covered by it at all. Disabling the service removes the listener
# rather than trying to firewall it.
#
# Disabled, not purged: libnss-mdns and rpi-usb-gadget depend on the package. The socket
# is masked too, or socket activation quietly starts the daemon again.
step "Local network discovery (avahi)"
if ! dpkg -s avahi-daemon >/dev/null 2>&1; then
  ok "not installed"
elif [ "$(systemctl is-enabled avahi-daemon.service 2>/dev/null)" = "masked" ]; then
  ok "already disabled"
elif [ "$DRY" = 1 ]; then
  would "mask avahi-daemon.service and .socket (removes the mDNS listener)"
else
  echo "  ${DIM}You reach this box by address over the tailnet, so .local discovery is"
  echo "  unused -- but it listens on every interface, including public IPv6.${N}"
  if ask "Disable mDNS/avahi?"; then
    run systemctl disable --now avahi-daemon.socket avahi-daemon.service >/dev/null 2>&1 || true
    run systemctl mask avahi-daemon.socket avahi-daemon.service >/dev/null
    ok "avahi disabled — <hostname>.local will no longer resolve to this box"
  else
    warn "skipped — mDNS stays reachable on every interface this box has"
  fi
fi

step "Python environment"
if [ -x "$REPO/venv/bin/python" ]; then ok "venv exists"
else runu python3 -m venv "$REPO/venv"; did "venv created"; fi
if [ "$DRY" = 1 ]; then would "pip install -r requirements.txt"
else
  "$REPO/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
  "$REPO/venv/bin/pip" install -q -r "$REPO/requirements.txt"
  ok "dependencies installed"
fi

step "Redis"
if [ "$HAVE_SYSTEMD" = 1 ]; then
  systemctl is-enabled redis-server >/dev/null 2>&1 || run systemctl enable redis-server >/dev/null
  systemctl is-active  redis-server >/dev/null 2>&1 || run systemctl start  redis-server
  [ "$DRY" = 1 ] || { redis-cli ping >/dev/null 2>&1 && ok "responding on localhost:6379" || warn "redis not responding yet"; }
  # Durability, which the distro package does NOT give you: Debian ships appendonly no,
  # so an unclean stop loses every write back to the last RDB snapshot -- up to an hour
  # under the default save policy. That is your spent time, your cooldowns and your
  # history. The Docker path has always set this via compose flags; this native path
  # never did, and the gap went unnoticed for months because the two deployments were
  # hardened separately and only one of them was checked.
  # CONFIG REWRITE persists it into redis.conf so it survives a restart, and the hourly
  # audit re-checks aof_enabled on the running server afterwards -- the config file only
  # records an intention.
  if [ "$DRY" = 1 ]; then would "enable Redis AOF persistence (appendonly yes)"
  elif [ "$(redis-cli config get appendonly 2>/dev/null | tail -1)" = "yes" ]; then
    ok "AOF persistence already on"
  elif redis-cli config set appendonly yes >/dev/null 2>&1 && redis-cli config rewrite >/dev/null 2>&1; then
    did "AOF persistence enabled and written to redis.conf"
  else
    warn "could not enable Redis AOF -- state will only be as fresh as the last RDB snapshot"
  fi
else info "skipped (no systemd)"; fi

# ---------------------------------------------------------------- accounts
step "Service accounts"
echo "  ${DIM}Cooldown's programs run as locked-down accounts with no password and no login,"
echo "  so a flaw in them can't take over the machine.${N}"
for u in "$APP_USER" "$PROXY_USER"; do
  if id "$u" >/dev/null 2>&1; then ok "$u exists"
  else run useradd --system --no-create-home --shell /usr/sbin/nologin "$u"; did "created $u"; fi
done
# They need to traverse into the code directory (homes are 700 by default).
if [ "$HOME_DIR" != "" ] && [ -d "$HOME_DIR" ]; then
  PERM="$(stat -c %a "$HOME_DIR")"
  if [ "$PERM" = 700 ]; then
    info "$HOME_DIR is 700 — the service accounts can't reach the code through it"
    if ask "Change it to 711 (they can pass through, but still cannot list your files)?"; then
      run chmod 711 "$HOME_DIR"; did "$HOME_DIR is now 711"
    else warn "left as-is — the services will fail to start until they can read $REPO"; fi
  else ok "$HOME_DIR is $PERM (reachable)"; fi
fi

# ---------------------------------------------------------------- the CA
step "Certificate authority"
if [ -f "$CA_DIR/mitmproxy-ca.pem" ]; then
  ok "already present at $CA_DIR"
  [ "$DRY" = 1 ] || info "fingerprint: $(sudo openssl x509 -in "$CA_DIR/mitmproxy-ca-cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
elif [ "$DRY" = 1 ]; then
  would "generate a CA and move it to $CA_DIR"
else
  echo "  ${DIM}Generating your own certificate authority (nothing is downloaded)…${N}"
  TMPCA="$(mktemp -d)"
  # Generated by deploy/gen_ca.sh rather than by letting mitmdump make its own, because
  # mitmproxy's CA can vouch for ANY hostname. This one is constrained to the hosts in
  # gen_allow_hosts.py, so the copy someone takes off the SD card is a trust anchor for
  # Reddit and the news list — not for your bank. gen_ca.sh verifies that on itself and
  # refuses to write a CA whose constraints are not being enforced.
  "$REPO/deploy/gen_ca.sh" "$TMPCA" >/dev/null \
      || die "CA generation failed (see deploy/gen_ca.sh; is openssl present?)"
  [ -f "$TMPCA/mitmproxy-ca.pem" ] || die "gen_ca.sh produced no CA"
  sudo mkdir -p "$(dirname "$CA_DIR")"
  sudo cp -a "$TMPCA" "$CA_DIR"                    # cp -a keeps the 600 key modes
  sudo chown -R "$PROXY_USER:$PROXY_USER" "$(dirname "$CA_DIR")"
  sudo chmod 700 "$(dirname "$CA_DIR")" "$CA_DIR"
  rm -rf "$TMPCA"
  ok "created, owned by $PROXY_USER, mode 700"
  info "constrained to $(python3 "$REPO/deploy/gen_allow_hosts.py" --name-constraints | grep -c '^permitted;DNS') gated domains"
  info "fingerprint: $(sudo openssl x509 -in "$CA_DIR/mitmproxy-ca-cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
  warn "This key is the master key. Never copy it off this machine."
fi

# ---------------------------------------------------------------- services
step "Services and firewall"
if [ "$HAVE_SYSTEMD" = 0 ]; then
  info "skipped (no systemd)"
else
  for u in "${UNITS[@]}"; do
    src="$REPO/deploy/$u.service"; [ -f "$src" ] || die "missing $src"
    if [ "$DRY" = 1 ]; then would "install /etc/systemd/system/$u.service (paths → $REPO)"; continue; fi
    # The shipped units assume /home/pi/cooldown; point them at wherever this actually is.
    sed -e "s#/home/pi/cooldown#$REPO#g" "$src" | sudo tee "/etc/systemd/system/$u.service" >/dev/null
  done
  [ "$DRY" = 1 ] || ok "units installed (paths set to $REPO)"
  run install -m 755 "$REPO/deploy/cooldown-redirect.sh" /usr/local/sbin/cooldown-redirect.sh
  did "redirect script installed"

  # One scoped sudo read for the packet-feed background. Validate before installing.
  if [ -f "$REPO/deploy/sudoers.d-cooldown" ]; then
    if [ "$DRY" = 1 ]; then would "install /etc/sudoers.d/cooldown (after visudo -c)"
    elif sudo visudo -cf "$REPO/deploy/sudoers.d-cooldown" >/dev/null 2>&1; then
      sudo install -m 440 -o root -g root "$REPO/deploy/sudoers.d-cooldown" /etc/sudoers.d/cooldown
      ok "granted one firewall read (nothing else)"
    else warn "sudoers file failed validation — skipped (harmless: the background just shows less red)"; fi
  fi

  run systemctl daemon-reload
  if [ "$DRY" = 1 ]; then would "enable + start ${UNITS[*]}"
  else
    sudo systemctl enable --now "${UNITS[@]}" >/dev/null 2>&1 || true
    sleep 3
    for u in "${UNITS[@]}"; do
      s="$(systemctl is-active "$u" 2>/dev/null || true)"
      [ "$s" = active ] && ok "$u running" || { warn "$u is '$s' — check: journalctl -u $u -n 30"; }
    done
  fi
fi

# ---------------------------------------------------------------- verify
step "Checking it works"
if [ "$DRY" = 1 ]; then would "curl the gate and the budget API"
elif [ "$HAVE_SYSTEMD" = 1 ]; then
  sleep 1
  if curl -fsS --max-time 5 http://127.0.0.1:5000/remaining >/dev/null 2>&1; then
    ok "the app is answering on 127.0.0.1:5000"
  else warn "the app isn't answering yet — journalctl -u cooldown-app -n 30"; fi
fi

# ---------------------------------------------------------------- next steps
if [ "$DRY" = 1 ]; then printf "\n${B}Dry run complete.${N} Re-run without --check to apply.\n"; exit 0; fi

cat <<EOF

${B}${G}Installed.${N} Two things are left, and they can't be automated:

${B}1 · Route your devices through this box${N}
   Install Tailscale here and on your phone/laptop, then make this box an exit node:

     ${C}curl -fsSL https://tailscale.com/install.sh | sh${N}
     ${C}sudo tailscale up --advertise-exit-node${N}

   Approve the exit node in the Tailscale admin console, then pick this box as the
   exit node on your phone. ${DIM}(Traffic only flows through Cooldown once you do.)${N}

${B}2 · Trust the certificate on each device${N}
   On each device you want Cooldown to apply to — once it's routed through this box
   (step 1 above) — open ${C}http://mitm.it${N} and install the certificate for your platform.

   ${Y}iOS needs a second step people always miss:${N}
     Settings → General → About → Certificate Trust Settings → turn it on.
   ${DIM}Without it, websites silently fail to load.${N}

${B}Then${N}
   Open a budgeted site on that device — you should see the Cooldown gate.

   ${B}Usage dashboard:${N} ${C}http://<this-box's tailscale IP>:5000/stats${N}
   ${DIM}Deliberately served on this box's own address rather than inside the gated site:
   that makes it a different origin, so a script on Reddit can't read your device list.
   Reachable over Tailscale only — the firewall drops :5000 on every other interface.
   The gate page links to it, so you don't have to remember the address.${N}
   Change sites and time limits at the top of ${C}app.py${N}, then:
     ${C}sudo systemctl restart cooldown-app cooldown-proxy${N}

   Undo everything: ${C}./install.sh --uninstall${N}
EOF
