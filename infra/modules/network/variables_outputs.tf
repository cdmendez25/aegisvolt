variable "project" { type = string }
variable "env" { type = string }
variable "region" { type = string }
variable "vpc_cidr" { type = string }
variable "availability_zones" { type = list(string) }
variable "graphql_node_port" { type = number }
variable "enable_nat_gateway" {
  type    = bool
  default = false
}
variable "tags" {
  type    = map(string)
  default = {}
}

output "vpc_id" { value = aws_vpc.this.id }
output "vpc_cidr" { value = aws_vpc.this.cidr_block }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "private_route_table_ids" { value = aws_route_table.private[*].id }
output "k8s_nodes_sg_id" { value = aws_security_group.k8s_nodes.id }
output "vpce_sg_id" { value = aws_security_group.vpce.id }
output "execute_api_vpce_id" { value = aws_vpc_endpoint.interface["execute-api"].id }
output "vpc_endpoint_ids" {
  value = merge(
    { s3 = aws_vpc_endpoint.s3.id, dynamodb = aws_vpc_endpoint.dynamodb.id },
    { for k, v in aws_vpc_endpoint.interface : k => v.id }
  )
}
