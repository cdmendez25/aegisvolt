terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

locals {
  name        = "${var.project}-${var.env}"
  backend_uri = var.enable_vpc_link ? "http://${aws_lb.internal.dns_name}:${var.graphql_node_port}" : var.backend_base_url
}

resource "aws_lb" "internal" {
  name               = "${local.name}-gql-nlb"
  internal           = true
  load_balancer_type = "network"
  subnets            = var.private_subnet_ids
  tags               = var.tags
}

resource "aws_lb_target_group" "graphql" {
  name        = "${local.name}-gql-tg"
  port        = var.graphql_node_port
  protocol    = "TCP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    port     = "traffic-port"
  }
  tags = var.tags
}

resource "aws_lb_listener" "graphql" {
  load_balancer_arn = aws_lb.internal.arn
  port              = var.graphql_node_port
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.graphql.arn
  }
}

resource "aws_api_gateway_vpc_link" "this" {
  count       = var.enable_vpc_link ? 1 : 0
  name        = "${local.name}-vpclink"
  target_arns = [aws_lb.internal.arn]
  tags        = var.tags
}

resource "aws_api_gateway_rest_api" "this" {
  name        = "${local.name}-api"
  description = "AegisVolt GraphQL API (${var.env})"

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [var.execute_api_vpce_id]
  }

  tags = var.tags
}

resource "aws_api_gateway_rest_api_policy" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.this.execution_arn}/*"
      },
      {
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.this.execution_arn}/*"
        Condition = {
          StringNotEquals = { "aws:SourceVpce" = var.execute_api_vpce_id }
        }
      }
    ]
  })
}

locals {
  routes = {
    graphql = "graphql"
    healthz = "healthz"
  }
}

resource "aws_api_gateway_resource" "route" {
  for_each    = local.routes
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = each.value
}

resource "aws_api_gateway_method" "route" {
  for_each      = local.routes
  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = aws_api_gateway_resource.route[each.key].id
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "route" {
  for_each                = local.routes
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = aws_api_gateway_resource.route[each.key].id
  http_method             = aws_api_gateway_method.route[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "ANY"
  uri                     = "${local.backend_uri}/${each.value}"
  connection_type         = var.enable_vpc_link ? "VPC_LINK" : "INTERNET"
  connection_id           = var.enable_vpc_link ? aws_api_gateway_vpc_link.this[0].id : null
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_integration.route,
      aws_api_gateway_rest_api_policy.this.policy,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "this" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.env
  tags          = var.tags
}
