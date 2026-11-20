output "environment" {
  value = local.env
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "vpc_endpoints" {
  value = module.network.vpc_endpoint_ids
}

output "dynamodb_tables" {
  value = module.dynamodb.table_names
}

output "log_groups" {
  value = module.observability.log_group_names
}

output "api_gateway_rest_api_id" {
  value = module.apigateway.rest_api_id
}

output "api_gateway_invoke_url" {
  description = "Invocación en Floci (path-style execute-api)"
  value       = "${var.aws_endpoint}/execute-api/${module.apigateway.rest_api_id}/${module.apigateway.stage_name}/graphql"
}

output "graphql_node_port" {
  value = local.cfg.graphql_node_port
}

output "service_iam_policies" {
  value = module.eks.service_policy_arns
}
