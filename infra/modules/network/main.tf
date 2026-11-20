# -----------------------------------------------------------------------------
# MÓDULO NETWORK
# VPC dedicada por ambiente, 2 subredes públicas + 2 privadas (2 AZ),
# tablas de ruteo separadas y VPC Endpoints para que el tráfico interno
# (API Gateway <-> EKS, pods -> DynamoDB/S3/CloudWatch) NUNCA salga a internet.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

locals {
  name = "${var.project}-${var.env}"
  azs  = slice(var.availability_zones, 0, 2)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true # requerido para private DNS de Interface VPCE
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = "${local.name}-vpc" })
}

# ---------------- Subredes -----------------
# Públicas: 10.X.0.0/24, 10.X.1.0/24  (NAT, balanceadores públicos)
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = merge(var.tags, {
    Name                     = "${local.name}-public-${local.azs[count.index]}"
    Tier                     = "public"
    "kubernetes.io/role/elb" = "1"
  })
}

# Privadas: 10.X.10.0/24, 10.X.11.0/24  (nodos EKS, NLB interno, Interface VPCE)
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]
  tags = merge(var.tags, {
    Name                              = "${local.name}-private-${local.azs[count.index]}"
    Tier                              = "private"
    "kubernetes.io/role/internal-elb" = "1"
  })
}

# ---------------- Ruteo público -----------------
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${local.name}-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${local.name}-rt-public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---------------- Ruteo privado -----------------
# Las subredes privadas NO tienen ruta a 0.0.0.0/0 vía IGW. Solo:
#   - ruta local de la VPC (implícita)
#   - prefix lists de S3 y DynamoDB (agregadas por los Gateway Endpoints)
#   - NAT opcional (solo si se necesita salir a internet, ej. pull de imágenes)
resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${local.name}-rt-private-${local.azs[count.index]}" })
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? 1 : 0
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${local.name}-nat-eip" })
}

resource "aws_nat_gateway" "this" {
  count         = var.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = merge(var.tags, { Name = "${local.name}-nat" })
}

resource "aws_route" "private_nat" {
  count                  = var.enable_nat_gateway ? 2 : 0
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

# ---------------- Security Groups -----------------
# SG de los Interface Endpoints: solo HTTPS desde dentro de la VPC
resource "aws_security_group" "vpce" {
  name        = "${local.name}-vpce-sg"
  description = "Interface VPC endpoints: HTTPS solo desde la VPC"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS desde la VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(var.tags, { Name = "${local.name}-vpce-sg" })
}

# SG de los nodos de Kubernetes: aceptan el NodePort del gateway GraphQL
# solamente desde las subredes privadas (donde vive el NLB del VPC Link).
resource "aws_security_group" "k8s_nodes" {
  name        = "${local.name}-k8s-nodes-sg"
  description = "Nodos EKS: NodePort GraphQL solo desde subredes privadas"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "NodePort GraphQL gateway desde NLB interno"
    from_port   = var.graphql_node_port
    to_port     = var.graphql_node_port
    protocol    = "tcp"
    cidr_blocks = aws_subnet.private[*].cidr_block
  }

  ingress {
    description = "Trafico entre nodos/pods del cluster"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "Salida (hacia VPCE de DynamoDB/S3/Logs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${local.name}-k8s-nodes-sg" })
}

# ---------------- VPC Endpoints -----------------
# GATEWAY endpoints (S3 y DynamoDB): agregan una ruta con el prefix list del
# servicio en las tablas de ruteo PRIVADAS. Sin costo, sin ENI.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = merge(var.tags, { Name = "${local.name}-vpce-s3" })
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  # Política: este endpoint solo permite acceder a las tablas de ESTE ambiente
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyThisEnvironmentTables"
      Effect    = "Allow"
      Principal = "*"
      Action    = "dynamodb:*"
      Resource  = "arn:aws:dynamodb:${var.region}:*:table/${var.project}-*-${var.env}*"
    }]
  })

  tags = merge(var.tags, { Name = "${local.name}-vpce-dynamodb" })
}

# INTERFACE endpoints: crean ENIs con IP privada en las subredes privadas y,
# con private_dns_enabled, el DNS público del servicio resuelve a esas IPs.
locals {
  interface_endpoints = {
    execute-api = "com.amazonaws.${var.region}.execute-api" # API Gateway privado
    logs        = "com.amazonaws.${var.region}.logs"        # CloudWatch Logs
    monitoring  = "com.amazonaws.${var.region}.monitoring"  # CloudWatch Metrics
    sts         = "com.amazonaws.${var.region}.sts"         # IRSA de los pods
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = local.interface_endpoints
  vpc_id              = aws_vpc.this.id
  service_name        = each.value
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true
  tags                = merge(var.tags, { Name = "${local.name}-vpce-${each.key}" })
}
