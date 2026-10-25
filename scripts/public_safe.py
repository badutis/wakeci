"""Refuse values that would tie this repository to one particular installation.

The repository must be safe to make public at any time. This catches the leaks that have
a recognisable shape:

- private and link-local IPv4 addresses, and IPv6 unique-local and link-local addresses;
- MAC addresses, written with ``:`` or ``-`` or in the dotted ``xxxx.xxxx.xxxx`` form;
- email addresses other than placeholders;
- names of AI tools, as attribution.

It checks every file in the index. With ``--history`` it also checks every version of
every file in the history of ``HEAD`` and of every tag, every commit message, author and
committer in them, and every tag object (message and tagger, nested tags included).

``--rev SHA`` (with ``--history``) checks only what that one object brings: the history of
the commit, or of the commit a tag points at, plus the tag object chain. It skips the index
and the other local tags. The pre-push hook (``scripts/install-hooks.sh``) runs it for each
ref about to be pushed, so a leak is stopped before it reaches GitHub.

Allowed placeholders: the documentation address ranges (RFC 5737: 192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24; RFC 3849: 2001:db8::/32), the documentation MAC range
(RFC 7042: 00:00:5e:00:53:xx), ``example.com/.org/.net``, GitHub ``noreply`` addresses
and ``support@github.com`` (Dependabot's sign-off).

Not caught: public addresses, MAC addresses written as 12 hex digits without separators
(they cannot be told apart from hashes), UTF-16 files (read as binary), and names of real
machines, users or repositories. A public list of those would itself leak them, so keeping them out stays
the author's job.

Exit code 0 when clean, 1 with one line per finding, 2 for a usage error and 3 when git
itself fails (so the pre-push hook can tell a finding from a broken run).
"""

import argparse
import ipaddress
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from typing import Optional

# This file is scanned like any other, so nothing in it may look like a finding: the
# networks are numbers, not dotted text, and the tool names are built from pieces
# (tests/test_public_safe.py checks this). Earlier versions of this file spelled the
# patterns out; those exact versions, and only they, are skipped by content.
EARLIER_VERSIONS_OF_THIS_FILE = frozenset(
    {
        "3dcfd868f25aa5a1df3eb47402d62195e26721c3",
        "6c9a9be268bbec4b591b8a501038a5b18c3250cb",
        "941f5fc56ebf9b4ad21eb0e4cff753f15d4b6b49",
        "9e06094e2b33a1cc3fa08e590856bbb28ce56396",
        "c2d487a9b878802870a85d897c1d2f5b5a77e2c0",
        "c3d341dd8f702ac52ca005474a757bfd2d77b10c",
        "e3ecc93cea68bd98eba1e14325a425c7744fe85a",
    }
)

