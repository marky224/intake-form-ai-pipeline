// =====================================================================
// main.bicepparam — default parameter values for main.bicep.
//
// Mirrors the defaults in infra/terraform/variables.tf. A deployment
// would invoke this via `az deployment sub create -l eastus -f
// main.bicep -p main.bicepparam`, but this stack does not deploy —
// the file exists so `bicep build-params main.bicepparam` exercises
// the param-binding surface in CI.
// =====================================================================

using 'main.bicep'

param location = 'eastus'
param projectName = 'intake-form-ai-pipeline'
param vnetCidr = '10.0.0.0/16'
param demoDomain = 'ai-intake.markandrewmarquez.com'
param wafRateLimitPer5Min = 100
param blockedUserAgents = [
  'python-requests'
  'curl'
  'scrapy'
  'wget'
]
param monthlyBudgetUsd = 150

// alertEmail is marked @secure() in main.bicep so it must be supplied
// at deploy time (env var, Key Vault reference, or interactive prompt).
// A real deployment pattern: `param alertEmail = readEnvironmentVariable('ALERT_EMAIL')`.
// Left unset here so `bicep build-params` doesn't bake the email into
// the compiled parameter JSON.
