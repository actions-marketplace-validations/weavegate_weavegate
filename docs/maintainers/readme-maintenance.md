# README editorial and visual maintenance

This working note is included in the public repository for maintainers updating
the repository README without rewriting unaffected sections. It records the
strategy used for the current README and the contract between its prose,
measurements, commands, and four SVGs.

## Reader outcome

The first screen should answer two questions:

1. What is weavegate for? It deterministically reproduces application-level
   database races and retains evidence that can be replayed against a fix.
2. How is it built? Fixtures declare data, an adapter exposes sync-points, the
   orchestrator controls schedules, and composable SQL oracles own verdicts.

Optimize for those outcomes rather than exhaustive feature coverage. Keep the
README at no more than **100 lines and 6 KiB**; move detail into the existing
`docs/` page that owns it.

## Stable reading order

Do not add a separate “The problem” section. Preserve this order:

| README block | Job | Visual |
| --- | --- | --- |
| Header | Logo, tagline, status badges, in-page navigation | Existing logo |
| Overview | Three-sentence product explanation, overview graphic, Find/Force/Prove bullets | `assets/readme/problem.svg` |
| Results | Measured evidence and its boundary | `assets/readme/results.svg` |
| How it works | Engine boundaries and four execution stages | `assets/readme/how-it-works.svg` |
| Run the CLI | Copyable command, expected verdict, input/output/replay | `assets/readme/run-cli.svg` |
| Contributing / License | Contribution, support, release, attribution, license routes | None |

The centered navigation links to `#overview`, `#results`, `#how-it-works`, and
`#run-the-cli`. Update a label and its target together. The overview anchor is
explicit because that block deliberately has no heading.

## Content contracts

### Overview

Keep exactly one three-sentence paragraph in this order:

1. product category, target problem, and application-level boundary;
2. sync-point exploration, real database execution, and rule-based judgment;
3. inputs and retained outputs, including replay against the fix.

Follow it with the overview graphic and three bullets labeled **Find the
order**, **Force the race**, and **Prove the outcome**. Capability caveats end
the block as a link to `docs/limitations.md` rather than a negative list.

### Results

The SVG and table are two representations of the same measurements; change
them together. Every quantitative table row links directly to its experiment:

- replay and controls: `docs/experiments/baseline-comparison.md#measured-result`;
- candidates, saved schedule, stability, and runtime:
  `docs/experiments/exploration.md#reproduction`;
- fixed-variant replay when needed:
  `docs/experiments/determinism.md#repeated-result`.
- Spring Boot external JVM replay:
  `docs/experiments/spring-replay.md#repeated-paired-test`.

Retain an evidence-boundary row in both the SVG and table. Never turn a fixture
measurement into a general detection rate, benchmark, or performance claim.

### How it works

Describe the implementation through its boundaries, not a feature inventory:

1. **Declare:** fixture, config, scenario, migration, seed, and SQL assertions;
2. **Orchestrate:** candidate enumeration, release, replay, and repeat;
3. **Execute:** current adapter, per-worker connections, and real database;
4. **Judge and retain:** oracle verdict, schedule, trace, observations, report,
   and exit code.

Verdict logic remains in oracles. Planned adapters or integrations must not
appear as current stages.

### Run the CLI

Keep one copyable command block. Verify it against `docs/reference/cli.md` and
the matching fixture before changing the SVG. Failure output uses the canonical
structure from `docs/reference/diagnostics/WG001.md`; constructed visuals must
not imply that omitted or rearranged output is a captured terminal session.
Keep the pre-release and Docker requirements immediately beside the example.

## Source-of-truth map

| Changed fact | Authoritative source | README surfaces to inspect |
| --- | --- | --- |
| Capability or boundary | Current code, `docs/limitations.md`, `AGENTS.md` | Overview prose, bullets, overview SVG |
| Experiment result | The relevant file under `docs/experiments/` | Results SVG and table |
| Architecture or artifact | Current packages, `docs/architecture.md`, report schema | How-it-works SVG and numbered list; overview outputs if affected |
| CLI flag or output | `docs/reference/cli.md`, diagnostic page, real output | Run SVG, command block, Quickstart link text |
| Runtime prerequisite | `go.mod`, fixture config, `CONTRIBUTING.md` | Run callout and SVG |
| Release status | `CHANGELOG.md` and GitHub Releases | Badges and footer links |
| Coverage badge | Generated coverage evidence described in `CONTRIBUTING.md` | Badge only; never hand-edit the number |
| Brand rule | `weavegate/design-reference` | Only the affected SVGs or logo asset |

