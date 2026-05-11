// =====================================================================
// modules/front-door.bicep — Azure Front Door Standard + WAF + custom domain
//
// Mirrors infra/terraform/cloudfront.tf + infra/terraform/waf.tf, which
// in the TF stack are two separate files because CloudFront and WAFv2
// are independent AWS resources. Azure collapses them: a Front Door
// profile + WAF policy + custom domain + diagnostic settings live as
// related resources under one Front Door profile.
//
// AWS  → Azure mapping:
//   aws_cloudfront_origin_access_control      → Front Door managed identity
//                                                + Storage RBAC (Storage Blob Data Reader)
//   aws_cloudfront_distribution               → Microsoft.Cdn/profiles (Standard_AzureFrontDoor)
//                                                + .../afdEndpoints
//                                                + .../originGroups
//                                                + .../originGroups/origins
//                                                + .../afdEndpoints/routes
//   aws_acm_certificate (DNS-validated)       → Front Door managed cert
//                                                (auto-provisioned + auto-renewed for custom domains)
//   aws_wafv2_web_acl.edge (CLOUDFRONT scope) → Microsoft.Network/frontDoorWebApplicationFirewallPolicies
//                                                (Standard_AzureFrontDoor SKU; Premium adds bot manager)
//   rate-based statement (rate_based_statement)            → customRules of type RateLimitRule
//   byte-match on User-Agent (or_statement)                → customRules of type MatchRule with operator Contains
//   AWSManagedRulesCommonRuleSet                           → managedRules.managedRuleSets[?].ruleSetType = Microsoft_DefaultRuleSet
//   AWSManagedRulesKnownBadInputsRuleSet                   → bundled inside Microsoft_DefaultRuleSet
//   AWSManagedRulesAmazonIpReputationList                  → managedRuleSets[?].ruleSetType = Microsoft_BotManagerRuleSet (Premium only — documented impedance)
//   v2 access logs delivery (CWL Delivery → S3)            → Diagnostic Settings on the profile → Storage Account
//   PriceClass_100                                          → Standard_AzureFrontDoor SKU (Standard ~$35/mo + traffic; Premium ~$330/mo)
//   default_root_object = index.html                        → Origin patternsToMatch + originPath
//   AWS-managed SecurityHeadersPolicy                       → Front Door has no managed equivalent;
//                                                             would be applied via Rules Engine
//                                                             (Microsoft.Cdn/profiles/ruleSets) — deferred,
//                                                             tracked in comment below.
// =====================================================================

@description('Project identifier for naming.')
param projectName string

@description('Custom domain for the demo (e.g. ai-intake.markandrewmarquez.com).')
param demoDomain string

@description('Front Door origin hostname — the static-website endpoint of the landing Storage Account, NOT the blob endpoint. Format: <accountname>.z<region-code>.web.<env>.core.windows.net. Resolved from the Storage Account\'s primaryEndpoints.web in main.bicep and passed in.')
param originHostname string

@description('Per-IP rate limit over a rolling window (Azure unit: requests per minute, see note in code).')
param wafRateLimitPer5Min int

@description('User-Agent header substrings to BLOCK at the Front Door edge.')
param blockedUserAgents array

@description('Storage Account resource ID where Front Door diagnostic settings (access logs) are exported.')
param diagnosticsStorageAccountId string

@description('Common tags.')
param tags object

// ---------- Front Door profile + endpoint ----------

// Standard SKU: $35/mo + per-request and egress costs. Premium adds Bot
// Manager + advanced WAF features (bot signatures, JS challenge); ~$330/mo,
// not worth it at portfolio scale. The TF CloudFront equivalent is
// PriceClass_100 (NA + EU only), which is a cost-optimization on CloudFront's
// global-by-default footprint; Standard Front Door is global by default
// at this SKU.
resource frontDoorProfile 'Microsoft.Cdn/profiles@2024-09-01' = {
  name: '${projectName}-fd'
  location: 'global'
  tags: tags
  sku: {
    name: 'Standard_AzureFrontDoor'
  }
  identity: {
    // System-assigned managed identity is what Front Door uses to access
    // the origin Storage Account via Storage RBAC — the Azure analog of
    // CloudFront OAC + SigV4.
    type: 'SystemAssigned'
  }
  properties: {
    originResponseTimeoutSeconds: 60
  }
}

