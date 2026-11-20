variable "project" { type = string }
variable "env" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "execute_api_vpce_id" { type = string }
variable "graphql_node_port" { type = number }
variable "enable_vpc_link" {
  description = "true en AWS real (VPC Link -> NLB interno -> NodePort EKS)"
  type        = bool
  default     = false
}
variable "backend_base_url" {
  description = "Backend directo cuando no hay VPC Link (Floci -> NodePort del nodo kind)"
  type        = string
}
variable "tags" {
  type    = map(string)
  default = {}
}

output "rest_api_id" { value = aws_api_gateway_rest_api.this.id }
output "stage_name" { value = aws_api_gateway_stage.this.stage_name }
output "nlb_dns_name" { value = aws_lb.internal.dns_name }
