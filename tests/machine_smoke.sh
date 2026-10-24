#!/usr/bin/env bash
# Runs machine/install.sh for real on a throwaway Ubuntu and talks to the result through a
# real sshd, the way the controller does. CI runs it on a GitHub-hosted runner; it needs
# passwordless sudo and changes the system (users, /usr/local/sbin, /etc/sudoers.d,
# /usr/sbin/ethtool), so never run it on a real machine.
#
# It never connects without a command: that would suspend the runner. Every SSH call asks
# for a check, which wakeci-sleep answers without changing anything.
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }
mode_owner() { sudo stat -c '%a %U' "$1"; }

[ "${CI:-}" = true ] || fail "this changes the system; it runs only in CI"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"
USER_NAME=wakeci
CI_USER=runner

# --- the system around it -----------------------------------------------------------
sudo apt-get update -q >/dev/null
sudo apt-get install -y -q openssh-server >/dev/null
sudo systemctl start ssh
# A virtual runner has no Wake-on-LAN. A fake ethtool lets the installer's last step pass;
# it records what it was asked.
sudo tee /usr/sbin/ethtool >/dev/null <<'FAKE'
#!/bin/sh
if [ "$1" = -s ]; then echo "$*" >> /tmp/ethtool.calls; exit 0; fi
printf 'Settings for %s:\n\tWake-on: g\n' "$1"
FAKE
sudo chmod 0755 /usr/sbin/ethtool
# The GitHub runner runs this very script under its own Runner.Worker, so wakeci-sleep would
# always see a job. This pgrep, first in sudo's secure_path, calls the real one with the
# same arguments and leaves out only the runner's own processes (and itself).
sudo tee /usr/local/sbin/pgrep >/dev/null <<'FAKE'
#!/bin/sh
/usr/bin/pgrep -a "$@" | awk -v self="$$" '$1 != self && $0 !~ /\/bin\/Runner\.Worker/ && $0 !~ /\/usr\/local\/sbin\/pgrep/' | grep -q .
FAKE
sudo chmod 0755 /usr/local/sbin/pgrep
IFACE="$(ip route show default | awk '{print $5; exit}')"
for u in "$USER_NAME" "$CI_USER"; do
  id "$u" >/dev/null 2>&1 || sudo useradd -m -s /bin/bash "$u"
done
ssh-keygen -q -t ed25519 -N '' -C smoke -f "$W/key"
KEY="$(awk '{print $1" "$2}' "$W/key.pub")"
AUTH="$(getent passwd "$USER_NAME" | cut -d: -f6)/.ssh/authorized_keys"
install_machine() { sudo bash "$HERE/machine/install.sh" --user "$USER_NAME" --ci-user "$CI_USER" --pubkey "$W/key.pub" --iface "$IFACE"; }

# --- the installer refuses what it must ----------------------------------------------
if sudo bash "$HERE/machine/install.sh" --user "$USER_NAME" --ci-user "$USER_NAME" --pubkey "$W/key.pub" --iface "$IFACE" 2>/dev/null; then
  fail "the key was given to the CI account"
fi
sudo usermod -s /bin/false "$USER_NAME"
OUT="$(install_machine 2>&1)" && fail "a login shell the forced command cannot run in was accepted"
grep -q "login shell" <<<"$OUT" || fail "no reason for the refused shell: $OUT"
sudo usermod -s /bin/bash "$USER_NAME"
ok "refuses the CI account and a wrong login shell"

# --- install, as from scratch, over an old line and a stranger's key -----------------
sudo install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "$(dirname "$AUTH")"
# The line an older version wrote, the same key without any restriction, and someone
# else's key, with no newline at the end.
printf '%s\n%s\n%s' \
  "command=\"sudo -n /usr/local/sbin/wakeci-sleep\",restrict $KEY wakeci" \
  "$KEY added-by-hand" \
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyOtherKeyOtherKeyOtherKeyOther other" \
  | sudo tee "$AUTH" >/dev/null
OUT="$(install_machine 2>&1)" || { echo "$OUT"; fail "install failed"; }
grep -q "^ready: $USER_NAME, $IFACE (Wake-on: g)" <<<"$OUT" || fail "no ready line: $OUT"
grep -q -- "-s $IFACE wol g" /tmp/ethtool.calls || fail "Wake-on-LAN was not turned on"

