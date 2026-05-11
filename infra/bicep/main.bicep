// =====================================================================
// main.bicep — Azure parallel orchestrator (subscription scope)
//
// Mirrors infra/terraform/main.tf, with these differences forced by the
// Azure resource model:
//   - Bicep targets subscription scope so the resource group itself is a
//     resource managed by this stack (vs. AWS's flat-account model where
//     no equivalent boundary exists). Modules then run at the RG scope
//     via `scope: rg` on each module call.
//   - Cost-controls (budget) is invoked at subscription scope because
//     Microsoft.Consumption/budgets must live at subscription or
//     management-group scope, not RG.
//
// THIS STACK DOES NOT DEPLOY. CI runs `bicep build` only; see README.md.
// =====================================================================

targetScope = 'subscription'

// ---------- Parameters ----------

@description('Azure region for the resource group and regional resources. Mirrors var.aws_region (us-east-1) — `eastus` is the closest pairing.')
param location string = 'eastus'

@description('Project identifier used for resource naming and the project tag. Mirrors var.project_name.')
param projectName string = 'intake-form-ai-pipeline'

@description('CIDR block for the VNet. Mirrors var.vpc_cidr.')
param vnetCidr string = '10.0.0.0/16'

@description('Public DNS name for the demo. Mirrors var.demo_domain.')
param demoDomain string = 'ai-intake.markandrewmarquez.com'

@description('Per-IP request limit over a rolling 5-minute window enforced by the Front Door WAF rate-limit rule. Mirrors var.waf_rate_limit_per_5min.')
@minValue(10)
@maxValue(2000000000)
param wafRateLimitPer5Min int = 100

@description('User-Agent header substrings to BLOCK at the Front Door edge. Mirrors var.blocked_user_agents.')
param blockedUserAgents array = [
  'python-requests'
  'curl'
  'scrapy'
  'wget'
]

@description('Monthly budget threshold in USD. Azure budgets do not support DAILY time grain (see modules/cost-controls.bicep); this is the monthly equivalent of the TF stack\'s $5/day daily budget.')
param monthlyBudgetUsd int = 150

@description('Email endpoint for the cost-alerts Action Group. Secure parameter; for parity with the AWS-side discipline (which subscribes personal-email endpoints manually post-apply rather than from Terraform state), supply at deploy time via env var or Key Vault reference. Empty default so `bicep build` does not bake an email into compiled JSON. A real deploy must supply this.')
@secure()
param alertEmail string = ''

// ---------- Locals (Bicep variables) ----------

var commonTags = {
  Project: projectName
  Environment: 'demo'
  ManagedBy: 'bicep'
  Owner: 'mark'
  Stack: 'main'
}

var resourceGroupName = '${projectName}-rg'

// Storage account names: globally unique, 3-24 chars, lowercase alphanumeric.
// The TF stack name-suffixes by AWS account ID; the Azure analog is
// `uniqueString(subscription().id, projectName)`.
var nameSuffix = uniqueString(subscription().id, projectName)
var storageAccountBaseName = replace(replace(projectName, '-', ''), '_', '')

// Storage account "logical buckets" mirror the 5 S3 buckets in the TF stack.
// Each storage account hosts a single primary container so the bucket-level
// policy / access-log / lifecycle isolation that S3 gives natively maps
// cleanly.
//
// Keyed by role (rather than ordered list + numeric indexing) so reordering
// or insertion can't silently rewire which account becomes the audit-log
// target, Front Door origin, etc. Module invocations + downstream wiring
// below reference these by role name. (Caught in CodeRabbit review.)
var storageAccountsByRole = {
  documents: {
    // Max 24 chars. Truncate base to 14 to leave room for role + suffix.
    name: take('${take(storageAccountBaseName, 14)}doc${nameSuffix}', 24)
    containerName: 'documents'
  }
  artifacts: {
    name: take('${take(storageAccountBaseName, 14)}art${nameSuffix}', 24)
    containerName: 'artifacts'
  }
  'access-logs': {
    name: take('${take(storageAccountBaseName, 14)}log${nameSuffix}', 24)
    containerName: 'access-logs'
  }
  'activity-log': {
    name: take('${take(storageAccountBaseName, 14)}act${nameSuffix}', 24)
    containerName: 'activity-log'
  }
  landing: {
    name: take('${take(storageAccountBaseName, 14)}lnd${nameSuffix}', 24)
    containerName: '$web' // Static website hosting container; Front Door origin.
  }
}

var storageRoles = [
  'documents'
  'artifacts'
  'access-logs'
  'activity-log'
  'landing'
]

// ---------- Resource group ----------

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: commonTags
}

// ---------- Network ----------

module network 'modules/network.bicep' = {
  scope: rg
  name: 'network'
  params: {
    projectName: projectName
    location: location
    vnetCidr: vnetCidr
    tags: commonTags
  }
}

// ---------- Storage (5 buckets) ----------

@batchSize(1) // Sequential apply so any service-quota pacing issues surface one-by-one.
module storage 'modules/storage.bicep' = [
  for role in storageRoles: {
    scope: rg
    name: 'storage-${role}'
    params: {
      storageAccountName: storageAccountsByRole[role].name
      containerName: storageAccountsByRole[role].containerName
      role: role
      location: location
      // Mirrors the TF stack: documents/artifacts/landing get TLS+private+CMK
      // hardening; access-logs + activity-log are the audit-log targets.
      isAuditLogTarget: role == 'access-logs' || role == 'activity-log'
      tags: commonTags
    }
  }
]

