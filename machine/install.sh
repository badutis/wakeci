#!/usr/bin/env bash
# Prepares one CI machine for the controller: wakeci-sleep, a sudoers line, the controller's
# restricted key, Wake-on-LAN. Run on the machine itself:
#
#   sudo bash install.sh --user wakeci --ci-user runner --pubkey /tmp/wakeci.pub [--iface eno1]
#
# --ci-user is the account that runs CI jobs: it never gets the key.
# Running it again duplicates nothing. Wake-on-LAN is enabled in the BIOS by hand
# (docs/design.md).
set -euo pipefail

USER_NAME=""
CI_USER=""
PUBKEY=""
IFACE="eno1"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    --ci-user) CI_USER="$2"; shift 2 ;;
    --pubkey) PUBKEY="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -n "$USER_NAME" ] || die "--user is missing"
id "$USER_NAME" >/dev/null 2>&1 || die "--user must be an existing account"
[ -n "$CI_USER" ] || die "--ci-user is missing (the account that runs CI jobs)"
id "$CI_USER" >/dev/null 2>&1 || die "--ci-user must be an existing account (the one that runs CI jobs)"
# Compared by UID: one user can have several names.
[ "$(id -u "$USER_NAME")" != "$(id -u "$CI_USER")" ] || die "--user cannot be $CI_USER: that account runs CI code"
# sshd runs the forced command (step 3) with this account's login shell.
USER_SHELL="$(getent passwd "$USER_NAME" | cut -d: -f7)"
case "$(basename "$USER_SHELL")" in
  sh|dash|bash) ;;
  *) die "--user $USER_NAME has the login shell $USER_SHELL: give it /bin/sh or /bin/bash (usermod -s /bin/sh $USER_NAME)" ;;
esac
[ -f "$PUBKEY" ] || die "--pubkey file not found"
[ "$(grep -c . "$PUBKEY")" -eq 1 ] || die "--pubkey file must hold exactly one key"
grep -qE '^ssh-(ed25519|rsa) ' "$PUBKEY" || die "--pubkey is not an SSH public key"
[ -e "/sys/class/net/$IFACE" ] || die "no interface $IFACE"
HERE="$(cd "$(dirname "$0")" && pwd)"

# 1. The suspend command.
install -o root -g root -m 0755 "$HERE/wakeci-sleep" /usr/local/sbin/wakeci-sleep

# 2. sudo without a password, for this command only, with no argument or with "check".
# Checked before it is put in place.
SUDOERS_TMP="$(mktemp)"
{
  echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/wakeci-sleep \"\""
  echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/wakeci-sleep check"
} > "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP" >/dev/null || die "invalid sudoers line"
install -o root -g root -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/wakeci-sleep
rm -f "$SUDOERS_TMP"

# 3. The controller's key, restricted to one command. A command the client sends (sshd
# puts it in SSH_ORIGINAL_COMMAND) never runs: any non-empty one only adds the literal
# word "check", so the client can ask what would happen instead of suspending.
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "$HOME_DIR/.ssh"
AUTH="$HOME_DIR/.ssh/authorized_keys"
touch "$AUTH"
chown "$USER_NAME:$USER_NAME" "$AUTH"
chmod 0600 "$AUTH"
KEY="$(awk '{print $1" "$2}' "$PUBKEY")"
WANT="command=\"sudo -n /usr/local/sbin/wakeci-sleep \${SSH_ORIGINAL_COMMAND:+check}\",restrict $KEY wakeci"
# Any other line with the same key (say, added by hand while testing, without the
# restriction) is removed: this key must never give a full shell.
FILTERED="$(mktemp)"
grep -vF "$KEY" "$AUTH" > "$FILTERED" || true
# A last line without a newline would glue our line onto someone else's key.
if [ -s "$FILTERED" ] && [ -n "$(tail -c1 "$FILTERED")" ]; then
  echo >> "$FILTERED"
fi
echo "$WANT" >> "$FILTERED"
if ! cmp -s "$FILTERED" "$AUTH"; then
  cat "$FILTERED" > "$AUTH"
fi
rm -f "$FILTERED"

# 4. Wake-on-LAN on every boot (the setting is lost on reboot).
command -v ethtool >/dev/null || apt-get install -y -q ethtool
cat > /etc/systemd/system/wol@.service <<'UNIT'
[Unit]
Description=Wake-on-LAN magic packet on %i
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ethtool -s %i wol g

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "wol@$IFACE.service" >/dev/null
ethtool "$IFACE" | grep -qE '^[[:space:]]*Wake-on:[[:space:]]*g$' || die "Wake-on-LAN did not turn on ($IFACE)"

echo "ready: $USER_NAME, $IFACE (Wake-on: g)"