PRIVATE_NETS_V4 = [
    ipaddress.IPv4Network((0x0A000000, 8)),
    ipaddress.IPv4Network((0xAC100000, 12)),
    ipaddress.IPv4Network((0xC0A80000, 16)),
    ipaddress.IPv4Network((0x64400000, 10)),  # carrier-grade NAT
    ipaddress.IPv4Network((0xA9FE0000, 16)),  # link-local
]
PRIVATE_NETS_V6 = [
    ipaddress.IPv6Network((0xFC << 120, 7)),  # unique-local
    ipaddress.IPv6Network((0xFE80 << 112, 10)),  # link-local
]
# A trailing dot ends a sentence and is allowed; a dot followed by a digit is not.
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d)(?!\.\d)")
IPV6 = re.compile(
    r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f]:)(?<!::)((?:f[c-e][0-9a-f]{0,2}):[0-9a-f:]*[0-9a-f])(?![0-9a-f:])", re.I
)
# A run of six or more hex pairs: the text right before a MAC may itself look like a
# pair ("mac:fc:..."), so the run is reported unless some six-pair window of it is allowed.
# Not after a 3–4 digit group and a colon: that is the tail of an IPv6 address.
MAC_RUN = re.compile(r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f]{3}:)((?:[0-9A-Fa-f]{2}[:-]){5,}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
MAC_DOTTED = re.compile(
    r"(?<![0-9A-Fa-f.])([0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4})(?![0-9A-Fa-f])(?!\.[0-9A-Fa-f])"
)
MAC_ALLOWED = re.compile(r"^(00005e0053[0-9a-f]{2}|f{12}|0{11}[0-9a-f])$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# support@github.com: Dependabot signs its commits off with it.
EMAIL_ALLOWED = re.compile(
    r"(@users\.noreply\.github\.com|^noreply@github\.com|^support@github\.com|@example\.(com|org|net))$", re.I
)
# No letter before the name, so it is caught inside snake_case and CamelCase words too.
AI_TOOLS = ("clau" + "de", "anthro" + "pic", "chat" + "gpt", "open" + "ai", "co" + "pilot")
AI = re.compile(r"(?<![A-Za-z])(" + "|".join(AI_TOOLS) + ")", re.I)


def _ipv4(text: str) -> Optional[ipaddress.IPv4Address]:
    parts = text.split(".")
    if any(int(p) > 255 for p in parts):
        return None
    # Octets with leading zeros are refused by ipaddress, but they are still that address.
    return ipaddress.IPv4Address(".".join(str(int(p)) for p in parts))


def _mac_allowed(run: str) -> bool:
    pairs = re.split(r"[:-]", run.lower())
    return any(MAC_ALLOWED.match("".join(pairs[i : i + 6])) for i in range(len(pairs) - 5))


def findings_in_text(text: str) -> Iterator[str]:
    for m in IPV4.finditer(text):
        ip = _ipv4(m.group(1))
        if ip is not None and any(ip in net for net in PRIVATE_NETS_V4):
            yield f"private address {ip}"
    for m in IPV6.finditer(text):
        try:
            ip6 = ipaddress.IPv6Address(m.group(1))
        except ValueError:
            continue
        if any(ip6 in net for net in PRIVATE_NETS_V6):
            yield f"private address {ip6}"
    for m in MAC_RUN.finditer(text):
        if not _mac_allowed(m.group(1)):
            yield f"MAC address {m.group(1)}"
    for m in MAC_DOTTED.finditer(text):
        if not MAC_ALLOWED.match(m.group(1).replace(".", "").lower()):
            yield f"MAC address {m.group(1)}"
    for m in EMAIL.finditer(text):
        if not EMAIL_ALLOWED.search(m.group(0)):
            yield f"email address {m.group(0)}"
    for m in AI.finditer(text):
        yield f"AI attribution '{m.group(0)}'"


def scan_lines(where: str, text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        out += [f"{where}:{n}: {f}" for f in findings_in_text(line)]
    return out


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def decode(raw: bytes) -> Optional[str]:
    """Text in any encoding is read; only a file with a NUL byte counts as binary."""
    if b"\0" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def index_files() -> Iterable[tuple[str, str]]:
    # The committed content, not the working tree: a symlink is read as its target path,
    # a submodule (mode 160000) is skipped, a file deleted from disk does not crash.
    for entry in git("ls-files", "-s", "-z").decode(errors="replace").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, sha = meta.split()[:2]
        if mode == "160000":
            continue
        text = decode(git("cat-file", "blob", sha))
        if text is not None:
            yield path, text


def read_objects(shas: list[str]) -> Iterator[tuple[str, str, bytes]]:
    """(sha, type, content) for many objects in one ``git cat-file --batch`` call."""
    if not shas:
        return
    out = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="\n".join(shas).encode() + b"\n",
        check=True,
        capture_output=True,
    ).stdout
    pos = 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode(errors="replace").split()
        pos = nl + 1
        if len(header) < 3:  # "<sha> missing"
            continue
        size = int(header[2])
        yield header[0], header[1], out[pos : pos + size]
        pos += size + 1  # the content is followed by a newline


def history(revs: list[str]) -> Iterable[tuple[str, str]]:
    """Every blob and every commit reachable from ``revs`` (commits read raw: author and
    committer names and emails, and the message, whatever characters it holds). A blob
    with no path is one a tag points at directly; it is scanned too."""
    paths: dict[str, str] = {}
    for line in git("rev-list", "--objects", *revs, "--").decode(errors="replace").splitlines():
        sha, _, path = line.partition(" ")
        paths.setdefault(sha, path)
    for sha, kind, raw in read_objects(sorted(paths)):
        path = paths[sha]
        if kind == "blob" and sha not in EARLIER_VERSIONS_OF_THIS_FILE:
            text = decode(raw)
            if text is not None:
                yield f"{path or '(blob)'}@{sha[:7]}", text
        elif kind == "commit":
            yield f"commit {sha[:7]}", raw.decode(errors="replace")


def tag_chain(sha: str, seen: set, limit: Optional[int] = None) -> Iterable[tuple[str, str]]:
    """Every tag object from ``sha`` down to what it finally points at (tags can be nested).
    The raw object holds the tag name, the tagger and the message. Objects in ``seen`` are
    not read again. With ``limit``, a longer chain is an error rather than a silent stop."""
    depth = 0
    while sha not in seen:
        seen.add(sha)
        kind, raw = next(((k, r) for _, k, r in read_objects([sha])), ("missing", b""))
        if kind != "tag":
            return
        depth += 1
        if limit is not None and depth > limit:
            raise RuntimeError(f"tag chain longer than {limit} objects at {sha[:7]}")
        text = raw.decode(errors="replace")
        yield f"tag object {sha[:7]}", text
        target = re.search(r"^object ([0-9a-f]+)$", text, re.M)
        if not target:
            return
        sha = target.group(1)


def tag_refs() -> list[tuple[str, str]]:
    out = git("for-each-ref", "refs/tags", "--format=%(objectname) %(refname:short)").decode(errors="replace")
    return [(sha, name) for sha, _, name in (line.partition(" ") for line in out.splitlines()) if name]


def tags(refs: list[tuple[str, str]]) -> Iterable[tuple[str, str]]:
    """The tag objects of every local tag, nested ones included, each read once."""
    seen: set = set()
    for sha, name in refs:
        for where, text in tag_chain(sha, seen):
            yield f"{where} ({name})", text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true", help="also scan the history of HEAD and the tags")
    ap.add_argument(
        "--rev",
        help="with --history: scan the history of this commit instead of HEAD, and skip the index"
        " (the pre-push hook passes each commit it is about to push)",
    )
    args = ap.parse_args(argv)
    if args.rev and not args.history:
        ap.error("--rev needs --history")
    if args.rev and not re.match(r"^[0-9a-f]{7,64}$", args.rev):
        ap.error("--rev must be a commit id")
    found: list[str] = []
    try:
        if not args.rev:
            for where, text in index_files():
                found += scan_lines(where, text)
        if args.history:
            if args.rev:
                sources = (tag_chain(args.rev, set(), limit=100), history([args.rev]))
            else:
                refs = tag_refs()
                sources = (history(["HEAD"] + [sha for sha, _ in refs]), tags(refs))
            for source in sources:
                for where, text in source:
                    found += scan_lines(where, text)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace").strip()
        print(f"git failed: {' '.join(e.cmd)}: {err}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — a crash must never read as "findings"
        print(f"check failed: {e!r}", file=sys.stderr)
        return 3
    for f in found:
        print(f)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
