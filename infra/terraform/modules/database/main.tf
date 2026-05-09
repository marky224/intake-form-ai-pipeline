data "aws_caller_identity" "current" {}

# ---------- KMS CMK ----------

# Customer-managed key encrypting both the Aurora cluster volume and the
# Secrets Manager secret holding the master credentials. The key policy
# grants account root admin (standard AWS pattern that prevents lockout)
# and grants the Aurora service principal the encrypt/decrypt/grant ops
# it needs to manage cluster volumes and snapshots, gated by the
# kms:GrantIsForAWSResource condition so the principal can only create
# grants on behalf of AWS-managed resources.
data "aws_iam_policy_document" "kms_key" {
  statement {
    sid    = "AccountRootAdmin"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowAuroraServiceUse"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]
    resources = ["*"]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "aurora" {
  description             = "Aurora cluster + master-secret encryption key for ${var.name_prefix}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms_key.json

  tags = {
    Name = var.name_prefix
  }
}

resource "aws_kms_alias" "aurora" {
  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.aurora.key_id
}

# ---------- Master credentials ----------

# Aurora master password. Aurora rejects `/`, `@`, `"`, `'`, `\`, and space
# in master passwords, so override_special enumerates the safe set rather
# than relying on the random provider's default.
resource "random_password" "master" {
  length           = 32
  special          = true
  override_special = "!#$%&*+-.:;<=>?[]^_`{|}~"
}

resource "aws_secretsmanager_secret" "master" {
  name                    = "${var.name_prefix}/master"
  description             = "Aurora master credentials for ${var.name_prefix}"
  kms_key_id              = aws_kms_key.aurora.arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = {
    Name = "${var.name_prefix}/master"
  }
}

# Secret value carries cluster endpoint + port so consumers fetch
# everything they need with one secretsmanager:GetSecretValue call.
# The endpoint reference makes secret_version implicitly depend on the
# cluster, so first apply creates: random_password → cluster (with
# password from random_password) → secret_version (with cluster endpoint).
resource "aws_secretsmanager_secret_version" "master" {
  secret_id = aws_secretsmanager_secret.master.id
  secret_string = jsonencode({
    username = var.master_username
    password = random_password.master.result
    engine   = "postgres"
    host     = aws_rds_cluster.this.endpoint
    port     = aws_rds_cluster.this.port
    dbname   = var.database_name
  })
}

# ---------- Networking ----------

resource "aws_db_subnet_group" "this" {
  name       = var.name_prefix
  subnet_ids = var.private_subnet_ids

  tags = {
    Name = var.name_prefix
  }
}

# Cluster security group. No ingress rules in this PR — Lambda ingress
# lands when compute infrastructure does. Default egress (allow all) is
# kept; Aurora doesn't initiate outbound traffic, so it's a no-op.
resource "aws_security_group" "cluster" {
  name        = "${var.name_prefix}-cluster"
  description = "Aurora cluster ingress for ${var.name_prefix}. Ingress rules added when compute lands."
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.name_prefix}-cluster"
  }
}

# ---------- Cluster + parameter group ----------

# Cluster parameter group preloads pgvector. shared_preload_libraries
# is a static parameter, so apply_method must be pending-reboot — the
# first instance reboot after creation activates it. The cluster is
# created with this parameter group attached, so the boot of the first
# instance (which Terraform does in this same apply) picks it up.
resource "aws_rds_cluster_parameter_group" "this" {
  name        = "${var.name_prefix}-pg"
  family      = "aurora-postgresql16"
  description = "Aurora PostgreSQL 16 cluster parameter group with pgvector preload for ${var.name_prefix}"

  parameter {
    name         = "shared_preload_libraries"
    value        = "vector"
    apply_method = "pending-reboot"
  }

  tags = {
    Name = "${var.name_prefix}-pg"
  }
}

resource "aws_rds_cluster" "this" {
  cluster_identifier = var.name_prefix
  engine             = "aurora-postgresql"
  engine_version     = var.engine_version
  engine_mode        = "provisioned"
  database_name      = var.database_name
  master_username    = var.master_username
  master_password    = random_password.master.result

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.cluster.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.aurora.arn

  iam_database_authentication_enabled = true

  serverlessv2_scaling_configuration {
    min_capacity             = var.min_capacity
    max_capacity             = var.max_capacity
    seconds_until_auto_pause = var.seconds_until_auto_pause
  }

  backup_retention_period      = var.backup_retention_period
  preferred_backup_window      = "07:00-09:00"
  preferred_maintenance_window = "sun:09:00-sun:11:00"

  # deletion_protection = false and skip_final_snapshot = true are
  # portfolio choices: the cluster is recreatable from synthetic data,
  # so iteration friction matters more than accidental-destroy
  # protection. The deploy role's RDS scope is name-prefixed to project
  # resources, so the blast radius of a stray destroy is contained.
  deletion_protection = false
  skip_final_snapshot = true

  apply_immediately = true

  tags = {
    Name = var.name_prefix
  }
}

resource "aws_rds_cluster_instance" "this" {
  identifier         = "${var.name_prefix}-instance-1"
  cluster_identifier = aws_rds_cluster.this.id
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version
  instance_class     = "db.serverless"

  db_subnet_group_name = aws_db_subnet_group.this.name

  performance_insights_enabled = false
  monitoring_interval          = 0

  apply_immediately = true

  tags = {
    Name = "${var.name_prefix}-instance-1"
  }
}
