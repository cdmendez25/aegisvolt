variable "project" { type = string }
variable "env" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}

variable "feature_flags" {
  type = map(object({
    enabled     = bool
    description = string
  }))
}

variable "seed_connectors" {
  type = list(object({
    station_id   = string
    station_name = string
    city         = string
    connector_id = string
    type         = string
    power_kw     = number
  }))
}

variable "seed_wallets" {
  type = map(object({
    name    = string
    balance = number
  }))
}

output "table_names" {
  value = merge(
    { for k, t in aws_dynamodb_table.service : k => t.name },
    { sagas = aws_dynamodb_table.sagas.name, flags = aws_dynamodb_table.flags.name }
  )
}

output "table_arns" {
  value = merge(
    { for k, t in aws_dynamodb_table.service : k => t.arn },
    { sagas = aws_dynamodb_table.sagas.arn, flags = aws_dynamodb_table.flags.arn }
  )
}
