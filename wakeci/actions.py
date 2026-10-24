"""Actions on machines: the Wake-on-LAN packet and suspending over SSH."""

import socket
import subprocess
from typing import Callable

from .decide import Machine


def magic_packet(mac: str) -> bytes:
    hexes = mac.replace(":", "").replace("-", "").lower()
    if len(hexes) != 12 or any(c not in "0123456789abcdef" for c in hexes):
        raise ValueError(f"invalid MAC: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(hexes) * 16


def wake(machine: Machine, broadcast: str = "255.255.255.255", port: int = 9, sock: Callable = socket.socket) -> None:
    packet = magic_packet(machine.mac)
    with sock(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))


# Exit codes of `wakeci-sleep` (machine/wakeci-sleep). It schedules the suspend a few seconds
# ahead and exits first, so the SSH session ends cleanly instead of being cut off.
# A result that starts with one of SLEEP_OK means the machine is going down.
# SLEEP_CHECKED answers `wakeci-sleep check`, which the controller never sends.
SLEEP_OK = ("suspends", "reboots")
SLEEP_CHECKED = 10
SLEEP_EXIT = {
    0: "suspends in 5 s",
    2: "refused: wakeci-sleep was given an argument it does not know",
    3: "refused: a job is running on the machine",
    4: "reboots in 5 s: an update asked for a reboot",
    SLEEP_CHECKED: "answered a check and did not suspend",
}


def ssh_sleep_command(machine: Machine, key_path: str, known_hosts: str) -> list[str]:
    # The machine's authorized_keys restricts this key to one command (`command=`), so no
    # command is passed here: the machine decides what runs, not the controller.
    return [
        "ssh",
        "-i",
        key_path,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        f"{machine.ssh_user}@{machine.host}",
    ]


def ssh_check_command(machine: Machine, key_path: str, known_hosts: str) -> list[str]:
    """Asks ``wakeci-sleep check`` (wakeci doctor --probe). Whatever the client sends, the
    machine's forced command only adds the word "check"; a machine set up before 0.5.0
    ignores it and suspends."""
    return [*ssh_sleep_command(machine, key_path, known_hosts), "check"]


def put_to_sleep(machine: Machine, key_path: str, known_hosts: str, run: Callable = subprocess.run) -> str:
    try:
        done = run(ssh_sleep_command(machine, key_path, known_hosts), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "SSH did not answer within 30 s"
    if done.returncode == 255:
        return f"SSH failed: {done.stderr.strip()[:200]}"
    return SLEEP_EXIT.get(done.returncode, f"unknown exit code {done.returncode}")
