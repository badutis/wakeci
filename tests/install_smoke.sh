#!/usr/bin/env bash
# Installs the controller from scratch on a disposable Linux machine with systemd and
# checks the result. CI runs it on a GitHub-hosted runner; it needs passwordless sudo and
# changes the system (user, /opt, /etc, a systemd unit), so do not run it on a real host.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
mode_owner() { sudo stat -c '%a %U' "$1"; }

W="$(mktemp -d)"
HOOK_PID=""
trap '[ -z "$HOOK_PID" ] || kill "$HOOK_PID" 2>/dev/null; rm -rf "$W"' EXIT

step "webhook receiver (stands in for Discord)"
# Appends every POST body to $W/hook.log, one per line.
python3 - "$W/hook.log" <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
out = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(out, "ab") as f:
            f.write(body + b"\n")
        self.send_response(204)
        self.end_headers()
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", 9199), H).serve_forever()
PY
HOOK_PID=$!

step "bundle"
controller/deploy.sh --bundle-only "$W/b.tar.gz" HEAD controller/config.example.json
mkdir "$W/x"
tar -xzf "$W/b.tar.gz" -C "$W/x"

step "first install: no tokens yet, so the service must not start"
OUT="$(sudo bash "$W/x/controller/install.sh" --config "$W/x/config.json" --version smoke 2>&1)" \
  || { echo "$OUT"; fail "first install failed"; }
echo "$OUT"
grep -q "NOT started" <<<"$OUT" || fail "service started without tokens"
grep -q "missing token /etc/wakeci/token-example-org:" <<<"$OUT" || fail "missing token not reported"
grep -q "missing token /etc/wakeci/notify-url:" <<<"$OUT" || fail "missing webhook URL not reported"
grep -q "^FAIL  files         /etc/wakeci/token-example-org is missing" <<<"$OUT" || fail "doctor did not run or missed the token"
[ -f /opt/wakeci/controller/doctor.py ] || fail "doctor.py not kept in /opt/wakeci/controller"
id wakeci >/dev/null || fail "no wakeci user"
[ "$(getent passwd wakeci | cut -d: -f7)" = /usr/sbin/nologin ] || fail "wakeci can log in"
[ "$(mode_owner /etc/wakeci)" = "750 root" ] || fail "/etc/wakeci: $(mode_owner /etc/wakeci)"
[ "$(mode_owner /etc/wakeci/id_ed25519)" = "600 wakeci" ] || fail "key: $(mode_owner /etc/wakeci/id_ed25519)"
[ "$(mode_owner /etc/wakeci/config.json)" = "640 root" ] || fail "config: $(mode_owner /etc/wakeci/config.json)"
[ -f /opt/wakeci/wakeci/__main__.py ] || fail "package not at /opt/wakeci/wakeci"
[ -f /opt/wakeci/controller/install.sh ] || fail "installer not kept in /opt/wakeci/controller"
[ "$(cat /opt/wakeci/VERSION)" = smoke ] || fail "VERSION"
[ ! -e /etc/systemd/journald.conf.d/50-wakeci-volatile.conf ] || fail "journal changed without --ram-journal"
systemctl is-enabled --quiet wakeci || fail "service not enabled"
! systemctl is-active --quiet wakeci || fail "service active without tokens"
FP1="$(sudo ssh-keygen -lf /etc/wakeci/id_ed25519)"  # the private key itself

step "tokens"
if printf '\n' | sudo bash /opt/wakeci/controller/install.sh --set-token token-empty; then
  fail "empty token accepted"
fi
for bad in ../escape .. config.json id_ed25519 known_hosts id_ed25519.pub; do
  if printf 'x\n' | sudo bash /opt/wakeci/controller/install.sh --set-token "$bad"; then
    fail "token name $bad accepted"
  fi
done
[ "$(mode_owner /etc/wakeci/config.json)" = "640 root" ] || fail "config.json touched by --set-token"
printf 'not-a-real-token\n' | sudo bash /opt/wakeci/controller/install.sh --set-token token-example-org
printf '  not-a-real-token-2 \n' | sudo bash /opt/wakeci/controller/install.sh --set-token token-example-org2
printf 'http://127.0.0.1:9199/hook\n' | sudo bash /opt/wakeci/controller/install.sh --set-token notify-url
[ "$(mode_owner /etc/wakeci/token-example-org)" = "600 wakeci" ] || fail "token mode"
[ "$(sudo cat /etc/wakeci/token-example-org2)" = not-a-real-token-2 ] || fail "token not trimmed"

step "second install, in place, with --ram-journal: starts, keeps the key"
OUT="$(sudo bash /opt/wakeci/controller/install.sh --ram-journal 2>&1)" || { echo "$OUT"; fail "second install failed"; }
echo "$OUT"
grep -q "wakeci is running" <<<"$OUT" || fail "service did not start"
# doctor --offline as the service user: everything local is fine except the host keys of
# the example's machines, which do not exist here.
grep -q "^OK    files" <<<"$OUT" || fail "doctor: files"
grep -q "^OK    ssh-key" <<<"$OUT" || fail "doctor: ssh-key"
grep -q "^WARN  metrics       0.0.0.0:9105 is in use" <<<"$OUT" || fail "doctor: metrics"
# The example's 192.0.2.255 is no interface's here; the line lists the interfaces that
# `ip -j` reported, so it shows that they were read.
grep -qE '^WARN  wol           192\.0\.2\.255 is not the broadcast address of any interface here \([a-z0-9]+ [0-9.]+' <<<"$OUT" \
  || fail "doctor: wol did not read the interfaces"
if [ "$(grep -c '^FAIL  ' <<<"$OUT")" != 1 ] || ! grep -q "^FAIL  host-key" <<<"$OUT"; then
  fail "doctor: expected only the host-key failure"
fi
[ "$(sudo ssh-keygen -lf /etc/wakeci/id_ed25519)" = "$FP1" ] || fail "SSH key was replaced"
[ "$(cat /opt/wakeci/VERSION)" = smoke ] || fail "VERSION lost on an in-place run"
grep -qx 'Storage=volatile' /etc/systemd/journald.conf.d/50-wakeci-volatile.conf || fail "--ram-journal had no effect"
grep -q "host: .*, $(uname -m), " <<<"$OUT" || fail "host line missing"

step "start message reached the webhook"
for _ in $(seq 1 20); do
  grep -q 'wakeci smoke started' "$W/hook.log" 2>/dev/null && break
  sleep 1
done
cat "$W/hook.log" 2>/dev/null || true
grep -q '"content": "wakeci smoke started' "$W/hook.log" || fail "no start message on the webhook"
grep -q '"allowed_mentions"' "$W/hook.log" || fail "Discord message without allowed_mentions"

step "metrics"
for _ in $(seq 1 30); do
  if M="$(curl -fsS http://127.0.0.1:9105/metrics 2>/dev/null)" && grep -q '^wakeci_dry_run 1$' <<<"$M"; then
    echo "$M"
    grep -q '^wakeci_notify_failed_total 0$' <<<"$M" || fail "webhook posts failed"
    echo; echo "OK"
    exit 0
  fi
  sleep 1
done
sudo journalctl -u wakeci -n 30 --no-pager || true
fail "no /metrics with wakeci_dry_run 1"
