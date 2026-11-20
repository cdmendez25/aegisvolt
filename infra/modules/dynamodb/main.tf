# -----------------------------------------------------------------------------
# MÓDULO DYNAMODB  —  patrón Database-per-Service, separado por ambiente.
#
#   aegisvolt-reservations-<env>  -> station-reservation-service (único dueño)
#   aegisvolt-billing-<env>       -> billing-wallet-service      (único dueño)
#   aegisvolt-dispatch-<env>      -> energy-dispatch-service     (único dueño)
#   aegisvolt-sagas-<env>         -> orquestador SAGA (capa GraphQL)
#   aegisvolt-flags-<env>         -> Feature Flags del ambiente
#
# Ningún servicio lee la tabla de otro: se comunican solo por API.
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

locals {
  service_tables = {
    reservations = "station-reservation-service"
    billing      = "billing-wallet-service"
    dispatch     = "energy-dispatch-service"
  }
  is_prod = var.env == "prod"
}

resource "aws_dynamodb_table" "service" {
  for_each     = local.service_tables
  name         = "${var.project}-${each.key}-${var.env}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = local.is_prod
  }

  # En prod la tabla no se puede borrar ni con terraform destroy
  deletion_protection_enabled = local.is_prod

  tags = merge(var.tags, { Name = "${var.project}-${each.key}-${var.env}", OwnerService = each.value })
}

# Log de estado de cada SAGA (lo escribe el orquestador, lo audita Prefect)
resource "aws_dynamodb_table" "sagas" {
  name         = "${var.project}-sagas-${var.env}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "saga_id"

  attribute {
    name = "saga_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "idempotency_key"
    type = "S"
  }

  # Prefect consulta por estado para detectar SAGAs huérfanas/compensadas
  global_secondary_index {
    name            = "by_status"
    projection_type = "ALL"
    key_schema {
      attribute_name = "status"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
  }

  # Evita disparar dos SAGAs si el cliente reintenta la misma mutación
  global_secondary_index {
    name            = "by_idempotency_key"
    projection_type = "ALL"
    key_schema {
      attribute_name = "idempotency_key"
      key_type       = "HASH"
    }
  }

  point_in_time_recovery {
    enabled = local.is_prod
  }
  deletion_protection_enabled = local.is_prod

  tags = merge(var.tags, { Name = "${var.project}-sagas-${var.env}", OwnerService = "graphql-gateway" })
}

# Feature Flags: una tabla POR AMBIENTE => estado totalmente independiente
resource "aws_dynamodb_table" "flags" {
  name         = "${var.project}-flags-${var.env}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "flag_name"

  attribute {
    name = "flag_name"
    type = "S"
  }

  deletion_protection_enabled = local.is_prod
  tags                        = merge(var.tags, { Name = "${var.project}-flags-${var.env}" })
}

# Valor INICIAL de cada flag en este ambiente.
# ignore_changes = [item]: Terraform crea la flag pero NO revierte los cambios
# hechos en caliente (UI / aws dynamodb update-item). Así se cambia una flag
# sin redesplegar ni re-aplicar infraestructura.
resource "aws_dynamodb_table_item" "flag" {
  for_each   = var.feature_flags
  table_name = aws_dynamodb_table.flags.name
  hash_key   = aws_dynamodb_table.flags.hash_key

  item = jsonencode({
    flag_name   = { S = each.key }
    enabled     = { BOOL = each.value.enabled }
    description = { S = each.value.description }
    environment = { S = var.env }
    updated_by  = { S = "terraform" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# ---------------- Datos semilla -----------------
resource "aws_dynamodb_table_item" "connector" {
  for_each   = { for c in var.seed_connectors : "${c.station_id}#${c.connector_id}" => c }
  table_name = aws_dynamodb_table.service["reservations"].name
  hash_key   = "pk"
  range_key  = "sk"

  item = jsonencode({
    pk           = { S = "STATION#${each.value.station_id}" }
    sk           = { S = "CONNECTOR#${each.value.connector_id}" }
    entity       = { S = "CONNECTOR" }
    station_id   = { S = each.value.station_id }
    station_name = { S = each.value.station_name }
    city         = { S = each.value.city }
    connector_id = { S = each.value.connector_id }
    type         = { S = each.value.type }
    power_kw     = { N = tostring(each.value.power_kw) }
    status       = { S = "AVAILABLE" }
  })

  # El estado (AVAILABLE/RESERVED/CHARGING) lo maneja el microservicio en runtime
  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "wallet" {
  for_each   = var.seed_wallets
  table_name = aws_dynamodb_table.service["billing"].name
  hash_key   = "pk"
  range_key  = "sk"

  item = jsonencode({
    pk       = { S = "WALLET#${each.key}" }
    sk       = { S = "META" }
    entity   = { S = "WALLET" }
    fleet_id = { S = each.key }
    name     = { S = each.value.name }
    balance  = { N = tostring(each.value.balance) }
    currency = { S = "USD" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}
