# -----------------------------------------------------------------------------
# MÓDULO EKS
# - Políticas IAM de mínimo privilegio por microservicio (siempre se crean):
#   cada servicio solo puede tocar SU tabla (Database-per-Service a nivel IAM).
# - Clúster EKS + node group en subredes PRIVADAS (solo si enable_eks = true).
#   En local se usa kind como sustituto del plano de datos; este módulo deja
#   declarada la topología equivalente para AWS real.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

locals {
  name = "${var.project}-${var.env}"
}

# ---------------- IAM mínimo privilegio por servicio -----------------
resource "aws_iam_policy" "service" {
  for_each    = var.service_tables
  name        = "${local.name}-${each.key}-policy"
  description = "${each.key}: acceso solo a sus tablas en ${var.env}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnTablesOnly"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
          "dynamodb:TransactWriteItems", "dynamodb:ConditionCheckItem"
        ]
        Resource = flatten([for arn in each.value : [arn, "${arn}/index/*"]])
      },
      {
        Sid      = "ReadFeatureFlags"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Scan"]
        Resource = [var.flags_table_arn]
      },
      {
        Sid      = "WriteOwnLogsAndMetrics"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "cloudwatch:PutMetricData"]
        Resource = ["*"]
      }
    ]
  })
  tags = var.tags
}

# ---------------- Clúster EKS (opcional) -----------------
data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  count              = var.enable_eks ? 1 : 0
  name               = "${local.name}-eks-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  count      = var.enable_eks ? 1 : 0
  role       = aws_iam_role.cluster[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "node" {
  count              = var.enable_eks ? 1 : 0
  name               = "${local.name}-eks-node-role"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = var.enable_eks ? toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ]) : toset([])
  role       = aws_iam_role.node[0].name
  policy_arn = each.value
}

resource "aws_eks_cluster" "this" {
  count    = var.enable_eks ? 1 : 0
  name     = "${local.name}-eks"
  role_arn = aws_iam_role.cluster[0].arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    security_group_ids      = [var.nodes_sg_id]
    endpoint_private_access = true  # API de K8s accesible desde la VPC
    endpoint_public_access  = false # y NO desde internet
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
  tags       = var.tags
}

resource "aws_eks_node_group" "this" {
  count           = var.enable_eks ? 1 : 0
  cluster_name    = aws_eks_cluster.this[0].name
  node_group_name = "${local.name}-ng"
  node_role_arn   = aws_iam_role.node[0].arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = [var.node_instance_type]

  scaling_config {
    desired_size = var.node_count
    min_size     = 1
    max_size     = var.node_count + 2
  }

  depends_on = [aws_iam_role_policy_attachment.node]
  tags       = var.tags
}
