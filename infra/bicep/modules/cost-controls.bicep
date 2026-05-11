// =====================================================================
// modules/cost-controls.bicep — Action Group + Budget + anomaly alert
//
// Mirrors infra/terraform/cost-controls.tf.
//
// AWS  → Azure mapping:
//   aws_sns_topic.cost_alerts                → Microsoft.Insights/actionGroups
//   aws_sns_topic_policy.cost_alerts         → No analog needed (Action Groups don't have a separate
//                                              resource policy; Azure handles publish-from-service
//                                              authorization at the service level via RBAC).
//   aws_budgets_budget.daily_spend (DAILY $5) → Microsoft.Consumption/budgets (Monthly $150)
//   aws_ce_anomaly_monitor + subscription    → Microsoft.CostManagement scheduled action
//                                              (Azure Cost Management surfaces anomalies as a built-in
//                                              feature; this module wires a notification rule).
//
// MATERIAL IMPEDANCE: Azure budgets do not support DAILY time grain — only
// Monthly, Quarterly, Annually. The TF stack's daily $5 threshold has no
// direct analog. The accepted impedance:
//   - Monthly $150 budget = ~$5/day × 30 (slightly headroomed; the TF stack
//     targets the AWS account whose `Realistic monthly cost: $10-15` per
//     CLAUDE.md, so $150 sits well above the realistic envelope while still
//     surfacing pathological spikes).
//   - Tiered notifications at 50%/80%/100% give early signal that a monthly
//     budget alone wouldn't — closest analog to the TF stack's
//     "anything-in-a-day" daily breaker.
//   - Anomaly alerts (which DO surface daily-rate spikes) complement the
//     monthly budget — they fire on absolute-dollar deviation from baseline
//     regardless of the budget's time grain, so a $5/day spike against a
//     normally-$0.50/day pattern triggers them.
//
// TARGET SCOPE: subscription. Budgets must live at subscription or
// management-group scope; Action Groups can live at RG scope but live at
// subscription scope here for parity with the budget.
// =====================================================================

targetScope = 'subscription'

@description('Project identifier for naming.')
param projectName string

@description('Monthly budget threshold in USD.')
param monthlyBudgetUsd int

@description('Email destination for the anomaly alert. The Action Group (which carries the same email for budget alerts) lives at RG scope in main.bicep and is referenced via actionGroupId.')
@secure()
param alertEmail string

@description('Resource group name — scope filter for the budget so the budget only sees project-RG spend.')
param resourceGroupName string

@description('Action Group resource ID (RG-scoped, created in main.bicep). Budget notifications publish to this group.')
param actionGroupId string

// ---------- Budget ----------

// Microsoft.Consumption budgets live at subscription scope. Filter narrows
// to project-tagged spend (matching the TF stack's TagKeyValue cost
// filter on `user:Project$intake-form-ai-pipeline`) AND project resource
// group, so other projects in this subscription stay outside scope.
//
// Time grain choice: Monthly. Azure does not support DAILY budgets; see
// the module-header impedance note for the rationale on the $150 monthly
// equivalent.
resource dailyBudget 'Microsoft.Consumption/budgets@2024-08-01' = {
  name: '${projectName}-monthly'
  properties: {
    category: 'Cost'
    amount: monthlyBudgetUsd
    timeGrain: 'Monthly'
    timePeriod: {
      // Budget starts on the 1st of the current month. The platform
      // automatically rolls this forward each month.
      startDate: '2026-05-01T00:00:00Z'
      // No endDate = budget runs indefinitely.
    }
    filter: {
      and: [
        {
          dimensions: {
            name: 'ResourceGroupName'
            operator: 'In'
            values: [
              resourceGroupName
            ]
          }
        }
        {
          tags: {
            name: 'Project'
            operator: 'In'
            values: [
              projectName
            ]
          }
        }
      ]
    }
    notifications: {
      // Three-tier notification ladder — early signal at 50%, warning at
      // 80%, action at 100%. Mirrors the TF stack's single 100%-ACTUAL
      // notification on a DAILY budget with extra granularity to
      // compensate for the monthly time grain.
      Actual_50_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: []
        contactRoles: []
        contactGroups: [
          actionGroupId
        ]
      }
      Actual_80_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: []
        contactRoles: []
        contactGroups: [
          actionGroupId
        ]
      }
      Actual_100_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: []
        contactRoles: []
        contactGroups: [
          actionGroupId
        ]
      }
      // Forecasted notification at 100% — Azure budgets DO support
      // forecasted alerts on monthly time grains (unlike AWS Budgets,
      // where DAILY budgets are ACTUAL-only). This is the early-warning
      // complement that the AWS hotfix PR #38 had to drop on the DAILY
      // budget.
      Forecasted_100_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: []
        contactRoles: []
        contactGroups: [
          actionGroupId
        ]
      }
    }
  }
}

// ---------- Anomaly alert ----------

// Cost Management anomaly detection: a scheduled action that fires daily
// at a fixed time and emails when a cost anomaly is detected. The Azure
// analog of the account's Default-Services-Subscription (which the AWS
// side delegates anomaly detection to rather than managing its own
// monitor + subscription in IaC).
//
// `scheduledActions/v1.0` uses a kind discriminator: `InsightAlert` is
// the anomaly-alert subtype. Daily 09:00 UTC delivery means anomaly
// signals surface within ~24h of the spike (acceptable for a portfolio
// breaker; the budget provides faster signal at threshold crossings).
resource anomalyAlert 'Microsoft.CostManagement/scheduledActions@2024-08-01' = {
  name: '${projectName}-anomaly-alert'
  kind: 'InsightAlert'
  properties: {
    displayName: '${projectName} cost anomaly daily alert'
    status: 'Enabled'
    viewId: '${subscription().id}/providers/Microsoft.CostManagement/views/ms:DailyAnomalyByResourceGroup'
    notification: {
      to: [
        alertEmail
      ]
      subject: 'Cost anomaly detected on ${projectName}'
      message: 'Azure Cost Management detected a daily anomaly in resource group ${resourceGroupName}. Review at https://portal.azure.com/.'
    }
    schedule: {
      frequency: 'Daily'
      hourOfDay: 9
      startDate: '2026-05-01T00:00:00Z'
      endDate: '2030-12-31T00:00:00Z'
      daysOfWeek: []
      weeksOfMonth: []
      dayOfMonth: 0
    }
  }
}

// ---------- Outputs ----------

@description('Budget name — mirrors TF output daily_budget_name (renamed monthly here).')
output budgetName string = dailyBudget.name

@description('Anomaly alert scheduled-action resource ID.')
output anomalyAlertId string = anomalyAlert.id
