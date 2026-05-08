module "documents_bucket" {
  source = "./modules/storage"

  bucket_name = local.documents_bucket_name
  purpose     = "documents"
}

module "artifacts_bucket" {
  source = "./modules/storage"

  bucket_name = local.artifacts_bucket_name
  purpose     = "artifacts"
}
