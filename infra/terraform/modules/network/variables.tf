variable "project_name" {
  description = "Project identifier used for resource naming and the Name tag prefix."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. Default fits the locked CIDR plan: 10.0.0.0/24 each for 3 public subnets, 10.0.10-12.0/24 for 3 private subnets."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = var.vpc_cidr == "10.0.0.0/16"
    error_message = "Phase 2 currently supports only 10.0.0.0/16 — subnet CIDRs are hard-coded to slices of this block. Parameterize subnet derivation before allowing other VPC ranges."
  }
}

variable "azs" {
  description = "Availability zones to deploy into. Length determines the number of public/private subnet pairs (locked at 3 in Phase 2)."
  type        = list(string)

  validation {
    condition     = length(var.azs) == 3
    error_message = "azs must contain exactly 3 zone names — the network module's CIDR plan and route-table associations assume 3 AZs."
  }
}
