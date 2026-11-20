variable "project" { type = string }
variable "env" { type = string }
variable "retention_days" { type = number }
variable "services" {
  description = "Microservicios con métricas de error/5xx"
  type        = list(string)
}
variable "log_sources" {
  description = "Todo lo que escribe logs (servicios + canary + prefect)"
  type        = list(string)
}
variable "tags" {
  type    = map(string)
  default = {}
}

output "log_group_names" {
  value = { for k, g in aws_cloudwatch_log_group.service : k => g.name }
}
output "metrics_namespace" { value = local.namespace }
