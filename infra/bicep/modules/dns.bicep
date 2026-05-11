// =====================================================================
// modules/dns.bicep — Azure DNS zone + records for the demo
//
// Mirrors infra/terraform/dns.tf.
//
// AWS  → Azure mapping:
//   var.route53_hosted_zone_id            → existing Microsoft.Network/dnsZones (assumed Mark-owned, out-of-band)
//   aws_acm_certificate (DNS-validated)   → Front Door managed certificate (lives in front-door.bicep)
//   aws_route53_record.cert_validation    → No analog (Front Door managed certs auto-validate via
//                                            a CNAME the platform asks the customer to create;
//                                            in a real deploy, the validation CNAME is added as
//                                            an A/CNAME record here once the customDomain resource
//                                            in Front Door reports its validation token)
//   aws_route53_record.demo_a (alias)     → Microsoft.Network/dnsZones/A (alias → AFD endpoint)
//   aws_route53_record.demo_aaaa (alias)  → Microsoft.Network/dnsZones/AAAA (alias → AFD endpoint)
//
// Azure DNS supports "alias records" that resolve to the runtime IP of
// an Azure resource (analog of Route 53 alias records pointing at a
// CloudFront distribution). The alias target is specified via
// `targetResource.id` on the recordset.
// =====================================================================

@description('Custom demo domain (must end in the hosted zone domain).')
param demoDomain string

@description('Front Door endpoint hostname (azurefd.net default).')
param frontDoorEndpointHostname string

@description('Front Door profile resource ID — used as the alias target on A/AAAA records.')
param frontDoorResourceId string

@description('Common tags.')
param tags object

// Split demoDomain into "subdomain" (the record name) and "zone" (the
// hosted zone). For ai-intake.markandrewmarquez.com:
//   subdomain = "ai-intake"
//   zone      = "markandrewmarquez.com"
//
// Bicep doesn't have a "split into N segments" helper; using `split` +
// index.
var demoDomainParts = split(demoDomain, '.')
var subdomain = demoDomainParts[0]
var zoneName = join(skip(demoDomainParts, 1), '.')

// ---------- DNS Zone (existing — owned out-of-band like Route 53) ----------

// `existing` retrieves a reference to an already-deployed DNS zone
// without managing it from this stack. Mirrors the TF stack's
// var.route53_hosted_zone_id pattern.
resource dnsZone 'Microsoft.Network/dnsZones@2018-05-01' existing = {
  name: zoneName
}

// ---------- A record (alias → Front Door endpoint) ----------

// Azure DNS resolves A-alias records to the IPv4 anycast IPs of the
// target Front Door endpoint at query time, same as Route 53 alias
// records at CloudFront. TTL of 60s matches the TF stack's
// cert_validation TTL.
resource demoA 'Microsoft.Network/dnsZones/A@2018-05-01' = {
  parent: dnsZone
  name: subdomain
  properties: {
    TTL: 60
    targetResource: {
      id: frontDoorResourceId
    }
    metadata: {
      project: tags.Project
      managedBy: 'bicep'
    }
  }
}

// ---------- AAAA record (alias → Front Door endpoint) ----------

resource demoAAAA 'Microsoft.Network/dnsZones/AAAA@2018-05-01' = {
  parent: dnsZone
  name: subdomain
  properties: {
    TTL: 60
    targetResource: {
      id: frontDoorResourceId
    }
    metadata: {
      project: tags.Project
      managedBy: 'bicep'
    }
  }
}

// ---------- Outputs ----------

@description('Fully-qualified DNS name of the demo record (subdomain + zone).')
output demoDomainFqdn string = '${subdomain}.${zoneName}'

@description('Front Door endpoint the A/AAAA records resolve to (informational).')
output aliasTargetHostname string = frontDoorEndpointHostname
