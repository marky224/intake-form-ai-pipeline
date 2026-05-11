// =====================================================================
// modules/network.bicep — VNet + subnets + NAT + NSGs + Service Endpoints
//
// Mirrors infra/terraform/modules/network/main.tf.
//
// AWS  → Azure mapping:
//   VPC                        → VNet
//   Subnets (public/private)   → Subnets (NSG-tier-tagged)
//   IGW                        → Implicit (public subnets reach internet via Public IP / NAT)
//   NAT Gateway                → NAT Gateway (regional resource, attached to subnet)
//   EIP                        → Public IP (Standard SKU)
//   Default SG locked-down     → "deny-all" NSG attached at subnet level
//   Route Tables               → Route Tables (UDR), explicit even if Azure handles default routes
//   S3 / DynamoDB Gateway VPC Endpoints → Storage Service Endpoint on subnets
//                                + PostgreSQL Private Endpoint (separately, in database.bicep)
//
// One Azure-specific addition: a private DNS zone for the
// postgres.database.azure.com namespace so the Flexible Server's private
// endpoint resolves to its VNet IP from inside the VNet. The TF stack
// doesn't have a direct analog because Aurora's private DNS is auto-
// provisioned per-VPC.
// =====================================================================

@description('Project identifier for resource naming + the Project tag.')
param projectName string

@description('Azure region for regional resources.')
param location string

@description('CIDR block for the VNet.')
param vnetCidr string

@description('Common tags applied to all resources.')
param tags object

// Three subnets per tier mirror the TF stack's 3-AZ layout. Azure
// "availability zones" are similar but only some regions support them —
// `eastus` does. Subnet zone affinity is implicit (Azure subnets are
// regional, with per-zone assignments declared at the resource level for
// resources like NAT Gateway / VMSS that need it).
var publicSubnetCidrs = [
  cidrSubnet(vnetCidr, 24, 0)
  cidrSubnet(vnetCidr, 24, 1)
  cidrSubnet(vnetCidr, 24, 2)
]
var privateSubnetCidrs = [
  cidrSubnet(vnetCidr, 24, 10)
  cidrSubnet(vnetCidr, 24, 11)
  cidrSubnet(vnetCidr, 24, 12)
]
var databaseSubnetCidr = cidrSubnet(vnetCidr, 24, 20)

// NOTE on the TF stack's "deny-all default SG" analog: Azure has no
// concept of a "default NSG" that auto-attaches to subnets without an
// explicit one. The TF stack's `aws_default_security_group` lockdown
// addresses a specific AWS gotcha (the per-VPC default SG carries
// allow-all rules that auto-bind to resources without explicit SGs) —
// that gotcha doesn't exist on Azure, so this module doesn't carry an
// analog resource. Subnet-level NSGs are added per-tier when compute
// lands. (Earlier revision declared a `denyAllNsg` resource and never
// attached it to anything — caught in CodeRabbit review.)

// ---------- Public IP + NAT Gateway ----------

// Public IP must be Standard SKU + Static + zone-redundant for NAT Gateway
// attachment. Zone-redundant matches the multi-zone subnet layout.
resource natPublicIp 'Microsoft.Network/publicIPAddresses@2024-01-01' = {
  name: '${projectName}-nat-pip'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  zones: [
    '1'
    '2'
    '3'
  ]
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 4
  }
}

// Single NAT Gateway — same cost-optimization story as the TF stack
// (~$30/mo on its own). Azure NAT Gateways are zone-pinnable, but here
// we choose no-zone (regional) so it can serve all 3 private subnets
// without cross-zone egress costs. The TF stack lives in 1a's public
// subnet so private subnets in 1b/1c cross-AZ to reach it; the Azure
// equivalent of "AZ-affinity SPOF" doesn't apply because Azure NAT
// without zone declaration is regionally redundant by default.
resource natGateway 'Microsoft.Network/natGateways@2024-01-01' = {
  name: '${projectName}-nat'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [
      {
        id: natPublicIp.id
      }
    ]
  }
}

// ---------- Route table for private subnets ----------

// Azure injects a default 0.0.0.0/0 → Internet route on every subnet
// automatically. The NAT Gateway *override* of that default lives in the
// subnet's `natGateway` property (set below), not in a UDR. The UDR here
// exists for parity with the TF stack's explicit aws_route_table.private
// — it doesn't carry the egress route, but it does carry the explicit-
// no-internet rule that hardens private subnets against misconfigured
// 0.0.0.0/0 UDRs from a future overlay.
resource privateRouteTable 'Microsoft.Network/routeTables@2024-01-01' = {
  name: '${projectName}-private-rt'
  location: location
  tags: union(tags, {
    Tier: 'private'
  })
  properties: {
    disableBgpRoutePropagation: false
    routes: []
  }
}

