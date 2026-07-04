# Security Regression Gates and Backlog

Sprint 7/8 local entrypoint:

```bash
bash tests/security/run-security-regression.sh
```

Full T1-T39 matrix, Sprint 8 backlog, blocked environment gates, SUP-MCP-020, E16 rollout, E18 checklist, and retention notes live in:

```text
tests/security/security-regression-matrix.md
```

Sprint 8 is documentation/rastreabilidade only: no HTML final yet, no new product-code fixes, and no commit. Items blocked by Supabase, Stripe, authenticated staging, multi-tenant data, or gradual flag rollout are tracked in the matrix with owner and exit gate.
