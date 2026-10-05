# Security policy

## Supported versions

Security fixes are developed on `main` and applied to the latest tagged
release. Older pre-1.0 tags, including release candidates, are not supported
after a newer release is published.

| Version | Supported |
| --- | --- |
| `main` | Yes |
| `v0.2.0` | Yes |
| Older tagged releases | No |

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/weavegate/weavegate/security/advisories/new)
to submit a report privately. If that channel is unavailable, email
`jaeunda@gmail.com` with the details. Please do not open a public issue or a
discussion thread for a security report.

Include what you can:

- the commit SHA you tested against,
- the steps needed to reproduce the behavior,
- what an attacker gains from it,
- any log, trace, or report output that shows it.

## Response expectations

weavegate is maintained by one person. Expect an acknowledgement within seven
days. If a report is accepted, the fix and the disclosure timing are agreed
with the reporter before the fix is published. If a report is declined, you
will get the reasoning.

## Scope

weavegate is a testing tool that runs workflows against disposable databases.
The following are in scope:

- sync-point instrumentation that can be reached or activated outside a test
  build,
- fixture, schedule, or scenario input that causes unintended commands to run
  against a database or the host,
- credentials, connection strings, or database contents leaking into reports,
  traces, or logs.

## Out of scope

Vulnerabilities in MySQL, InnoDB, Docker, Testcontainers, or any other
upstream dependency belong to those projects; please report them there.

A failing oracle, a reproduced application race, or a schedule that breaks the
workflow under test is the tool doing its job, not a vulnerability in
weavegate.
