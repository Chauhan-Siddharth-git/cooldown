#!/usr/bin/env bash
# Cooldown — Docker installer.
#
#   ./docker-install.sh            gate a browser on THIS computer (the simple demo)
#   ./docker-install.sh --phone    also gate your phone, via Tailscale (advanced)
#   ./docker-install.sh --check    dry run: report what WOULD happen, change nothing
#   ./docker-install.sh --down     stop and remove the containers
#   ./docker-install.sh --logs     follow the logs
#
# Safe to re-run. For the permanent always-on setup, use ./install.sh on a Raspberry Pi.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY=0; PHONE=0; MODE=up
for a in "$@"; do case "$a" in
  --phone|--tailscale) PHONE=1 ;;
  --check|--dry-run)   DRY=1 ;;
  --down|--stop)       MODE=down ;;
  --logs)              MODE=logs ;;
  -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $a (try --help)"; exit 2 ;;
esac; done

if [ -t 1 ]; then B=$'\e[1m'; DIM=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else B=""; DIM=""; G=""; Y=""; R=""; C=""; N=""; fi
step() { printf "\n${B}${C}==>${N} ${B}%s${N}\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
info() { printf "  ${DIM}·${N} %s\n" "$*"; }
warn() { printf "  ${Y}!${N} %s\n" "$*"; }
die()  { printf "\n${R}✗ %s${N}\n" "$*" >&2; exit 1; }

COMPOSE_FILE=docker-compose.yml
[ "$PHONE" = 1 ] && COMPOSE_FILE=docker-compose.tailscale.yml

# ---------------------------------------------------------------- docker checks
step "Checking Docker"
command -v docker >/dev/null || die "Docker isn't installed. Get it from https://docs.docker.com/get-docker/"

# The two failures people actually hit, told apart so the fix is obvious.
if ! docker info >/dev/null 2>&1; then
  if docker info 2>&1 | grep -qi "permission denied"; then
    cat <<EOF

${R}${B}✗ Docker is installed but this user can't talk to it.${N}

  Fix it with:
    ${C}sudo usermod -aG docker \$USER${N}
  then ${B}log out and back in${N} (the group only applies to new sessions), and re-run this.

EOF
    exit 1
  fi
  die "Docker is installed but the daemon isn't running. Start Docker Desktop (or: sudo systemctl start docker)."
fi
ok "docker is running"

if docker compose version >/dev/null 2>&1; then DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then DC=(docker-compose); warn "using the old docker-compose binary"
else
  cat <<EOF

${R}${B}✗ The Docker Compose plugin is missing.${N}
  ${DIM}(Symptom if you run compose commands by hand: "unknown shorthand flag: 'd'")${N}

  Debian/Ubuntu:  ${C}sudo apt install docker-compose-v2${N}
  Mac/Windows:    it ships with Docker Desktop — make sure that's up to date.

EOF
  exit 1
fi
ok "compose available: ${DC[*]}"
[ -f "$COMPOSE_FILE" ] || die "$COMPOSE_FILE not found — run this from the repo directory."

# ---------------------------------------------------------------- down / logs
if [ "$MODE" = down ]; then
  step "Stopping"
  [ "$DRY" = 1 ] && { info "would: ${DC[*]} -f $COMPOSE_FILE down"; exit 0; }
  "${DC[@]}" -f "$COMPOSE_FILE" down
  ok "containers removed"
  echo "  ${DIM}The certificate you installed on your devices is still trusted — remove it there too"
  echo "  if you're done experimenting.${N}"
  exit 0
fi
if [ "$MODE" = logs ]; then exec "${DC[@]}" -f "$COMPOSE_FILE" logs -f; fi

# ---------------------------------------------------------------- what this is
if [ "$PHONE" = 1 ]; then
  cat <<EOF

${B}Cooldown — Docker + Tailscale (gates your phone too)${N}
${DIM}Runs Cooldown in a container that joins your Tailscale network as an exit node, so
your phone routes through it — even on cellular.${N}

${Y}${B}Read this bit:${N}
  · This is a ${B}privileged container${N} (it needs to run a VPN and route traffic).
  · It only gates while ${B}this computer is awake and online${N}.
  · It's a "no Raspberry Pi yet" option — not the reliable one. For always-on, run
    ${C}./install.sh${N} on a Pi instead.
EOF
else
  cat <<EOF

${B}Cooldown — Docker (this computer only)${N}
${DIM}Runs Cooldown in containers and gates the tempting sites in a browser on THIS
machine. No Pi, no VPN, nothing permanent — a try-before-you-commit demo.${N}

  ${Y}It does NOT gate your phone.${N} For that: ${C}./docker-install.sh --phone${N}
  ${DIM}or the real setup on a Raspberry Pi: ./install.sh${N}
EOF
fi

cat <<EOF

  To do its job Cooldown ${B}reads your own web traffic${N}: it makes a certificate
  ("master key") ${B}inside the container${N} and you tell your browser to trust it.
  Anyone holding that key could read traffic from anything that trusts it.
  ${DIM}Full explanation: SECURITY.md${N}
EOF
if [ "$DRY" != 1 ] && [ -t 0 ]; then
  consent=""
  read -r -p "  ${B}Type 'yes' to continue:${N} " consent </dev/tty || true
  [ "$consent" = yes ] || die "Cancelled — nothing was changed."
fi

# ---------------------------------------------------------------- timezone
TZGUESS="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)"
step "Timezone"
info "using ${B}$TZGUESS${N} ${DIM}(bedtime curfew + the daily reset use this)${N}"

# ---------------------------------------------------------------- tailscale key
if [ "$PHONE" = 1 ]; then
  step "Tailscale auth key"
  if [ -f .env ] && grep -q "^TS_AUTHKEY=tskey-" .env && ! grep -q "REPLACE-ME" .env; then
    ok ".env already has a key"
  elif [ "$DRY" = 1 ]; then
    info "would: prompt for a Tailscale auth key and write .env"
  else
    cat <<EOF
  You need a Tailscale auth key so the container can join your network.
    1. Open ${C}https://login.tailscale.com/admin/settings/keys${N}
    2. "Generate auth key", turn ${B}Reusable${N} on, leave Ephemeral off
    3. Copy the key (starts with ${DIM}tskey-auth-${N})

  ${Y}Treat it like a password.${N} It goes in .env, which is gitignored.
EOF
    [ -t 0 ] || die "Need an interactive terminal to ask for the key. Or write .env yourself: cp .env.example .env"
    tskey=""
    read -r -p "  Paste the key: " tskey </dev/tty || true
    case "$tskey" in
      tskey-*) ;;
      *) die "That doesn't look like a Tailscale auth key (it should start with 'tskey-'). Nothing was changed." ;;
    esac
    umask 077
    { echo "TS_AUTHKEY=$tskey"; echo "TS_HOSTNAME=cooldown-docker"; echo "TZ=$TZGUESS"; } > .env
    chmod 600 .env
    ok "wrote .env (mode 600, gitignored)"
  fi
