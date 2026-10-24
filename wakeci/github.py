"""Reading the GitHub API: runners and queued jobs. GET only, outbound connections only.

The request limit (5,000 per hour per token) is protected in three ways:

- every response is kept with its ETag; an unchanged one returns 304, which does not
  count against the limit;
- jobs of an in-progress run are re-read not every poll but every `IN_PROGRESS_REFRESH`
  (that response changes all the time, so ETags do not help there), and every
  `IN_PROGRESS_REFRESH_LOW` when the remaining limit is low;
- when the limit is nearly used up, `backoff_until` tells the loop to wait for the reset.

Each source is read on its own: an error in one (an expired token) does not blind the
other, but the observation is marked incomplete, and then nothing is suspended (decide.py).
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .decide import Observation, QueuedJob, Runner

API = "https://api.github.com"
IN_PROGRESS_REFRESH = timedelta(seconds=60)
IN_PROGRESS_REFRESH_LOW = timedelta(minutes=5)
LOW_REMAINING = 1000
STOP_REMAINING = 100


@dataclass(frozen=True)
class Source:
    """One place where runners are registered, and the repositories whose jobs use them."""

    runners_path: str  # e.g. "repos/example-org/app" or "orgs/example-org2"
    repos: tuple[str, ...]  # e.g. ("example-org/app",)
    token: str


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHub:
    def __init__(self, sources: list[Source], opener=None, clock=None):
        self.sources = sources
        self._open = opener or urllib.request.urlopen
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, tuple[str, object]] = {}
        self._touched: set = set()
        # run's jobs URL -> (when it was read, the run's updated_at, queued jobs)
        self._run_jobs: dict[str, tuple[datetime, str, list[QueuedJob]]] = {}
        self._kept: set = set()
        self.remaining: dict[str, int] = {}  # source -> remaining limit
        self.reset_at: dict[str, datetime] = {}
        self.errors: dict[str, str] = {}  # source -> last error

    # --- requests ----------------------------------------------------------------------

    def _note_limit(self, source: str, headers) -> None:
        if headers is None:
            return
        rem = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if rem is not None and str(rem).isdigit():
            self.remaining[source] = int(rem)
        if reset is not None and str(reset).isdigit():
            self.reset_at[source] = datetime.fromtimestamp(int(reset), timezone.utc)

    def _get(self, path: str, src: Source) -> object:
        url = f"{API}/{path}"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {src.token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        self._touched.add(url)
        cached = self._cache.get(url)
        if cached:
            req.add_header("If-None-Match", cached[0])
        try:
            with self._open(req, timeout=15) as resp:
                self._note_limit(src.runners_path, resp.headers)
                body = json.loads(resp.read().decode("utf-8"))
                etag = resp.headers.get("ETag")
                if etag:
                    self._cache[url] = (etag, body)
                return body
        except urllib.error.HTTPError as e:
            self._note_limit(src.runners_path, e.headers)
            if e.code == 304 and cached:
                return cached[1]
            raise

    def _pages(self, path: str, key: str, src: Source) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            body = self._get(f"{path}{sep}per_page=100&page={page}", src)
            items = body.get(key, []) if isinstance(body, dict) else []
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    def runners(self, src: Source) -> list[dict]:
        """Every runner registered at the source, as GitHub returns it."""
        return self._pages(f"{src.runners_path}/actions/runners", "runners", src)

    # --- observation -------------------------------------------------------------------

    def backoff_until(self) -> Optional[datetime]:
        """Until when to stop asking: some source's limit is nearly used up."""
        latest = None
        for source, rem in self.remaining.items():
            if rem < STOP_REMAINING and source in self.reset_at and (latest is None or self.reset_at[source] > latest):
                latest = self.reset_at[source]
        return latest

    def observe(self) -> Observation:
        self._touched = set()
        self._kept = set()
        runners: list[Runner] = []
        queued: list[QueuedJob] = []
        complete = True
        for src in self.sources:
            try:
                got_runners = [
                    Runner(
                        name=r["name"],
                        online=r.get("status") == "online",
                        busy=bool(r.get("busy")),
                        labels=frozenset(lab["name"] for lab in r.get("labels", [])),
                        source=src.runners_path,
                    )
                    for r in self.runners(src)
                ]
                got_jobs: list[QueuedJob] = []
                for repo in src.repos:
                    got_jobs.extend(self._queued_jobs(repo, src))
            except Exception as e:  # noqa: BLE001 — any error of one source stays with that source
                complete = False
                self.errors[src.runners_path] = first_error(e) or repr(e)
                continue
            self.errors.pop(src.runners_path, None)
            runners.extend(got_runners)
            queued.extend(got_jobs)
        # Responses of finished runs are no longer needed; without this, memory would grow
        # without bound. The ETag of a run taken from memory is kept: otherwise its next
        # re-read would go without If-None-Match and count against the limit unchanged.
        kept_urls = tuple(f"{API}/{p}?" for p in self._kept)
        self._cache = {k: v for k, v in self._cache.items() if k in self._touched or k.startswith(kept_urls)}
        self._run_jobs = {k: v for k, v in self._run_jobs.items() if k in self._kept}
        return Observation(runners=tuple(runners), queued=tuple(queued), complete=complete)

    def _queued_jobs(self, repo: str, src: Source) -> list[QueuedJob]:
        out: list[QueuedJob] = []
        now = self._now()
        low = self.remaining.get(src.runners_path, LOW_REMAINING) < LOW_REMAINING
        refresh = IN_PROGRESS_REFRESH_LOW if low else IN_PROGRESS_REFRESH
        # Queued work can be in a run that has not started (`queued`) and in one that is
        # already going (`in_progress`) when some of its jobs already have a runner.
        for status in ("queued", "in_progress"):
            runs = self._pages(f"repos/{repo}/actions/runs?status={status}", "workflow_runs", src)
            for run in runs:
                path = f"repos/{repo}/actions/runs/{run['id']}/jobs"
                seen = self._run_jobs.get(path)
                updated = str(run.get("updated_at") or "")
                # The old list may be used only while the run has not changed since: otherwise
                # a job a runner has just taken would still look queued, and the controller
                # would wake another machine for nothing.
                if status == "in_progress" and seen and seen[1] == updated and now - seen[0] < refresh:
                    self._kept.add(path)
                    out.extend(seen[2])
                    continue
                jobs = []
                for job in self._pages(path, "jobs", src):
                    labels = frozenset(lab.lower() for lab in job.get("labels", []))
                    if job.get("status") == "queued" and "self-hosted" in labels:
                        jobs.append(
                            QueuedJob(
                                labels=labels,
                                since=parse_time(job.get("created_at")),
                                source=src.runners_path,
                                id=job.get("id"),
                            )
                        )
                self._run_jobs[path] = (now, updated if status == "in_progress" else "", jobs)
                self._kept.add(path)
                out.extend(jobs)
        return out


def read_token(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise ValueError(f"empty token: {path}")
    return token


def first_error(exc: BaseException) -> Optional[str]:
    if isinstance(exc, urllib.error.HTTPError):
        return f"GitHub {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"network: {exc.reason}"
    return None
