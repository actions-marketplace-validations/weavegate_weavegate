# ADR 0011: Report Markdown rendering boundary

- Status: Accepted
- Date: 2026-09-10
- Issue: [#43](https://github.com/weavegate/weavegate/issues/43)

## Context

`report.md` combines values from configuration, schedules, diagnostics, and the
CLI-generated replay command. Those values previously had different safety
rules at their production points. Diagnostic row keys and values were escaped
while `observed` was built, but a scenario name and the complete replay command
were inserted into Markdown unchanged. A newline in a config path could
therefore split `replay:` and forge another report line. Printable Markdown
delimiters could also change how a report is interpreted when embedded in a
GitHub comment.

Producer-specific escaping does not define a complete report contract. It also
changes structured diagnostic data for the needs of one presentation. The
boundary must cover every current and future string rendered into Markdown
while leaving `scenario.json`, `observation.json`, and `report.json` as
structured evidence.

The replay command creates an additional conflict. POSIX single quotes preserve
a literal newline in an argument, so the command remains shell-correct only by
containing that newline. A field cannot both preserve those exact bytes and
remain one physical report line.

## Decision

`internal/report` is the single safety boundary for `report.md`. Renderers pass
the fixed Markdown shape and each runtime value separately to one line writer.
The writer escapes every string value before interpolation and appends the line
terminator itself.

- Every non-printable rune uses its Go escape spelling, such as `\n`, `\t`,
  `\x1b`, or `\u200b`. Each malformed UTF-8 byte uses `\xNN` rather than
  collapsing to U+FFFD.
- Markdown delimiters that can create inline markup, raw HTML, tables, or GitHub
  references are backslash-escaped. Intraword underscores remain unchanged;
  delimiter underscores and a leading list marker are escaped. Dollar signs use
  `\x24`, because GitHub interprets paired dollar signs as math even when they
  are backslash-escaped or written as HTML entities.
- Structured report values are not escaped for Markdown. JSON encoding
  provides the JSON artifact's own control-character representation and
  replaces malformed UTF-8 bytes with U+FFFD.

Ordinary replay commands contain no characters changed by this boundary and
remain pasteable verbatim. If a command contains a control character or
Markdown delimiter that the boundary changes, its `replay:` line is an
audit-safe representation, not a pasteable shell command. The user must rerun
with the original argument values. One-line and Markdown safety take precedence
over unconditional pasteability.

This decision narrows the unconditional pasteability language in ADR 0009. It
does not change ADR 0005's determinism boundary: escaping is a pure function of
the deterministic input values, and a config path is still not normalized to an
execution-specific absolute path.

## Consequences

- `observed:`, `scenario:`, `replay:`, and every other rendered string share
  one rule, including strings added to diagnostic blocks later.
- A runtime value cannot add a physical line, emit a terminal-control rune, or
  introduce Markdown structure into a report or a comment that embeds it.
- Normal report output and normal replay commands keep their existing bytes.
- The Markdown artifact can differ textually from its structured JSON source
  when safety requires an escape. JSON retains valid UTF-8 values; malformed
  UTF-8 bytes survive only as `\xNN` escapes in the Markdown display.
- A table-driven renderer test enumerates every variable field currently emitted
  into `report.md` and applies the same newline, terminal-control, and Markdown
  payload to each string field.

## What didn't work

1. **Escape at each producer.** This left new fields safe only when their
   producer remembered the Markdown sink and made structured data depend on a
   presentation format.
2. **Keep literal newlines in shell-quoted arguments.** This preserved replay
   semantics but allowed a value to forge report lines and break an enclosing
   Markdown context.
3. **Use a shell-specific newline escape.** `$'\n'` is not POSIX shell syntax,
   and command substitution has trailing-newline and quoting behavior that does
   not preserve every possible path byte.
4. **Reject unusual paths or scenario names.** Input validation is not a
   rendering boundary and would impose filesystem and configuration restrictions
   solely to protect one output format.
