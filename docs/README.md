# Unifix docs

Two documents, plus the repo-root entry point. The **code is the source of truth**;
these explain the shape and the rationale.

- **[`/CLAUDE.md`](../CLAUDE.md)** (repo root) — start here: what the product is, how to
  run & verify, auth, the per-kind processing flow, config params, module structure, and tests.
- **[`architecture.md`](architecture.md)** — the *what*: components, the two layers, the
  request→result flow, the engine pipeline, the data model, and deployment notes.
- **[`design-decisions.md`](design-decisions.md)** — the *why*: storage/compression (and the
  measured "compression ≠ token savings" finding), deterministic field confidence, the three
  keyframe modes, editable DB-backed prompts/schemas, and the known-issues roadmap.