// AFD endpoint: maps to a `<endpoint>-<hash>.<region>.azurefd.net` host.
// Custom-domain attachment below makes ai-intake.markandrewmarquez.com
// resolve to this endpoint.
resource frontDoorEndpoint 'Microsoft.Cdn/profiles/afdEndpoints@2024-09-01' = {
  parent: frontDoorProfile
  name: '${projectName}-endpoint'
  location: 'global'
  tags: tags
  properties: {
    enabledState: 'Enabled'
  }
}

// ---------- Origin group + origin (landing Storage Account) ----------

resource frontDoorOriginGroup 'Microsoft.Cdn/profiles/originGroups@2024-09-01' = {
  parent: frontDoorProfile
  name: 'landing-origin-group'
  properties: {
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 50
    }
    healthProbeSettings: {
      probePath: '/'
      probeRequestType: 'HEAD'
      probeProtocol: 'Https'
      probeIntervalInSeconds: 100
    }
    sessionAffinityState: 'Disabled'
  }
}

resource frontDoorOrigin 'Microsoft.Cdn/profiles/originGroups/origins@2024-09-01' = {
  parent: frontDoorOriginGroup
  name: 'landing-origin'
  properties: {
    // Static-website endpoint of the landing storage account, passed in
    // as `originHostname`. The blob endpoint won't serve static-website
    // content — Front Door must point at `<account>.z<region>.web.<env>.core.windows.net`,
    // which Azure exposes at storageAccount.properties.primaryEndpoints.web
    // (resolved in main.bicep).
    hostName: originHostname
    httpPort: 80
    httpsPort: 443
    originHostHeader: originHostname
    priority: 1
    weight: 1000
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
  }
}

// ---------- WAF policy (rate limit + UA block + managed rules) ----------

// Front Door WAF policies live in their own resource type; attached to
// the Front Door profile via security policies (below). The Standard
// SKU caps rate-limit rules at 1, managed rule sets at 1
// (Microsoft_DefaultRuleSet only), and matches custom-rules with Geo /
// IP / Size / String operators. The TF stack's 5-rule layering compresses
// to:
//   - 1 RateLimitRule (TF priority 1)
//   - 1 MatchRule of type StringMatch with Contains for UA block (TF priority 2)
//   - 1 ManagedRuleSet: Microsoft_DefaultRuleSet_2.1 (covers TF priorities 3-4)
//   - TF priority 5 (AmazonIpReputationList) has no direct Standard-SKU
//     analog — documented impedance. Would land via Microsoft_BotManagerRuleSet
//     on Premium.

// Azure's rate-limit window is per-minute (RateLimitDurationInMinutes) with
// a per-minute threshold. AWS WAFv2 uses a 5-minute window. The conversion:
// AWS_5min_limit / 5 ≈ Azure per-minute limit.
//
// Bicep's `/` is integer division on int operands, so this truncates. At
// the default 100/5 = 20, the result is exact; at non-multiples-of-5 the
// truncation is the intended floor (better to under-limit than over-limit
// when converting between window units). Documented for clarity.
var azureRateLimitPerMinute = wafRateLimitPer5Min / 5

resource wafPolicy 'Microsoft.Network/FrontDoorWebApplicationFirewallPolicies@2024-02-01' = {
  name: '${replace(projectName, '-', '')}edgewaf'
  location: 'global'
  tags: tags
  sku: {
    name: 'Standard_AzureFrontDoor'
  }
  properties: {
    policySettings: {
      enabledState: 'Enabled'
      mode: 'Prevention'
      requestBodyCheck: 'Enabled'
    }
    customRules: {
      rules: [
        {
          name: 'RateLimitPerIp'
          priority: 1
          enabledState: 'Enabled'
          ruleType: 'RateLimitRule'
          rateLimitDurationInMinutes: 1
          rateLimitThreshold: azureRateLimitPerMinute
          // Match on RequestUri "starts with /" — i.e. any request. Azure
          // requires a matchConditions block on RateLimitRule; matching all
          // requests is the way to apply the rule globally per-IP. An earlier
          // version of this file used `IPMatch 0.0.0.0/0 negateCondition=true`,
          // which inverts "all IPs" to "no IPs" and meant the rule never
          // fired (caught in CodeRabbit review).
          matchConditions: [
            {
              matchVariable: 'RequestUri'
              operator: 'BeginsWith'
              negateCondition: false
              matchValue: [
                '/'
              ]
            }
          ]
          action: 'Block'
        }
        {
          name: 'BlockKnownScrapeUserAgents'
          priority: 2
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          matchConditions: [
            {
              matchVariable: 'RequestHeader'
              selector: 'User-Agent'
              operator: 'Contains'
              negateCondition: false
              matchValue: blockedUserAgents
              transforms: [
                'Lowercase'
              ]
            }
          ]
          action: 'Block'
        }
      ]
    }
    managedRules: {
      managedRuleSets: [
        {
          ruleSetType: 'Microsoft_DefaultRuleSet'
          ruleSetVersion: '2.1'
          // The Standard SKU manages this rule set as a whole; per-rule
          // exclusions / overrides require Premium.
          ruleSetAction: 'Block'
        }
      ]
    }
  }
}

