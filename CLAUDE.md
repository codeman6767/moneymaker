# CLAUDE.md

Permanent rules for this repository. These apply to all work unless the user explicitly overrides them in a specific request.

## System overview

This project has two lanes:

- **Research lane (source of truth).** The existing Python research and pregame
  system remains the source of model training and full Monte Carlo simulation.
  All models are trained, validated, and calibrated here.
- **Low-latency lane (optional).** An optional, event-driven low-latency lane
  for reacting to live provider events and submitting orders. It consumes
  immutable artifacts produced by the research lane; it never trains models or
  runs full simulations itself.

The low-latency lane is additive. It does not replace, weaken, or bypass any
part of the research lane.

## Low-latency rules

These rules are permanent and always in force:

### Performance claims and latency accounting

- Never claim end-to-end sub-second performance unless it is measured from
  provider event time through exchange acknowledgement.
- Always distinguish provider delay, network delay, internal processing delay,
  and exchange delay. Do not collapse them into a single number.

### Event and stream integrity

- Every event must contain provider, event, receipt, and monotonic timestamps.
- Every stream must support sequence validation, deduplication, correction
  handling, and snapshot recovery.
- Never trade from an order book or game state with an unresolved sequence gap.

### Hot decision path constraints

- Do not query PostgreSQL on the hot decision path.
- Do not load models on the hot decision path.
- Do not run large Monte Carlo simulations on the hot decision path.
- Preload immutable model artifacts and probability surfaces.

### Inference and implementation

- Use ONNX Runtime or another benchmarked low-overhead inference runtime.
- Use Python for the first correct implementation.
- Add a Rust/Tokio execution service only after benchmarks demonstrate that
  Python latency is material.

### Measurement and testing

- All fast paths must have latency histograms and p50, p95, p99, and maximum
  measurements.
- All latency tests must use monotonic clocks.

### Data source constraints

- Do not assume access to frame-level optical tracking.
- Frame-level tracking providers must remain optional adapters.
- Do not use unauthorized social-media scraping.

### Safety gates

- Never enable live orders because the code is fast. All previous model,
  paper-trading, and risk gates still apply.

## graphify

This project has a local knowledge graph at graphify-out/ (god nodes, community
structure, cross-file relationships). It is a local aid only; it is not required
to answer questions outside this repository.

Rules:
- For codebase questions, first use a **focused, non-empty** Graphify query.
  Examples:
  - `graphify query "Where is MLB roster ingestion implemented?"`
  - `graphify path "MlbStatsApiClient" "SqliteRosterRepository"`
  - `graphify explain "MLB result correction detection"`
- Never run `query`, `path`, or `explain` with empty arguments.
- These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw
  grep output. Read the full GRAPH_REPORT.md only when scoped queries are
  insufficient.
- After source changes, run `graphify update .` locally (AST-only, no API cost).
- Generated `graphify-out/` artifacts are git-ignored and must not be committed;
  Graphify itself stays available locally.
