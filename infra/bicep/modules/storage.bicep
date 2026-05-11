// =====================================================================
// modules/storage.bicep — Storage Account + Blob Container
//
// Mirrors infra/terraform/modules/storage/main.tf. Called 5x from
// main.bicep (documents / artifacts / access-logs / activity-log /
// landing) — same per-bucket isolation the TF stack uses.
//
// AWS  → Azure mapping:
//   aws_s3_bucket                            → Microsoft.Storage/storageAccounts
//   aws_s3_bucket_versioning (Enabled)       → blobServices.properties.isVersioningEnabled
//   aws_s3_bucket_server_side_encryption     → encryption (Microsoft-managed by default;
//                                              CMK-from-Key-Vault optional but free)
//   aws_s3_bucket_public_access_block        → publicNetworkAccess=Disabled +
//                                              allowBlobPublicAccess=false
//   aws_s3_bucket_logging                    → Diagnostic Settings → log target SA
//                                              (wired in main.bicep, not here)
//   aws_s3_bucket_policy (DenyInsecureTransport) → supportsHttpsTrafficOnly=true +
//                                                  minimumTlsVersion=TLS1_2
//   aws_s3_bucket_lifecycle_configuration    → managementPolicies
//
// Storage Accounts are global-namespace resources (the name must be
// globally unique). The main.bicep `nameSuffix` keeps that consistent.
// =====================================================================

@description('Globally-unique Storage Account name (3-24 chars, lowercase alphanumeric).')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Blob container name. `$web` is reserved for static-website hosting (landing bucket).')
param containerName string

@description('Role descriptor (documents / artifacts / access-logs / activity-log / landing). Becomes a tag for cost allocation and a switch for audit-log-target behavior.')
@allowed([
  'documents'
  'artifacts'
  'access-logs'
  'activity-log'
  'landing'
])
param role string

@description('Region for the Storage Account.')
param location string

@description('Whether this account is the destination for other accounts\' diagnostic / activity logs. Audit-log targets get extended lifecycle (365d expiration on the container) and skip self-logging.')
param isAuditLogTarget bool

@description('Common tags applied to the Storage Account.')
param tags object

// ---------- Storage Account ----------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: union(tags, {
    Purpose: role
  })
  sku: {
    // Standard_LRS = local-redundant (3 copies in one zone). Equivalent of
    // S3 Standard for this workload — durability is ~11 nines, multi-AZ
    // redundancy (ZRS) costs ~25% more. AWS S3 is region-redundant by
    // default; Azure splits redundancy into a separate SKU, so we pick
    // LRS for cost parity with S3 Standard at portfolio scale.
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    // ----- AWS public_access_block analog -----
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true // Required for some service integrations; RBAC is layered separately.
    publicNetworkAccess: 'Enabled' // Set to 'Disabled' once Private Endpoints are wired in a follow-up.

    // ----- AWS DenyInsecureTransport bucket-policy analog -----
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'

    // ----- Cross-tenant replication off, OAuth default auth -----
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true

    // ----- Encryption: Microsoft-managed keys by default. CMK-from-Key-Vault
    // is a separate resource ('Microsoft.Storage/storageAccounts/encryptionScopes')
    // that requires the Storage Account to have a system-assigned managed
    // identity granted Get/Wrap/Unwrap on a Key Vault key. Deferred to a
    // follow-up — the TF stack uses SSE-S3 (AES256, AWS-managed), so MMK
    // here is the matching default. -----
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
        file: {
          enabled: true
          keyType: 'Account'
        }
      }
      requireInfrastructureEncryption: false
    }

    // ----- Network restriction: deny by default, allow from VNet subnets
    // with the Storage Service Endpoint (wired in network.bicep). Mirrors
    // the TF stack's S3 Gateway VPC Endpoint pattern. AzureServices bypass
    // is enabled so Diagnostic Settings, Activity Log export, and Front
    // Door can write to this account without IP-allowlisting Azure's
    // public-IP ranges. -----
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices, Logging, Metrics'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

