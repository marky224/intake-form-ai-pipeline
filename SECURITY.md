# Security Policy

## Reporting a vulnerability

If you've found a security issue in this repository, please report it privately
rather than opening a public issue. Two options:

1. **GitHub private vulnerability reporting** (preferred): use the
   [Report a vulnerability](https://github.com/marky224/intake-form-ai-pipeline/security/advisories/new)
   button under the repo's Security tab. This creates a private advisory visible
   only to the maintainer and you.
2. **Email**: `security@markandrewmarquez.com`. Please include "intake-form-ai-pipeline"
   in the subject.

Expect an acknowledgement within 5 business days. This is a portfolio project
maintained by a single person, not a production service — response times reflect
that.

## Scope

This repository contains infrastructure-as-code, schema definitions, and
(in later phases) AI-pipeline application code. In-scope reports include:

- Secrets accidentally committed to the repo or its history
- Hardcoded credentials, AWS account IDs, or other identifiers in source
- Insecure infrastructure configurations (overly-broad IAM, public buckets,
  unencrypted resources, missing audit logging)
- Vulnerabilities in dependencies declared in `pyproject.toml` or
  `.github/workflows/*.yml`
- Prompt-injection or data-leakage issues in pipeline code (Phase 4+)

Out of scope:

- Findings against deployed infrastructure in the maintainer's AWS account
  (it's a personal account; please report rather than test)
- Denial-of-service against the live demo site
- Issues in third-party services this project depends on (report those upstream)

## What this project does *not* claim

This is a portfolio project, not a production system. It is **not** under a
Business Associate Agreement, does not currently process real PHI, and is not
audited against any compliance framework. The HIPAA-aware routing logic
demonstrates *how* a production deployment would partition by data sensitivity;
it is not itself a compliant deployment.

## Coordinated disclosure

If you'd like coordinated disclosure (e.g., you intend to publish a writeup),
please flag that in your initial report. Default disclosure window is 90 days
from acknowledgement, negotiable based on complexity.
