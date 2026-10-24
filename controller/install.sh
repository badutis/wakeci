#!/usr/bin/env bash
# Installs or updates the controller on the host it runs on: any Linux with systemd and
# Python 3.9+ (a small ARM board such as an Orange Pi, MKS Pi or Raspberry Pi, or an x86
# machine). Run as root from an unpacked bundle (controller/deploy.sh builds and copies one):
#
#   sudo bash controller/install.sh [--config FILE] [--version VERSION] [--ram-journal]
#   sudo bash controller/install.sh --set-token NAME      # reads one token, nothing echoed
#
# --ram-journal keeps the systemd journal of the WHOLE host in RAM (Storage=volatile), to
# spare an SD card. All logs are then lost on reboot, so use it only on such a board. Without
# it the journal is left as it is.
#
# The installed copy (/opt/wakeci/controller/install.sh) works the same way, so after the
# first install `sudo bash /opt/wakeci/controller/install.sh` re-runs it in place.
#
# Running it again only changes what differs. A run without --set-token never overwrites
# an existing SSH key, a known host key or a token. The service is (re)started only when the
# configuration and every token file it names are in place.
set -euo pipefail

ETC=/etc/wakeci
OPT=/opt/wakeci
UNIT=/etc/systemd/system/wakeci.service
JOURNALD_DROPIN=/etc/systemd/journald.conf.d/50-wakeci-volatile.conf
SVC_USER=wakeci
# The unit runs exactly this interpreter (controller/wakeci.service), so it is the one checked.
PY=/usr/bin/python3

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "-- $*"; }

CONFIG=""
VERSION=""
TOKEN_NAME=""
RAM_JOURNAL=no
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --set-token) TOKEN_NAME="$2"; shift 2 ;;
    --ram-journal) RAM_JOURNAL=yes; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run with sudo"
# The service, the journal and the restart checks all need systemd as the init system.
[ -d /run/systemd/system ] || die "this host does not run systemd; the controller needs it"
for c in useradd runuser install; do
  command -v "$c" >/dev/null || die "'$c' not found (it comes with the base system on Debian, Ubuntu, Fedora, Arch)"
done

