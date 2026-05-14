# Bicep parallel — Azure branch (no deployment)

> ⚠️ **V2 cloud target — not deployed in V1.** Same V1 status as the Terraform main stack (see `infra/terraform/README.md`): the project pivoted to a local-first V1 on 2026-05-14, so no cloud (AWS or Azure) is in the V1 build. The Bicep tree was always documentation-only and stays so. When V2 begins, the AWS Terraform stack reapplies and this Bicep tree continues to mirror it for the multi-cloud thinking artifact.

This directory is the Azure-cloud parallel of `infra/terraform/`. It is **module structure only and is not deployed** — the project's V2 target is AWS as the primary cloud (Aurora Serverless v2, S3, CloudFront, WAFv2, etc.) with the public demo at `https://ai-intake.markandrewmarquez.com/` shipping from there. The Bicep tree exists to demonstrate that the same architecture is buildable on Azure with the equivalent service set, and to document the cross-cloud impedance mismatches that show up when you actually try.

CI runs `bicep build` on every push so syntax stays clean as the Terraform side evolves. Nothing here ever touches an Azure subscription.

## AWS → Azure service mapping

| AWS resource (Terraform) | Azure resource (Bicep) | Impedance / notes |
|---|---|---|
| VPC + subnets + IGW + NAT Gateway + Route Tables | VNet + subnets + NAT Gateway + Public IP + NSGs | Direct map. NSGs sit at the subnet level (vs AWS's per-resource SG model). |
| S3 Gateway VPC Endpoints (S3, DynamoDB) | Service Endpoints (Storage) + Private Endpoints | Azure splits "gateway-style routing tweak" (Service Endpoints, free) from "private IP in subnet" (Private Endpoints, ~$7.30/mo per endpoint). The TF stack uses Gateway endpoints (free) — Service Endpoints are the closest free analog. |
| S3 buckets (documents, artifacts, access-logs, cloudtrail-logs, landing) | Storage Account + Blob Containers | One Storage Account per logical bucket keeps the per-bucket policy / access-log / lifecycle isolation that S3 gives natively. Could be consolidated into fewer Storage Accounts with per-container ACLs at the cost of blast-radius coupling. |
| KMS CMK | Key Vault key (RSA-HSM or RSA-2048 software) | Direct map. Key Vault has Soft-delete + Purge-protection equivalent to KMS's 30-day deletion window. |
| Aurora Serverless v2 PostgreSQL (min 0 ACU, 5-min auto-pause) | Azure Database for PostgreSQL Flexible Server (Burstable B1ms) | **Material impedance**: Azure has no true scale-to-zero managed PG. Flexible Server's lowest tier is Burstable B1ms at ~$13/mo if 24/7. Mark's pattern would be a Logic App / Function scheduling `az postgres flexible-server stop` during idle windows, getting ~$3/mo effective cost. Documented in `modules/database.bicep`. |
| CloudTrail | Activity Log + Diagnostic Settings (export to Storage) | Direct map. Activity Log is always-on; Diagnostic Settings are what makes it durable. |
| CloudFront + Origin Access Control | Azure Front Door Standard + Managed Identity + Storage RBAC | Direct map. Front Door Standard is the cheaper SKU (~$35/mo + traffic); Premium adds Bot Manager + advanced WAF rules at ~$330/mo. Standard is the right portfolio choice. |
| ACM cert (DNS-validated) | Front Door managed certificate (auto-renew) | Direct map. Both are free. |
| WAFv2 web ACL (rate-based + UA byte-match + AWS-managed rule sets) | Front Door WAF policy (rate-limit rule + custom match rule + managed rule sets) | Direct map. Managed rule set names differ: `Microsoft_DefaultRuleSet_2.1` is the rough analog of `AWSManagedRulesCommonRuleSet`. |
| Route 53 hosted zone + A/AAAA alias records | Azure DNS zone + A/AAAA records (alias to Front Door) | Direct map. The hosted zone is assumed Mark-owned analogously to `Z04568022MZ21HXK15I1D` in the Terraform stack. |
| CloudFront v2 access logs delivery (CWL Delivery → S3) | Front Door Diagnostic Settings → Storage Account | Azure's pattern is simpler — Diagnostic Settings are a first-class export surface; no intermediate Delivery Source/Destination/Delivery resources. |
| SNS topic | Action Group (email + webhook receivers) | Direct map. Action Groups are the standard alert destination for Azure Monitor + Cost Management. |
| AWS Budgets (DAILY $5, ACTUAL @ 100%) | `Microsoft.Consumption/budgets` (MONTHLY $150) | **Material impedance**: Azure budgets only support `Monthly`, `Quarterly`, `Annually` time grains — no DAILY. Closest analog is a monthly budget tuned to the same effective rate ($5/day × 30 = $150/month) plus an anomaly alert for early signal on daily-rate spikes. Documented in `modules/cost-controls.bicep`. |
| Cost Anomaly Detection (DIMENSIONAL/SERVICE monitor + IMMEDIATE subscription) | Cost Management anomaly detection (Microsoft.CostManagement/scheduledActions) | Direct map. Both surface "spend spiked vs baseline" alerts with similar configurability. |
| DynamoDB single-item cold-start lock | Cosmos DB SQL container, 400 RU/s autoscale, single document | Direct map (deferred — adds with the compute layer, same as the AWS side). Not in this PR. |
| IAM OIDC provider + role | Microsoft Entra ID Workload Identity Federation on a User-Assigned Managed Identity | Direct map. Not in this PR — bootstrap parallel is out of scope (no deployment means no CI federation is needed). See "Out of scope" below. |

## Directory layout

Mirrors `infra/terraform/` 1:1 except for the bootstrap stack (see "Out of scope"):

```text
infra/bicep/
├── README.md                     # this file
├── main.bicep                    # subscription-scoped orchestrator; creates the RG and invokes modules
├── main.bicepparam               # parameter file with defaults mirroring TF defaults
└── modules/
    ├── network.bicep             # VNet + subnets + NAT + Service Endpoints + PG private DNS zone
    ├── storage.bicep             # reusable Storage Account + container module (invoked 5x for the 5 buckets)
    ├── database.bicep            # PostgreSQL Flexible Server + Key Vault key + UAMI + Entra admin
    ├── front-door.bicep          # Front Door Standard + WAF policy + custom domain
    ├── dns.bicep                 # Azure DNS zone + A/AAAA records
    ├── audit-log.bicep           # Activity Log Diagnostic Settings (CloudTrail analog, subscription scope)
    ├── action-group.bicep        # Action Group for cost alerts (RG-scoped; SNS-topic analog)
    └── cost-controls.bicep       # Budget + anomaly alert (subscription scope; consumes the Action Group)
```

## Local syntax check

```bash
# Install bicep CLI if needed (single Go binary, no .NET runtime)
curl -fsSL https://github.com/Azure/bicep/releases/latest/download/bicep-linux-x64 -o ~/.local/bin/bicep
chmod +x ~/.local/bin/bicep

# Build all .bicep files in this directory tree
just bicep-build
```

The CI workflow (`.github/workflows/ci.yml`, `bicep-build` job) runs the same on every PR.

## Out of scope

- **Bootstrap parallel** (state backend + GitHub OIDC federation). Bicep is stateless ARM, so the Terraform-state-bucket half has no analog. The OIDC-federation half (User-Assigned Managed Identity + federated credential for the GitHub repo) is real but inert without an actual Azure subscription wired to CI — adding it would imply readiness to deploy, which is not the goal. The Terraform `bootstrap/` README is the canonical reference for the federation pattern; the Azure equivalent is `Microsoft.ManagedIdentity/userAssignedIdentities` + `Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials`.
- **`Microsoft.DocumentDB/databaseAccounts` (Cosmos DB)** for the cold-start lock. Lands with the compute layer, same phase as the AWS DynamoDB single-item lock.
- **Compute** (Functions, Container Apps, AKS, ML workspaces). The architecture-locked file targets AWS Lambda + Step Functions; Azure analogs (Functions + Logic Apps or Container Apps Jobs) come into scope only if Azure ever moves past the documentation-only branch.

## Why this exists

This project's locked architecture (see `architecture-locked.md` Infrastructure section) commits to AWS-primary with an Azure parallel branch — Bicep, no deployment. The portfolio value is in demonstrating:

1. That the same architecture is buildable on Azure end-to-end, with explicit awareness of where the service models diverge (PG scale-to-zero, budget time grains, VPC vs VNet endpoint cost models).
2. That hardening parity is the same across clouds — TLS-only, CMK-from-vault, public-access-blocked, audit-log export — and the choice of cloud doesn't relax those defaults.
3. That an interview conversation about "what would this look like on Azure?" has a concrete artifact to reference rather than a hand-wave.

Source of truth for any locked architectural decision remains `infra/terraform/`. When the Terraform side changes, the Bicep side should be updated to keep parity — `bicep build` in CI catches syntax breakage but cannot detect semantic drift.