// ---------- Security policy: attach WAF to the AFD endpoint ----------

resource wafSecurityPolicy 'Microsoft.Cdn/profiles/securityPolicies@2024-09-01' = {
  parent: frontDoorProfile
  name: 'waf-security-policy'
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: {
        id: wafPolicy.id
      }
      associations: [
        {
          domains: [
            {
              id: frontDoorEndpoint.id
            }
          ]
          patternsToMatch: [
            '/*'
          ]
        }
      ]
    }
  }
}

// ---------- Custom domain ----------

resource frontDoorCustomDomain 'Microsoft.Cdn/profiles/customDomains@2024-09-01' = {
  parent: frontDoorProfile
  name: replace(demoDomain, '.', '-')
  properties: {
    hostName: demoDomain
    tlsSettings: {
      certificateType: 'ManagedCertificate'
      minimumTlsVersion: 'TLS12'
    }
  }
}

// ---------- Route: custom domain → origin group ----------

resource frontDoorRoute 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-09-01' = {
  parent: frontDoorEndpoint
  name: 'default-route'
  properties: {
    customDomains: [
      {
        id: frontDoorCustomDomain.id
      }
    ]
    originGroup: {
      id: frontDoorOriginGroup.id
    }
    supportedProtocols: [
      'Https'
    ]
    patternsToMatch: [
      '/*'
    ]
    forwardingProtocol: 'HttpsOnly'
    linkToDefaultDomain: 'Enabled'
    httpsRedirect: 'Enabled'
    enabledState: 'Enabled'
    cacheConfiguration: {
      compressionSettings: {
        contentTypesToCompress: [
          'text/html'
          'text/css'
          'application/javascript'
          'application/json'
          'image/svg+xml'
        ]
        isCompressionEnabled: true
      }
      queryStringCachingBehavior: 'IgnoreQueryString'
    }
  }
  dependsOn: [
    frontDoorOrigin
    wafSecurityPolicy
  ]
}

// ---------- Security headers (Rules Engine - deferred) ----------

// The AWS-managed SecurityHeadersPolicy (HSTS / X-Content-Type-Options /
// X-Frame-Options / Referrer-Policy / X-XSS-Protection) attached to the
// CloudFront distribution has no managed equivalent in Front Door. The
// Azure pattern is to define a Rules Engine
// (`Microsoft.Cdn/profiles/ruleSets`) with `ModifyResponseHeader` actions
// per header. Deferred from this PR — the Terraform stack's policy is
// a single managed reference (no actual rules in our IaC); adding the
// equivalent Bicep would require 5 rules of identical shape. Tracked
// as a follow-up for when (if) this Bicep tree is ever validated against
// an actual deployment.

// ---------- Diagnostic settings: access logs to Storage ----------

resource frontDoorDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: frontDoorProfile
  name: '${projectName}-fd-diag'
  properties: {
    storageAccountId: diagnosticsStorageAccountId
    logs: [
      {
        category: 'FrontDoorAccessLog'
        enabled: true
      }
      {
        category: 'FrontDoorHealthProbeLog'
        enabled: true
      }
      {
        category: 'FrontDoorWebApplicationFirewallLog'
        enabled: true
      }
    ]
  }
}

// ---------- Outputs ----------

@description('Front Door profile resource ID.')
output profileId string = frontDoorProfile.id

@description('Front Door endpoint default hostname (e.g. <endpoint>-<hash>.z01.azurefd.net).')
output endpointHostname string = frontDoorEndpoint.properties.hostName

@description('WAF policy resource ID.')
output wafPolicyId string = wafPolicy.id
