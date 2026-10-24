# Monitoring

## Notifications

Without a `notify` block every warning goes only to the journal. With one, the controller
also posts to a webhook (a Discord or Slack channel, or anything that takes JSON):

```json
"notify": {
  "url_file": "/etc/wakeci/notify-url",
  "format": "discord",
  "after_minutes": 10,
  "repeat_hours": 6
}
```

- `url_file` holds the webhook URL. The URL is a secret, because anyone who has it can post
  to your channel. So it lives in its own file in `/etc/wakeci/`, set with
  `install.sh --set-token notify-url`, and it is never written to the log.
- `format`: `discord` (`{"content": …}`, mentions disabled), `slack` (`{"text": …}`) or
  `json` (`{"event", "key", "text", "resolved"}`).
- `after_minutes` (default 10): how long a problem must last before it is reported, so a
  short network drop stays quiet.
- `repeat_hours` (default 6): how often the message repeats while the problem lasts.

`wakeci doctor --notify-test` posts one test message.

What is sent:

| Event | When | "Resolved" message | In `dry_run` |
|---|---|---|---|
| a GitHub source cannot be read | for `after_minutes` without a break; nothing is suspended meanwhile | yes | yes |
| the GitHub request limit is nearly used up | at once; polling waits for the reset | yes | yes |
| the controller started | each start (version, machine count, `dry_run`) | — | yes |
| a machine did not wake up | after the second wake attempt | — | no (nothing is sent to it) |
| a machine failed to suspend | three times in a row | — | no (nothing is sent to it) |

Messages are posted from a separate thread. A slow or broken webhook never holds up the
control loop. A post that fails (an error answer, a rate limit, a timeout) is logged
without the URL, counted once in `wakeci_notify_failed_total` and sent again every 5
minutes, for up to 24 hours, with the time it first failed. A retry is dropped when it
has gone stale: the problem ended, or for a "resolved" message the problem came back and was
reported, or a newer message with the same subject was sent.
A redirect counts as a failure, because following it would silently drop the message.
The URL must be `https` (plain `http` only to `localhost`, for a local relay).

## Metrics

`GET /metrics` (on `metrics_listen`, by default port 9105) returns the Prometheus text
format:

| Metric | Type | Labels |
|---|---|---|
| `wakeci_machine_awake` | gauge | `machine` |
| `wakeci_machine_waking` | gauge | `machine` |
| `wakeci_actions_total` | counter | `kind`, `machine` |
| `wakeci_runner_busy_seconds_total` | counter | `runner` |
| `wakeci_queued_jobs` | gauge | `labels` |
| `wakeci_queued_oldest_seconds` | gauge | `labels` |
| `wakeci_github_ok` | gauge | `source` |
| `wakeci_github_remaining` | gauge | `source` |
| `wakeci_dry_run` | gauge | — |
| `wakeci_notify_failed_total` | counter | — |

The names are a contract, pinned by a test: a monitoring system that scrapes them breaks
without any error when a name changes, so a rename is a breaking change.

A controller that is down sends nothing: watch it from outside too, for example with an
alert on its `/metrics` being unreachable.
