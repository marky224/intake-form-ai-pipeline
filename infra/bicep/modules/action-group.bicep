// =====================================================================
// modules/action-group.bicep — Action Group for cost alerts (RG-scoped)
//
// Pulled out of cost-controls.bicep because Action Groups require RG
// scope, while the budget + anomaly resources they receive notifications
// from are subscription-scoped. Mirrors the AWS SNS topic in
// infra/terraform/cost-controls.tf.
//
// AWS  → Azure mapping:
//   aws_sns_topic.cost_alerts                  → Microsoft.Insights/actionGroups
//   aws_sns_topic_policy.cost_alerts           → No analog needed (no separate publisher-grant
//                                                resource; Azure budgets / metric alerts /
//                                                anomaly alerts publish via the platform identity).
//   aws_sns_topic_subscription (deferred)      → emailReceivers / webhookReceivers in this resource.
//
// Email destination handling: see the AWS-side note in
// infra/terraform/cost-controls.tf about `sensitive = true` not keeping
// values out of state — Bicep has the same shape. Deployment history
// (Microsoft.Resources/deployments) preserves parameter values; secure
// parameters are masked in portal UI but persisted in the deployment
// record. For a real deploy, the recommended pattern is to manage email
// subscriptions out-of-band via `az monitor action-group update` after
// the initial deploy, same as the AWS-side `aws sns subscribe` pattern.
// =====================================================================

@description('Project identifier for naming.')
param projectName string

@description('Email destination. Secure parameter — see module-header note on the impedance.')
@secure()
param alertEmail string

resource costAlertsActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${projectName}-cost-alerts'
  location: 'global'
  properties: {
    groupShortName: 'cost-alerts'
    enabled: true
    emailReceivers: [
      {
        name: 'mark-email'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
    smsReceivers: []
    webhookReceivers: []
    azureFunctionReceivers: []
    armRoleReceivers: []
  }
}

@description('Action Group resource ID.')
output actionGroupId string = costAlertsActionGroup.id

@description('Action Group name.')
output actionGroupName string = costAlertsActionGroup.name
