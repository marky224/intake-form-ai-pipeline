module "database" {
  source = "./modules/database"

  name_prefix        = "${var.project_name}-aurora"
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
}