# --- one token ---------------------------------------------------------------------------
if [ -n "$TOKEN_NAME" ]; then
  [[ "$TOKEN_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || die "token name must start with a letter or digit and hold only letters, digits, '.', '_' and '-'"
  id "$SVC_USER" >/dev/null 2>&1 || die "install the controller first"
  # Never one of the other files in $ETC. The final mv replaces a symlink itself, it does
  # not write through it.
  case "$TOKEN_NAME" in
    config.json|*.pub|id_ed25519|known_hosts) die "$TOKEN_NAME is not a token file name" ;;
  esac
  if [ -f "$ETC/config.json" ]; then
    PATHS="$("$PY" "$OPT/controller/check_config.py" "$ETC/config.json")" \
      || die "$ETC/config.json is invalid: fix it before setting tokens"
    if printf '%s\n' "$PATHS" | awk -F'\t' '$1 == "ssh_key" || $1 == "known_hosts" {print $2}' | grep -qxF "$ETC/$TOKEN_NAME"; then
      die "$ETC/$TOKEN_NAME is the SSH key or known_hosts, not a token"
    fi
  fi

  if [ -t 0 ]; then
    printf 'token for %s/%s (not shown): ' "$ETC" "$TOKEN_NAME" >&2
    IFS= read -rs TOKEN; echo >&2
  else
    IFS= read -r TOKEN || true
  fi
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  [ -n "$TOKEN" ] || die "empty token, nothing written"
  TMP="$(mktemp "$ETC/.token.XXXXXX")"
  printf '%s\n' "$TOKEN" > "$TMP"
  unset TOKEN
  chown "$SVC_USER:$SVC_USER" "$TMP"
  chmod 0600 "$TMP"
  mv -f "$TMP" "$ETC/$TOKEN_NAME"
  note "written $ETC/$TOKEN_NAME (0600, $SVC_USER); run the installer again to start the service"
  exit 0
fi

[ -f "$SRC/wakeci/__main__.py" ] || die "no wakeci/ package next to controller/ (run from an unpacked bundle)"
for f in wakeci.service check_config.py config.example.json; do
  [ -f "$HERE/$f" ] || die "controller/$f missing"
done
[ -z "$VERSION" ] || [[ "$VERSION" =~ ^[A-Za-z0-9._+-]+$ ]] || die "bad --version"
[ -z "$CONFIG" ] || [ -f "$CONFIG" ] || die "--config file not found: $CONFIG"

# --- the host ----------------------------------------------------------------------------
# ARM boards report their model in the device tree; an x86 machine through DMI.
MODEL="unknown"
if [ -r /proc/device-tree/model ]; then
  MODEL="$(tr -d '\0' < /proc/device-tree/model)"
elif [ -r /sys/class/dmi/id/product_name ]; then
  MODEL="$(cat /sys/class/dmi/id/product_name)"
fi
MEM_MB="$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)"
note "host: $MODEL, $(uname -m), ${MEM_MB} MB RAM"

# --- packages ----------------------------------------------------------------------------
NEED=()
[ -x "$PY" ] || NEED+=(python3)
for c in ssh ssh-keygen ssh-keyscan; do
  command -v "$c" >/dev/null || { NEED+=(openssh-client); break; }
done
# python3-minimal alone has the binary but not the whole standard library.
STDLIB='import compileall, http.server, ipaddress, json, urllib.request'
[ -x "$PY" ] && ! "$PY" -c "$STDLIB" 2>/dev/null && NEED+=(python3)
if [ ${#NEED[@]} -gt 0 ]; then
  # Installed automatically only with apt (Debian, Ubuntu, Raspberry Pi OS, Armbian).
  command -v apt-get >/dev/null \
    || die "missing ${NEED[*]}: install Python 3.9+ as $PY (with its standard library) and the OpenSSH client with your package manager, then run again"
  note "installing ${NEED[*]}"
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${NEED[@]}"
fi
[ -x "$PY" ] || die "$PY not found: the service runs it"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "$PY is older than 3.9 (the service runs that one)"
"$PY" -c "$STDLIB" || die "$PY lacks part of its standard library"

# --- the configuration, checked before anything changes ------------------------------------
if [ -n "$CONFIG" ]; then
  "$PY" "$HERE/check_config.py" "$CONFIG" >/dev/null || die "configuration rejected, nothing changed"
elif [ -f "$ETC/config.json" ]; then
  # A re-run without --config keeps the installed configuration: this version's checker must
  # accept it before the code is replaced (a newer checker can be stricter).
  "$PY" "$HERE/check_config.py" "$ETC/config.json" >/dev/null \
    || die "$ETC/config.json is not accepted by this version; fix it (or pass --config), nothing changed"
fi

# --- the account -------------------------------------------------------------------------
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$SVC_USER"
  note "created system user $SVC_USER"
fi

# --- the code ----------------------------------------------------------------------------
# Copied next to the old one and swapped in, so a failed copy leaves the old code working.
install -d -o root -g root -m 0755 "$OPT"
rm -rf "$OPT/wakeci.new" "$OPT/wakeci.old"
cp -R "$SRC/wakeci" "$OPT/wakeci.new"
find "$OPT/wakeci.new" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$OPT/wakeci.new"
find "$OPT/wakeci.new" -type d -exec chmod 0755 {} +
find "$OPT/wakeci.new" -type f -exec chmod 0644 {} +
# Compiled now, as root: the service may not write to /opt (ProtectSystem=strict).
"$PY" -m compileall -q "$OPT/wakeci.new" >/dev/null
[ -d "$OPT/wakeci" ] && mv "$OPT/wakeci" "$OPT/wakeci.old"
mv "$OPT/wakeci.new" "$OPT/wakeci"
rm -rf "$OPT/wakeci.old"
# The installer itself, so that tokens can be set and the install re-run later.
if [ "$SRC" != "$OPT" ]; then
  install -d -o root -g root -m 0755 "$OPT/controller"
  for f in install.sh check_config.py doctor.py wakeci.service config.example.json; do
    install -o root -g root -m 0644 "$HERE/$f" "$OPT/controller/$f"
  done
fi
if [ -n "$VERSION" ]; then
  printf '%s\n' "$VERSION" > "$OPT/VERSION"
  chmod 0644 "$OPT/VERSION"
elif [ "$SRC" != "$OPT" ]; then
  rm -f "$OPT/VERSION"  # new code of an unknown version: the old number would be wrong
fi
note "code: $OPT/wakeci ($(cat "$OPT/VERSION" 2>/dev/null || echo "no version"))"

# --- the configuration -------------------------------------------------------------------
install -d -o root -g "$SVC_USER" -m 0750 "$ETC"
if [ -n "$CONFIG" ]; then
  install -o root -g "$SVC_USER" -m 0640 "$CONFIG" "$ETC/config.json"
  note "configuration: $ETC/config.json"
fi
if [ ! -f "$ETC/config.json" ]; then
  note "no $ETC/config.json yet: run again with --config FILE (start from controller/config.example.json)"
  START=no
else
  PATHS="$("$PY" "$OPT/controller/check_config.py" "$ETC/config.json")" || die "$ETC/config.json is invalid"
  START=yes
fi

if [ "$START" = yes ]; then
  path_of() { printf '%s\n' "$PATHS" | awk -F'\t' -v k="$1" '$1 == k {print $2}'; }
  KEY="$(path_of ssh_key)"
  KNOWN="$(path_of known_hosts)"

  # check_config.py keeps both inside $ETC; a symlink there is refused, so the chown and
  # chmod below never follow one to another file.
  for f in "$KEY" "$KEY.pub" "$KNOWN"; do
    [ ! -L "$f" ] || die "$f is a symlink"
  done

  # --- SSH key for the machines (made here, never leaves this host) -----------------------
  if [ ! -f "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -C "wakeci@$(hostname)" -f "$KEY"
    note "new SSH key $KEY"
  fi
  chown -h "$SVC_USER:$SVC_USER" "$KEY"
  chmod 0600 "$KEY"
  [ -f "$KEY.pub" ] || ssh-keygen -y -f "$KEY" > "$KEY.pub"
  chmod 0644 "$KEY.pub"
  note "public key for machine/install.sh --pubkey:"
  cat "$KEY.pub"

  # --- host keys of the machines ------------------------------------------------------------
  # Trust on first use: a host is scanned only while it is not in the file. Compare the
  # printed fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the
  # machine. A sleeping machine cannot be scanned: run the installer again once it is up.
  touch "$KNOWN"
  chown -h root:"$SVC_USER" "$KNOWN"
  chmod 0644 "$KNOWN"
  while IFS= read -r host; do
    if ssh-keygen -F "$host" -f "$KNOWN" >/dev/null; then
      continue
    fi
    if SCAN="$(ssh-keyscan -T 5 -t ed25519 -- "$host" 2>/dev/null)" && [ -n "$SCAN" ]; then
      printf '%s\n' "$SCAN" >> "$KNOWN"
      note "host key of $host: $(printf '%s\n' "$SCAN" | ssh-keygen -lf - | awk '{print $2}')"
    else
      note "host $host did not answer (asleep?): its key is not known yet, it cannot be suspended"
    fi
  done < <(path_of host)

  # --- tokens ------------------------------------------------------------------------------
  while IFS= read -r token; do
    if [ -L "$token" ]; then
      note "token $token is a symlink: replace it with --set-token"
      START=no
    elif [ ! -f "$token" ]; then
      note "missing token $token: sudo bash $OPT/controller/install.sh --set-token $(basename "$token")"
      START=no
    elif ! runuser -u "$SVC_USER" -- test -r "$token"; then
      note "token $token is not readable by $SVC_USER"
      START=no
    fi
  done < <(path_of token)
  for f in "$KEY" "$KNOWN"; do
    runuser -u "$SVC_USER" -- test -r "$f" || { note "$f is not readable by $SVC_USER"; START=no; }
  done
fi

# --- less writing to an SD card (only when asked) -----------------------------------------
if [ "$RAM_JOURNAL" = yes ]; then
  install -d -m 0755 "$(dirname "$JOURNALD_DROPIN")"
  WANT_JOURNALD="$(printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n')"
  if [ "$(cat "$JOURNALD_DROPIN" 2>/dev/null)" != "$WANT_JOURNALD" ]; then
    printf '%s\n' "$WANT_JOURNALD" > "$JOURNALD_DROPIN"
    systemctl restart systemd-journald
    note "journal of the whole host kept in RAM only ($JOURNALD_DROPIN)"
  fi
elif [ -f "$JOURNALD_DROPIN" ]; then
  note "$JOURNALD_DROPIN from an earlier --ram-journal run is kept; delete it to log to disk again"
fi

# What wakeci doctor sees without the network, as the service user. Shown, never fatal:
# the lines above already say what stops the service from starting.
run_doctor() {
  echo
  note "wakeci doctor --offline (the full check: sudo -u $SVC_USER $PY $OPT/controller/doctor.py $ETC/config.json):"
  (cd / && runuser -u "$SVC_USER" -- "$PY" "$OPT/controller/doctor.py" --offline "$ETC/config.json") || true
  echo
}

# --- the service -------------------------------------------------------------------------
install -o root -g root -m 0644 "$HERE/wakeci.service" "$UNIT"
systemctl daemon-reload
systemctl enable wakeci.service >/dev/null || die "systemctl enable wakeci failed"
if [ "$START" != yes ]; then
  run_doctor
  note "service enabled, NOT started: fix the lines above and run again"
  exit 0
fi
if ! systemctl restart wakeci.service; then
  journalctl -u wakeci.service -n 20 --no-pager >&2 || true
  die "systemctl restart wakeci failed"
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  systemctl is-active --quiet wakeci.service || break
done
systemctl is-active --quiet wakeci.service || {
  journalctl -u wakeci.service -n 20 --no-pager >&2 || true
  die "wakeci did not stay up"
}
run_doctor
note "wakeci is running: journalctl -u wakeci -f"
