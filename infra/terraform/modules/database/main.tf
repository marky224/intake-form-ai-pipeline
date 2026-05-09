data "aws_caller_identity" "current" {}

# ---------- KMS CMK ----------

# Customer-managed key encrypting the Aurora cluster volume, the
# AWS-managed master-credentials secret, and the CloudWatch log group
# for postgresql exports. The key policy grants account root admin
# (standard AWS pattern that prevents lockout) and grants the Aurora
# service principal the encrypt/decrypt/grant ops it needs to manage
# cluster volumes, snapshots, and the AWS-managed secret, gated by the
# kms:GrantIsForAWSResource condition. The CloudWatch Logs service
# principal is granted the same encrypt/decrypt set so the log group
# can be encrypted under the CMK.
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

  # CloudWatch Logs needs encrypt/decrypt to write to the log group
  # encrypted under this CMK. The condition restricts the grant to
  # the project's log groups only — Logs evaluates kms:EncryptionContext
  # against `aws:logs:arn` which AWS sets to the log-group ARN being
  # written to.
  statement {
    sid    = "AllowCloudWatchLogsServiceUse"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_caller_identity.current.account_id}.amazonaws.com"]
    }
    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/aws/rds/cluster/${var.name_prefix}*"]
    }
  }
}

resource "aws_kms_key" "aurora" {
  description             = "Aurora cluster + master-secret + log-group encryption key for ${var.name_prefix}"
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

# ---------- Networking ----------

resource "aws_db_subnet_group" "this" {
  name       = var.name_prefix
  subnet_ids = var.private_subnet_ids

  tags = {
    Name = var.name_prefix
  }
}

# Cluster security group. No ingress rules in this PR — Lambda ingress
# lands when compute infrastructure does. Terraform's aws_security_group
# resource intentionally removes AWS's default allow-all egress rule
# (per provider docs), so this SG ends up with no egress either.
# That's fine for Aurora: the cluster doesn't initiate outbound traffic
# from its network interface — backups, snapshots, and Secrets Manager
# integration all run over AWS service-managed pathways, not through the
# cluster SG.
resource "aws_security_group" "cluster" {
  name        = "${var.name_prefix}-cluster"
  description = "Aurora cluster ingress for ${var.name_prefix}. No ingress rules; Lambda/bastion ingress added when compute lands. No egress rules either — Aurora uses AWS service-managed pathways for outbound."
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.name_prefix}-cluster"
  }
}

# ---------- Cluster + parameter group ----------

# Cluster parameter group preloads pgvector and enforces SSL/TLS for
# all client connections. shared_preload_libraries is a static
# parameter (apply_method = pending-reboot), activated on the first
# instance reboot. rds.force_ssl is dynamic (immediate); enabling it
# means the cluster rejects unencrypted connections at the protocol
# level — defense-in-depth on top of the cluster being in private
# subnets with scoped SG ingress.
resource "aws_rds_cluster_parameter_group" "this" {
  name        = "${var.name_prefix}-pg"
  family      = "aurora-postgresql16"
  description = "Aurora PostgreSQL 16 cluster parameter group for ${var.name_prefix} — pgvector preload + force_ssl"

  parameter {
    name         = "shared_preload_libraries"
    value        = "vector"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  tags = {
    Name = "${var.name_prefix}-pg"
  }
}

# CloudWatch log group for the cluster's postgresql audit/error log
# export. RDS writes to `/aws/rds/cluster/<cluster-id>/<log-type>` by
# default, creating the group implicitly with no retention if it
# doesn't exist. Creating it explicitly in Terraform lets us set
# retention + CMK encryption + tags. The cluster references this
# implicitly via enabled_cloudwatch_logs_exports — Aurora detects the
# existing log group on first export and uses it without re-creating.
resource "aws_cloudwatch_log_group" "aurora_postgresql" {
  name              = "/aws/rds/cluster/${var.name_prefix}/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.aurora.arn

  tags = {
    Name = "/aws/rds/cluster/${var.name_prefix}/postgresql"
  }
}

# Aurora Serverless v2 cluster with AWS-managed master password
# (manage_master_user_password = true). RDS provisions a Secrets
# Manager secret named `rds!cluster-<UUID>-<suffix>`, encrypts it
# with the same CMK as the cluster volume, and rotates it every 7
# days by default. The password never enters Terraform state — that's
# the security win over the explicit random_password + secret_version
# pattern that previously lived here.
resource "aws_rds_cluster" "this" {
  cluster_identifier = var.name_prefix
  engine             = "aurora-postgresql"
  engine_version     = var.engine_version
  engine_mode        = "provisioned"
  database_name      = var.database_name
  master_username    = var.master_username

  # AWS-managed master password (Tier 1 security improvement). Eliminates
  # password from Terraform state entirely. Default rotation cadence is
  # 7 days — security-first; cannot be fully disabled, only the interval
  # tunable. The KMS key choice is permanent (AWS doesn't allow changing
  # it after the cluster is managing the secret), so this CMK is locked
  # in for the secret's life.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.aurora.arn

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.cluster.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.aurora.arn

  iam_database_authentication_enabled = true

  # Postgresql audit/error logs exported to the CloudWatch log group
  # above (Tier 1 security improvement #3). depends_on ensures the log
  # group exists before the cluster starts trying to export — otherwise
  # Aurora would auto-create the group with default settings (no
  # retention, no CMK encryption).
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # AWS rejects seconds_until_auto_pause when min_capacity is non-zero
  # ("SecondsUntilAutoPause can only be specified when minimum capacity
  # is 0" — auto-pause is incompatible with always-warm clusters). Pass
  # null in that case so the module behaves correctly when callers raise
  # min_capacity for an always-warm config without having to also unset
  # seconds_until_auto_pause.
  serverlessv2_scaling_configuration {
    min_capacity             = var.min_capacity
    max_capacity             = var.max_capacity
    seconds_until_auto_pause = var.min_capacity == 0 ? var.seconds_until_auto_pause : null
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

  depends_on = [aws_cloudwatch_log_group.aurora_postgresql]

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
