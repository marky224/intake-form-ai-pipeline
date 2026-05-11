// =====================================================================
// modules/audit-log.bicep — Activity Log export (CloudTrail analog)
//
// Mirrors infra/terraform/cloudtrail.tf.
//
// AWS  → Azure mapping:
//   aws_cloudtrail (management events)        → Microsoft.Insights/diagnosticSettings on the subscription's
//                                                Activity Log, exporting Administrative + Security +
//                                                ServiceHealth + Alert + Recommendation + Policy +
//                                                Autoscale + ResourceHealth categories to a Storage Account.
//   aws_cloudtrail (S3 data events)           → Per-Storage-Account Diagnostic Settings on each
//                                                Storage Account's blobServices/default (StorageWrite +
//                                                StorageRead + StorageDelete categories). The TF stack
//                                                wires data events through CloudTrail's single resource;
//                                                Azure splits them across per-resource diagnostic settings.
//                                                Per-account diagnostic settings live in storage.bicep
//                                                in a real deploy; deferred from this module to keep the
//                                                Activity-Log analog isolated.
//   is_multi_region_trail = false             → No analog. Activity Log is global; the export
//                                                destination determines the residency.
//   enable_log_file_validation = true         → Storage Account with immutability + versioning provides
//                                                tamper-evidence equivalent. The TF stack uses CloudTrail's
//                                                native digest-files mechanism; Azure has no direct analog
//                                                but Storage immutability policies are stronger.
//
// TARGET SCOPE: subscription. Activity Log is subscription-scoped, so the
// diagnostic settings on it must also be created at subscription scope.
// main.bicep invokes this module with `scope: subscription()`.
// =====================================================================

targetScope = 'subscription'

@description('Project identifier for naming.')
param projectName string

@description('Resource ID of the storage account that receives Activity Log exports.')
param storageAccountResourceId string

// ---------- Diagnostic settings on the subscription's Activity Log ----------

// Categories chosen for parity with the TF stack's management-events
// capture: Administrative (writes), Security, ServiceHealth, Alert,
// Recommendation, Policy, Autoscale, ResourceHealth. Excludes Workload-
// specific categories that don't apply at portfolio scale.
resource activityLogDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${projectName}-activity-log'
  scope: subscription()
  properties: {
    storageAccountId: storageAccountResourceId
    logs: [
      {
        category: 'Administrative'
        enabled: true
      }
      {
        category: 'Security'
        enabled: true
      }
      {
        category: 'ServiceHealth'
        enabled: true
      }
      {
        category: 'Alert'
        enabled: true
      }
      {
        category: 'Recommendation'
        enabled: true
      }
      {
        category: 'Policy'
        enabled: true
      }
      {
        category: 'Autoscale'
        enabled: true
      }
      {
        category: 'ResourceHealth'
        enabled: true
      }
    ]
  }
}

// ---------- Outputs ----------

@description('Diagnostic settings resource ID — informational.')
output diagnosticSettingsId string = activityLogDiag.id