// Resolve role → module index (preserves the for-loop ordering of
// storageRoles). Bicep cannot index into a module array by role string
// directly, so the explicit lookup variable is the cleanest pattern for
// downstream wiring. Reordering storageRoles above keeps this in sync
// automatically.
var storageIndexByRole = {
  documents: 0
  artifacts: 1
  'access-logs': 2
  'activity-log': 3
  landing: 4
}

// ---------- Database ----------

module database 'modules/database.bicep' = {
  scope: rg
  name: 'database'
  params: {
    projectName: projectName
    location: location
    delegatedSubnetId: network.outputs.databaseSubnetId
    privateDnsZoneId: network.outputs.postgresPrivateDnsZoneId
    tags: commonTags
  }
}

// ---------- Audit log export (CloudTrail analog) ----------

module auditLog 'modules/audit-log.bicep' = {
  // Diagnostic settings on the subscription's Activity Log are themselves
  // subscription-scoped, NOT RG-scoped. The module declares targetScope =
  // 'subscription' to match.
  scope: subscription()
  name: 'audit-log'
  params: {
    projectName: projectName
    storageAccountResourceId: storage[storageIndexByRole['activity-log']].outputs.storageAccountId
  }
}

// ---------- Front Door (CloudFront + WAFv2 + ACM combined) ----------

module frontDoor 'modules/front-door.bicep' = {
  scope: rg
  name: 'front-door'
  params: {
    projectName: projectName
    demoDomain: demoDomain
    // Front Door origin is the landing storage account's static-website
    // endpoint — NOT the blob endpoint. webEndpoint returns a full URL
    // (https://<acct>.z<n>.web.<env>.core.windows.net/); strip the
    // scheme + trailing slash so Front Door's `hostName` gets the bare host.
    originHostname: replace(replace(storage[storageIndexByRole.landing].outputs.webEndpoint, 'https://', ''), '/', '')
    wafRateLimitPer5Min: wafRateLimitPer5Min
    blockedUserAgents: blockedUserAgents
    diagnosticsStorageAccountId: storage[storageIndexByRole['access-logs']].outputs.storageAccountId
    tags: commonTags
  }
}

// ---------- DNS ----------

module dns 'modules/dns.bicep' = {
  scope: rg
  name: 'dns'
  params: {
    demoDomain: demoDomain
    frontDoorEndpointHostname: frontDoor.outputs.endpointHostname
    frontDoorResourceId: frontDoor.outputs.profileId
    tags: commonTags
  }
}

// ---------- Action Group (RG-scoped) — destination for cost alerts ----------

// Action Groups are RG-scoped, but the budget that publishes to them is
// subscription-scoped. Declared here at RG scope; cost-controls module
// (subscription scope) receives the resource ID via param.
module costAlertsActionGroup 'modules/action-group.bicep' = {
  scope: rg
  name: 'cost-alerts-action-group'
  params: {
    projectName: projectName
    alertEmail: alertEmail
  }
}

// ---------- Cost controls (subscription scope) ----------

module costControls 'modules/cost-controls.bicep' = {
  scope: subscription()
  name: 'cost-controls'
  params: {
    projectName: projectName
    monthlyBudgetUsd: monthlyBudgetUsd
    alertEmail: alertEmail
    resourceGroupName: resourceGroupName
    actionGroupId: costAlertsActionGroup.outputs.actionGroupId
  }
}

// ---------- Outputs ----------

@description('Resource group containing the project resources.')
output resourceGroupName string = rg.name

@description('VNet resource ID. Mirrors TF output vpc_id.')
output vnetId string = network.outputs.vnetId

@description('PostgreSQL Flexible Server fully-qualified hostname. Mirrors TF output aurora_cluster_endpoint (impedance: no scale-to-zero on Azure — see modules/database.bicep).')
output postgresHostname string = database.outputs.serverFqdn

@description('Storage account names by role. Mirrors TF outputs documents_bucket_name + artifacts_bucket_name + access_logs_bucket_name + landing_bucket_name + cloudtrail_logs_bucket_name (renamed to activity-log here).')
output storageAccounts object = {
  documents: storage[storageIndexByRole.documents].outputs.storageAccountName
  artifacts: storage[storageIndexByRole.artifacts].outputs.storageAccountName
  accessLogs: storage[storageIndexByRole['access-logs']].outputs.storageAccountName
  activityLog: storage[storageIndexByRole['activity-log']].outputs.storageAccountName
  landing: storage[storageIndexByRole.landing].outputs.storageAccountName
}

@description('Front Door endpoint hostname (default `<endpoint>.azurefd.net`). Mirrors TF output cloudfront_domain_name.')
output frontDoorEndpoint string = frontDoor.outputs.endpointHostname

@description('Front Door custom-domain hostname. Mirrors TF output demo_domain.')
output customDomainHostname string = demoDomain

@description('Action Group resource ID for cost alerts. Mirrors TF output cost_alerts_sns_topic_arn.')
output costAlertsActionGroupId string = costAlertsActionGroup.outputs.actionGroupId

@description('Monthly budget name. Mirrors TF output daily_budget_name (renamed because Azure budgets are monthly-only — see modules/cost-controls.bicep).')
output monthlyBudgetName string = costControls.outputs.budgetName
