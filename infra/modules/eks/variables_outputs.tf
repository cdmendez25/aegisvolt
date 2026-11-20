variable "project" { type = string }
variable "env" { type = string }
variable "enable_eks" {
  type    = bool
  default = false
}
variable "kubernetes_version" {
  type    = string
  default = "1.33"
}
variable "private_subnet_ids" { type = list(string) }
variable "nodes_sg_id" { type = string }
variable "node_instance_type" {
  type    = string
  default = "t3.medium"
}
variable "node_count" {
  type    = number
  default = 2
}
variable "service_tables" {
  description = "servicio => lista de ARNs de tablas que le pertenecen"
  type        = map(list(string))
}
variable "flags_table_arn" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}

output "service_policy_arns" {
  value = { for k, p in aws_iam_policy.service : k => p.arn }
}
output "cluster_name" {
  value = var.enable_eks ? aws_eks_cluster.this[0].name : "kind-aegisvolt (local)"
}