[ "$(mode_owner /usr/local/sbin/wakeci-sleep)" = "755 root" ] || fail "wakeci-sleep: $(mode_owner /usr/local/sbin/wakeci-sleep)"
[ "$(mode_owner /etc/sudoers.d/wakeci-sleep)" = "440 root" ] || fail "sudoers: $(mode_owner /etc/sudoers.d/wakeci-sleep)"
[ "$(mode_owner "$AUTH")" = "600 $USER_NAME" ] || fail "authorized_keys: $(mode_owner "$AUTH")"
WANT_SUDOERS="$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/wakeci-sleep \"\"
$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/wakeci-sleep check"
[ "$(sudo cat /etc/sudoers.d/wakeci-sleep)" = "$WANT_SUDOERS" ] || fail "sudoers: $(sudo cat /etc/sudoers.d/wakeci-sleep)"
# shellcheck disable=SC2016  # the $ is meant literally: sshd's shell expands it
WANT_LINE='command="sudo -n /usr/local/sbin/wakeci-sleep ${SSH_ORIGINAL_COMMAND:+check}",restrict '"$KEY wakeci"
WANT_AUTH="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyOtherKeyOtherKeyOtherKeyOther other
$WANT_LINE"
[ "$(sudo cat "$AUTH")" = "$WANT_AUTH" ] || { sudo cat "$AUTH"; fail "authorized_keys is not the stranger's key plus one restricted line"; }
ok "installed: modes, sudoers, one restricted line, the stranger's key kept"

BEFORE="$(sudo sha256sum "$AUTH" /etc/sudoers.d/wakeci-sleep /usr/local/sbin/wakeci-sleep /etc/systemd/system/wol@.service)"
install_machine >/dev/null 2>&1 || fail "second install failed"
[ "$(sudo sha256sum "$AUTH" /etc/sudoers.d/wakeci-sleep /usr/local/sbin/wakeci-sleep /etc/systemd/system/wol@.service)" = "$BEFORE" ] \
  || fail "a second run changed something"
ok "a second run changes nothing"

# --- through a real sshd -------------------------------------------------------------
# probe [ssh options --] [remote command...]
probe() {
  local opts=()
  if [ "${1:-}" = -tt ]; then opts=(-tt); shift; fi
  set +e
  OUT="$(ssh "${opts[@]}" -i "$W/key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$USER_NAME@127.0.0.1" "$@" 2>"$W/err" </dev/null)"
  CODE=$?
  set -e
  OUT="${OUT%$'\r'}"
}
STATE_IDLE="would-suspend"
[ -f /var/run/reboot-required ] && STATE_IDLE="would-reboot"

for shell in /bin/bash /bin/sh; do
  sudo usermod -s "$shell" "$USER_NAME"
  probe check
  [ "$CODE" = 10 ] || fail "check with $shell: exit $CODE, $(cat "$W/err")"
  [ "$OUT" = "wakeci-sleep protocol=1 state=$STATE_IDLE" ] || fail "check with $shell said: $OUT"
done
sudo usermod -s /bin/bash "$USER_NAME"
ok "check answers through sshd, with bash and with sh, and changes nothing"

# Any command the client sends becomes a check; none of it runs.
rm -f /tmp/pwned
# shellcheck disable=SC2016  # sent to the remote side unexpanded on purpose
for cmd in '; touch /tmp/pwned' '$(touch /tmp/pwned)' '`touch /tmp/pwned`' 'touch /tmp/pwned' 'check; touch /tmp/pwned'; do
  probe "$cmd"
  [ "$CODE" = 10 ] || fail "client command '$cmd': exit $CODE, $(cat "$W/err")"
  [ ! -e /tmp/pwned ] || fail "client command '$cmd' ran"
done
# restrict refuses a terminal. Newer clients then stop (255); older ones run the check (10).
probe -tt check
grep -q "PTY allocation request failed" "$W/err" || fail "a terminal was not refused: exit $CODE, $(cat "$W/err")"
[ "$CODE" = 10 ] || [ "$CODE" = 255 ] || fail "with -tt: exit $CODE, $(cat "$W/err")"
ok "client commands never run"

# A running job is seen (the idle checks above show the runner's own worker is not).
bash -c 'exec -a Runner.Worker sleep 60' &
JOB=$!
sleep 1
probe check
kill "$JOB" 2>/dev/null || true
if [ "$CODE" != 10 ] || [ "$OUT" != "wakeci-sleep protocol=1 state=busy" ]; then
  fail "with a job running: exit $CODE, $OUT"
fi
ok "check sees a running job"

# sudo allows the script with no argument or with check, nothing else; and nothing else
# without a password.
for args in "suspend" "check x" "--help"; do
  # shellcheck disable=SC2086  # split on purpose
  if sudo -u "$USER_NAME" sudo -n /usr/local/sbin/wakeci-sleep $args 2>/dev/null; then
    fail "sudo allowed wakeci-sleep $args"
  fi
done
if sudo -u "$USER_NAME" sudo -n /usr/bin/id 2>/dev/null; then fail "sudo allowed another command"; fi
set +e
sudo -u "$USER_NAME" sudo -n /usr/local/sbin/wakeci-sleep check >/dev/null
CODE=$?
set -e
[ "$CODE" = 10 ] || fail "sudo wakeci-sleep check: exit $CODE"
ok "sudo allows only the two forms"

echo "machine smoke test passed"