fi

# ---------------------------------------------------------------- build & run
step "Building and starting"
if [ "$DRY" = 1 ]; then
  info "would: TZ=$TZGUESS ${DC[*]} -f $COMPOSE_FILE up -d --build"
  printf "\n${B}Dry run complete.${N} Re-run without --check to actually start it.\n"
  exit 0
fi
info "first build takes a few minutes…"
TZ="$TZGUESS" "${DC[@]}" -f "$COMPOSE_FILE" up -d --build

step "Checking it works"
sleep 6
"${DC[@]}" -f "$COMPOSE_FILE" ps
# The gate only answers a *navigation*, so send the header a browser would.
if curl -fsS --max-time 6 -x http://127.0.0.1:8080 -k \
        -H "Sec-Fetch-Mode: navigate" https://www.reddit.com/ 2>/dev/null | grep -qi "countdown\|time left\|take a break"; then
  ok "the proxy is gating Reddit"
else
  warn "couldn't confirm the gate yet — it may still be starting. Check: ./docker-install.sh --logs"
fi

# ---------------------------------------------------------------- next steps
cat <<EOF

${B}${G}Running.${N} Two things left, and they have to be done by hand:

${B}1 · Point your browser at the proxy${N}
   Set the HTTP and HTTPS proxy to ${C}127.0.0.1:8080${N}
   ${DIM}Firefox: Settings → Network Settings → Manual proxy configuration
   Chrome/Safari: they use the system proxy settings.${N}
EOF
if [ "$PHONE" = 1 ]; then cat <<EOF
   ${DIM}For the phone: open the Tailscale app, and pick ${B}cooldown-docker${N}${DIM} as your exit node.${N}
EOF
fi
cat <<EOF

${B}2 · Trust the certificate${N}
   On each device you want Cooldown to apply to, once it's pointed at the proxy,
   open ${C}http://mitm.it${N} and install the certificate for that platform.

   ${Y}iOS needs a second step people always miss:${N}
     Settings → General → About → Certificate Trust Settings → turn it on.
   ${DIM}Without it, websites silently fail to load.${N}

${B}Then${N}
   Visit a budgeted site — you should get the Cooldown gate instead of the feed.
   Logs:  ${C}./docker-install.sh --logs${N}
   Stop:  ${C}./docker-install.sh --down${N}
EOF