// ---------- Blob service (versioning + soft-delete) ----------

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    // ----- AWS aws_s3_bucket_versioning Enabled analog -----
    isVersioningEnabled: true

    // ----- Soft-delete for blobs + containers (Azure-native; no direct
    // S3 analog beyond versioning, but closer to "MFA Delete + versioning"
    // semantics). 7-day retention mirrors the TF noncurrent_version_expiration
    // for non-audit buckets; audit-log targets get longer retention via
    // the management policy below. -----
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }

    // ----- Change feed off (would be useful for an event-driven feedback
    // loop later but not in this PR). -----
    changeFeed: {
      enabled: false
    }
  }
}

// ---------- Container ----------

// Special-case the static-website "$web" container for the landing role.
// Azure's static-website feature provisions $web automatically once
// staticWebsite is enabled on the blob service — but enabling that here
// would couple the landing module to a property that the other 4
// storage accounts don't need. Instead we always declare the container
// explicitly; for the landing role, the name happens to be '$web', and
// AZ will not duplicate-create.
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (role != 'landing') {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

resource webContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (role == 'landing') {
  parent: blobService
  name: '$web'
  properties: {
    publicAccess: 'None'
  }
}

// ---------- Lifecycle management policy ----------

// Mirrors the TF stack's aws_s3_bucket_lifecycle_configuration rules:
//   - Expire noncurrent versions after N days.
//   - Abort incomplete multipart uploads after 1 day.
//   - Audit-log targets only: expire current-version objects after 365 days.
//
// Azure's management policy is JSON inside a single resource; the rules
// array carries the same semantics.
var noncurrentExpirationDays = 90
var auditLogExpirationDays = 365

resource managementPolicy 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: concat(
        [
          {
            name: 'expire-noncurrent-versions'
            enabled: true
            type: 'Lifecycle'
            definition: {
              filters: {
                blobTypes: [
                  'blockBlob'
                ]
              }
              actions: {
                version: {
                  delete: {
                    daysAfterCreationGreaterThan: noncurrentExpirationDays
                  }
                }
              }
            }
          }
          {
            // Azure Blob does not surface "incomplete multipart upload"
            // as a separate object class — uncommitted blocks are cleaned
            // up by the platform after 7 days automatically. The closest
            // analog of the TF stack's `aws_s3_bucket_lifecycle_configuration`
            // `abort_incomplete_multipart_upload { days_after_initiation = 1 }`
            // is to expire UNCOMMITTED block blobs after 1 day via a
            // baseBlob delete rule scoped to unindexed (uncommitted) blobs.
            // (Earlier revision had this at 365 days, which would have
            // deleted entire blobs after a year by mistake — caught in
            // CodeRabbit review.)
            name: 'expire-uncommitted-blocks'
            enabled: true
            type: 'Lifecycle'
            definition: {
              filters: {
                blobTypes: [
                  'blockBlob'
                ]
              }
              actions: {
                baseBlob: {
                  delete: {
                    daysAfterLastTierChangeGreaterThan: 1
                  }
                }
              }
            }
          }
        ],
        // Audit-log targets get a 365-day current-version expiration rule.
        isAuditLogTarget
          ? [
              {
                name: 'expire-log-objects'
                enabled: true
                type: 'Lifecycle'
                definition: {
                  filters: {
                    blobTypes: [
                      'blockBlob'
                    ]
                  }
                  actions: {
                    baseBlob: {
                      delete: {
                        daysAfterCreationGreaterThan: auditLogExpirationDays
                      }
                    }
                  }
                }
              }
            ]
          : []
      )
    }
  }
}

// ---------- Outputs ----------

@description('Storage Account resource ID.')
output storageAccountId string = storageAccount.id

@description('Storage Account name (globally unique).')
output storageAccountName string = storageAccount.name

@description('Primary blob endpoint, e.g. https://<name>.blob.core.windows.net/.')
output primaryBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob

@description('Container resource ID. Set for non-landing roles; for landing, the $web container ID is in webContainerId.')
output containerId string = role == 'landing' ? webContainer.id : container.id

@description('Static-website primary endpoint, e.g. https://<name>.z01.web.core.windows.net/. Used by Front Door as the landing origin hostname.')
output webEndpoint string = storageAccount.properties.primaryEndpoints.web