## Visual contract

The canonical source is `weavegate/design-reference`; the current SVG set was
aligned against revision `5e2035f6eb11a166f12f7992b9f36705c9df8500`.
Compare with the latest design-reference revision before adopting later brand
changes rather than treating this hash as a permanent pin.

- Use IBM Plex Sans for prose/UI and JetBrains Mono for commands and evidence,
  with system fallbacks embedded in every SVG.
- Use ink `#0d1117`, paper `#f8f9fa`/white, slate rules, and weave teal
  `#0b6e6a`/`#0f8b86` as the single brand accent.
- Reserve red `#d1242f` for FAIL, green `#1a7f37` for PASS, amber `#9a6700`
  for blocked/pre-release, and blue `#0969da` for worker B or informational
  sync-point control.
- Solid teal arrows mean configuration, execution, data, or evidence flow.
  Dashed blue arrows mean sync-point execution-order control.
- Use off-white planes, hairline borders, compact cards, radii of 5 px or less,
  and no gradients, shadows, oversized pills, or decorative color.
- Commands, machine values, schedule IDs, diagnostics, and artifacts are the
  focal evidence—not decorative icons.
- Preserve each SVG’s `role="img"`, `<title>`, `<desc>`, 1200 px viewBox width,
  semantic color meaning, and readable fallback fonts.

### SVG ownership

| Asset | Owns | Update when |
| --- | --- | --- |
| `problem.svg` | Race → controlled schedule → rule-based verdict story | Product boundary, primary diagnostic, retained evidence, or overview wording changes |
| `results.svg` | Replay, control, exploration metrics and evidence boundary | Any displayed experiment is rerun or its boundary changes |
| `how-it-works.svg` | Declare → orchestrate → execute → judge/retain architecture | Package responsibility, adapter, database, oracle, or artifact changes |
| `run-cli.svg` | Canonical command → controlled run → diagnostic/evidence flow | CLI invocation, prerequisite, diagnostic structure, exit code, or example scenario changes |

Do not regenerate all four assets for a one-surface fact change. Preserve
unaffected geometry and copy so diffs remain reviewable.

## Partial-update matrix

| Product change | Required README change |
| --- | --- |
| New measured result | Update the experiment first, then the Results SVG and matching row; keep the boundary |
| New current adapter | Update Overview only if the product boundary changes; update Execute in prose and architecture SVG |
| New database fixture | Update Results only when measured; update Run only if it becomes the canonical example |
| New diagnostic | Add its reference page first; change Run only if it replaces the canonical example |
| CLI flag or command rename | Update CLI reference and Quickstart, then the command block and Run SVG in the same change |
| Artifact added or removed | Update report schema, then How it works; update Overview only if it is a primary user output |
| Schedule semantics change | Update architecture/limitations first, then Overview and How-it-works SVGs |
| Brand-token change | Apply only the changed token or semantic rule across affected SVGs; do not alter product copy |
| Planned feature | Keep it out of the product flow; link milestones or label it explicitly planned in detailed docs |

## Update procedure

1. Classify the change using the source-of-truth and partial-update tables.
2. Reproduce or verify the changed fact in its authoritative document or code.
3. Edit the smallest set of README prose, table rows, and SVG-owned surfaces.
4. Keep values duplicated between prose and SVG byte-for-byte consistent.
5. Render every changed SVG; inspect at the GitHub README display width as well
   as its native canvas.
6. Run the checks below and record which Docker-backed checks were skipped.

```bash
xmllint --noout assets/readme/*.svg
wc -l -c README.md
git diff --check
google-chrome --headless --no-sandbox --disable-gpu --hide-scrollbars \
  --screenshot=/tmp/weavegate-readme-preview.png --window-size=1200,520 \
  file://$PWD/assets/readme/how-it-works.svg
```

The README check must report at most 100 lines and 6144 bytes. Also verify every
relative link target exists and every centered navigation anchor resolves.

## Review checklist

- [ ] A first-time reader can state what problem weavegate addresses.
- [ ] The Overview explains what goes in, how judgment happens, and what comes out.
- [ ] Results in prose, SVG, and experiment documents agree exactly.
- [ ] The architecture still reflects current package and verdict boundaries.
- [ ] The command is copyable and the displayed diagnostic is current.
- [ ] Planned work is absent or explicitly labeled planned.
- [ ] Semantic colors, arrow styles, type roles, and compact geometry match the design system.
- [ ] Only surfaces owned by the changed fact were edited.
- [ ] README size, XML validation, links, anchors, and diff checks pass.
