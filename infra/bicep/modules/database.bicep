// =====================================================================
// modules/database.bicep — Azure Database for PostgreSQL Flexible Server
//
// Mirrors infra/terraform/modules/database/main.tf.
//
// AWS  → Azure mapping:
//   aws_rds_cluster (Aurora Serverless v2)            → Microsoft.DBforPostgreSQL/flexibleServers (Burstable B1ms)
//   serverlessv2_scaling (min 0 ACU, 5-min auto-pause) → No direct analog (impedance — see below)
//   aws_rds_cluster_instance (db.serverless)          → flexibleServer.sku Standard_B1ms
//   aws_db_subnet_group                               → Delegated subnet on the flexibleServer (Microsoft.DBforPostgreSQL/flexibleServers delegation)
//   aws_security_group (cluster)                      → NSG on the delegated subnet (managed by network.bicep)
//   aws_rds_cluster_parameter_group (rds.force_ssl=1) → flexibleServers/configurations 'require_secure_transport'
//   aws_kms_key (CMK)                                 → Key Vault key + customer-managed key on the server
//   aws_cloudwatch_log_group (postgresql exports)     → Diagnostic Settings → log target Storage / Log Analytics
//   manage_master_user_password = true                → Microsoft Entra AD authentication preferred; AAD admin set below.
//                                                       (Bicep doesn't natively manage the AAD-side login object; a
//                                                       deployment script or post-deploy step is required.)
//   iam_database_authentication_enabled = true        → authConfig.activeDirectoryAuth = 'Enabled'
//   enabled_cloudwatch_logs_exports = ['postgresql']  → Diagnostic Settings (set in main.bicep / audit-log.bicep)
//
// MATERIAL IMPEDANCE: Aurora Serverless v2 scale-to-zero has no managed-PG
// analog on Azure. Closest practical paths:
//   1. Burstable B1ms 24/7, ~$13/mo + storage. Picked here.
//   2. Scheduled stop/start via Azure Automation / Function (server can be
//      stopped, and is automatically restarted by Azure after 7 days). Saves
//      ~75% on compute; adds operational coupling.
//   3. Cosmos DB for PostgreSQL (Citus) — distributed, but ~$30+/mo at
//      minimum and overkill for a single-instance demo cluster.
// This module declares Option 1 with a comment noting Option 2 as the
// production-roadmap improvement.
// =====================================================================

@description('Project identifier for naming.')
param projectName string

@description('Region for the Flexible Server.')
param location string

@description('Resource ID of the delegated subnet from the network module. Must be delegated to Microsoft.DBforPostgreSQL/flexibleServers.')
param delegatedSubnetId string

@description('Resource ID of the privatelink.postgres.database.azure.com Private DNS Zone, VNet-linked. Required for Flexible Server\'s private-DNS hostname resolution.')
param privateDnsZoneId string

@description('PostgreSQL major version. Mirrors var.engine_version pinned to PG 16; Azure exposes the major (the platform manages minor patching).')
@allowed([
  '14'
  '15'
  '16'
])
param postgresVersion string = '16'

@description('Server SKU. Standard_B1ms is the lowest cost option (~$13/mo if 24/7); Standard_B2s and up are non-burstable.')
param skuName string = 'Standard_B1ms'

@description('Storage size in GiB. Minimum 32 on Burstable.')
@minValue(32)
@maxValue(16384)
param storageSizeGb int = 32

@description('Initial database name. Mirrors var.database_name.')
param databaseName string = 'intake'

@description('Backup retention days. Mirrors var.backup_retention_period (Aurora portfolio default is 1 day; Azure minimum is 7).')
@minValue(7)
@maxValue(35)
param backupRetentionDays int = 7

@description('Object ID of the Microsoft Entra principal to register as the PostgreSQL admin. Required because authConfig declares activeDirectoryAuth=Enabled with passwordAuth=Disabled — without an admin principal, the server has no reachable login. Pass an empty string to skip the admin registration (e.g. for compile-only validation); a real deploy must supply this.')
param entraAdminObjectId string = ''

@description('Display name of the Entra admin principal (user UPN, group display name, or service principal name). Required iff entraAdminObjectId is set.')
param entraAdminLoginName string = ''

@description('Principal type for the Entra admin: User, Group, or ServicePrincipal.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param entraAdminPrincipalType string = 'User'

@description('Common tags.')
param tags object

// ---------- Key Vault for CMK ----------

// Mirrors the TF stack's CMK pattern (aws_kms_key.aurora encrypts cluster
// volume + master secret + log group). Key Vault adds soft-delete and
// purge-protection that mirror KMS's 30-day deletion window.
//
// Vault name: 3-24 chars, alphanumeric + hyphens, globally unique.
var keyVaultName = take('${replace(projectName, '-', '')}kv${uniqueString(resourceGroup().id)}', 24)

resource keyVault 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true // Required for use as a CMK source by Storage / PG / SQL.
    // PG Flexible Server's CMK access goes through Azure's control plane
    // and requires the vault to be reachable by the AzureServices bypass.
    // publicNetworkAccess=Enabled + networkAcls.defaultAction=Deny +
    // bypass=AzureServices is the documented pattern: the vault rejects
    // arbitrary internet traffic but allows the platform's CMK plumbing.
    // (An earlier revision set publicNetworkAccess=Disabled with no
    // Private Endpoint, which would have made PG unable to reach the
    // key for encryption — caught in CodeRabbit review.) For a real
    // VNet-isolation posture, layer a Private Endpoint on the vault and
    // flip back to Disabled.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource pgEncryptionKey 'Microsoft.KeyVault/vaults/keys@2024-04-01-preview' = {
  parent: keyVault
  name: 'postgres-cmk'
  properties: {
    kty: 'RSA'
    keySize: 2048
    keyOps: [
      'encrypt'
      'decrypt'
      'sign'
      'verify'
      'wrapKey'
      'unwrapKey'
    ]
    attributes: {
      enabled: true
      exportable: false
    }
    // 90-day rotation matches AWS KMS's enable_key_rotation = true (which is
    // annual). Tightening to 90 days is more conservative; AWS doesn't
    // expose configurable rotation periods.
    rotationPolicy: {
      lifetimeActions: [
        {
          trigger: {
            timeAfterCreate: 'P90D'
          }
          action: {
            type: 'rotate'
          }
        }
        {
          trigger: {
            timeBeforeExpiry: 'P30D'
          }
          action: {
            type: 'notify'
          }
        }
      ]
      attributes: {
        expiryTime: 'P2Y'
      }
    }
  }
}