// ---------- Private DNS zone for PostgreSQL Flexible Server ----------

// Flexible Server's private endpoint requires a private DNS zone with
// the exact name `<region-prefix>.postgres.database.azure.com` linked to
// the VNet for FQDN resolution to the private IP. Created here so the
// database module can attach to it; AWS analog is implicit (Aurora's
// per-VPC private DNS is provider-managed).
resource postgresPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

// ---------- VNet ----------

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${projectName}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetCidr
      ]
    }
    subnets: [
      // Public subnets (3, one per zone-implicit).
      {
        name: '${projectName}-public-1'
        properties: {
          addressPrefix: publicSubnetCidrs[0]
          // Service Endpoint on Storage so VM/AKS workloads in public subnets
          // can hit Storage Account "from VNet" without traversing the
          // public internet — closest free analog to the TF stack's S3
          // Gateway VPC Endpoint. Storage Service Endpoint is free; Private
          // Endpoints are ~$7.30/mo per endpoint.
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
        }
      }
      {
        name: '${projectName}-public-2'
        properties: {
          addressPrefix: publicSubnetCidrs[1]
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
        }
      }
      {
        name: '${projectName}-public-3'
        properties: {
          addressPrefix: publicSubnetCidrs[2]
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
        }
      }
      // Private subnets (3) — NAT-gateway egress, no public IPs on NICs.
      {
        name: '${projectName}-private-1'
        properties: {
          addressPrefix: privateSubnetCidrs[0]
          natGateway: {
            id: natGateway.id
          }
          routeTable: {
            id: privateRouteTable.id
          }
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
          // Disabled because these subnets host Private Endpoints in
          // follow-up work; PE creation fails on subnets with policies
          // enabled. The TF stack's gateway endpoints don't have this
          // gotcha because Gateway VPC Endpoints aren't a per-subnet
          // resource. (Caught in CodeRabbit review.)
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: '${projectName}-private-2'
        properties: {
          addressPrefix: privateSubnetCidrs[1]
          natGateway: {
            id: natGateway.id
          }
          routeTable: {
            id: privateRouteTable.id
          }
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
          // Disabled because these subnets host Private Endpoints in
          // follow-up work; PE creation fails on subnets with policies
          // enabled. The TF stack's gateway endpoints don't have this
          // gotcha because Gateway VPC Endpoints aren't a per-subnet
          // resource. (Caught in CodeRabbit review.)
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: '${projectName}-private-3'
        properties: {
          addressPrefix: privateSubnetCidrs[2]
          natGateway: {
            id: natGateway.id
          }
          routeTable: {
            id: privateRouteTable.id
          }
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
          // Disabled because these subnets host Private Endpoints in
          // follow-up work; PE creation fails on subnets with policies
          // enabled. The TF stack's gateway endpoints don't have this
          // gotcha because Gateway VPC Endpoints aren't a per-subnet
          // resource. (Caught in CodeRabbit review.)
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      // Delegated subnet for PostgreSQL Flexible Server. Azure requires
      // a dedicated subnet (no other resources) delegated to
      // Microsoft.DBforPostgreSQL/flexibleServers. The /24 here gives
      // 251 usable IPs (Azure reserves 5 per subnet), more than enough
      // for HA + read replicas at portfolio scale.
      {
        name: '${projectName}-postgres-delegated'
        properties: {
          addressPrefix: databaseSubnetCidr
          delegations: [
            {
              name: 'fs'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

// ---------- Link the private DNS zone to the VNet ----------

resource postgresPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresPrivateDnsZone
  name: '${projectName}-postgres-pdz-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// ---------- Outputs ----------

@description('VNet resource ID.')
output vnetId string = vnet.id

@description('VNet name.')
output vnetName string = vnet.name

@description('Resource ID of the delegated PostgreSQL Flexible Server subnet — passed to the database module.')
output databaseSubnetId string = '${vnet.id}/subnets/${projectName}-postgres-delegated'

@description('Private DNS zone resource ID for postgres.database.azure.com — passed to the database module so the Flexible Server can attach.')
output postgresPrivateDnsZoneId string = postgresPrivateDnsZone.id

@description('Public subnet resource IDs.')
output publicSubnetIds array = [
  '${vnet.id}/subnets/${projectName}-public-1'
  '${vnet.id}/subnets/${projectName}-public-2'
  '${vnet.id}/subnets/${projectName}-public-3'
]

@description('Private subnet resource IDs.')
output privateSubnetIds array = [
  '${vnet.id}/subnets/${projectName}-private-1'
  '${vnet.id}/subnets/${projectName}-private-2'
  '${vnet.id}/subnets/${projectName}-private-3'
]