// ---------- User-Assigned Managed Identity for CMK access ----------

// Flexible Server can use either a system-assigned or user-assigned
// managed identity to access the Key Vault for the CMK. UAMI is the
// documented pattern because the identity persists across server restores
// / recreates. Mirrors the AWS pattern where the Aurora service principal
// is granted Encrypt/Decrypt on the CMK via the key policy.
resource pgUami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${projectName}-postgres-uami'
  location: location
  tags: tags
}

// Key Vault RBAC role assignment: 'Key Vault Crypto Service Encryption User'
// is the least-privilege role for CMK use (wrap/unwrap data keys with the
// vault key; cannot list / read raw key material).
var keyVaultCryptoServiceEncryptionUserRoleId = 'e147488a-f6f5-4113-8e2d-b22465e65bf6'

resource pgKvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, pgUami.id, keyVaultCryptoServiceEncryptionUserRoleId)
  properties: {
    principalId: pgUami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultCryptoServiceEncryptionUserRoleId
    )
  }
}

// ---------- Flexible Server ----------

resource flexibleServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${projectName}-pg'
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: 'Burstable'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pgUami.id}': {}
    }
  }
  properties: {
    version: postgresVersion
    administratorLogin: 'intake_admin'
    // Server-generated administrator password handling: Flexible Server
    // does not have a true equivalent of RDS's manage_master_user_password.
    // Production deploys would set Entra ID auth as the only auth mode and
    // never expose a SQL admin password. Bicep can do this via authConfig
    // below; the administratorLoginPassword field then becomes optional.
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled' // Entra ID only — closest analog to Aurora's IAM database authentication.
      tenantId: subscription().tenantId
    }
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: storageSizeGb
      autoGrow: 'Enabled'
      // Premium_LRS is the only storage type Flexible Server's Burstable
      // tier supports — PremiumV2_LRS is restricted to General Purpose
      // and Memory Optimized tiers per the Azure PG storage compatibility
      // matrix. (CodeRabbit suggested PremiumV2_LRS; rejected because
      // it pairs only with non-Burstable SKUs and would fail at deploy
      // time against the Standard_B1ms tier above.)
      type: 'Premium_LRS'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled' // Single-instance, mirrors aws_rds_cluster_instance count=1.
    }
    maintenanceWindow: {
      customWindow: 'Enabled'
      dayOfWeek: 0 // Sunday
      startHour: 9
      startMinute: 0
    }
    dataEncryption: {
      type: 'AzureKeyVault'
      primaryUserAssignedIdentityId: pgUami.id
      primaryKeyURI: pgEncryptionKey.properties.keyUriWithVersion
    }
  }
  dependsOn: [
    pgKvRoleAssignment
  ]
}

// ---------- Server configurations ----------

// Mirror of the TF stack's rds.force_ssl = 1 parameter group. Flexible
// Server exposes the same semantic as the `require_secure_transport`
// PG configuration (default ON; explicit for parity).
resource configRequireSecureTransport 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: flexibleServer
  name: 'require_secure_transport'
  properties: {
    source: 'user-override'
    value: 'on'
  }
}

// pgaudit deferral (mirrors the TF stack's CKV2_AWS_27 inline-suppress
// note that pgaudit requires shared_preload_libraries + per-database
// CREATE EXTENSION; tied to the compute layer, not this PR).

// ---------- Entra ID admin (required because passwordAuth=Disabled) ----------

// Without an admin principal, the server is unreachable. Conditional on
// entraAdminObjectId so the module compiles cleanly for no-deploy
// validation; a real deploy must supply a non-empty objectId.
// (Earlier revision shipped without this resource — caught in CodeRabbit
// review.)
resource pgAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = if (!empty(entraAdminObjectId)) {
  parent: flexibleServer
  name: entraAdminObjectId
  properties: {
    principalType: entraAdminPrincipalType
    principalName: entraAdminLoginName
    tenantId: subscription().tenantId
  }
}

// ---------- Database ----------

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: flexibleServer
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// ---------- Outputs ----------

@description('Flexible Server fully-qualified domain name (private DNS-resolved inside the VNet).')
output serverFqdn string = flexibleServer.properties.fullyQualifiedDomainName

@description('Flexible Server resource ID.')
output serverId string = flexibleServer.id

@description('Key Vault holding the PostgreSQL CMK.')
output keyVaultId string = keyVault.id

@description('User-Assigned Managed Identity used by the Flexible Server for CMK access.')
output postgresUamiId string = pgUami.id
