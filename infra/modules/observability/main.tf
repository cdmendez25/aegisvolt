# -----------------------------------------------------------------------------
# MÓDULO OBSERVABILITY  —  CloudWatch por ambiente
#   Log groups:  /aegisvolt/<env>/<servicio>
#   Metric filters sobre los logs JSON (level, event, correlation_id)
#   Alarmas usadas como señal de salud del canary
# -----------------------------------------------------------------------------

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

locals {
  namespace = "AegisVolt/${var.env}"
}

resource "aws_cloudwatch_log_group" "service" {
  for_each          = toset(var.log_sources)
  name              = "/${var.project}/${var.env}/${each.key}"
  retention_in_days = var.retention_days
  tags              = merge(var.tags, { Service = each.key })
}

# Cuenta los logs con level=ERROR de cada microservicio
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each       = toset(var.services)
  name           = "${var.project}-${var.env}-${each.key}-errors"
  log_group_name = aws_cloudwatch_log_group.service[each.key].name
  pattern        = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name      = "LogErrors"
    namespace = local.namespace
    value     = "1"
    # (un filtro con dimensiones no admite default_value)
    dimensions = {
      Service = "$.service"
    }
  }
}

# Cuenta las SAGAs compensadas (rollback distribuido)
resource "aws_cloudwatch_log_metric_filter" "saga_compensated" {
  name           = "${var.project}-${var.env}-saga-compensated"
  log_group_name = aws_cloudwatch_log_group.service["graphql-gateway"].name
  pattern        = "{ $.event = \"SAGA_COMPENSATED\" }"

  metric_transformation {
    name          = "SagaCompensated"
    namespace     = local.namespace
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "saga_compensation_failed" {
  name           = "${var.project}-${var.env}-saga-compensation-failed"
  log_group_name = aws_cloudwatch_log_group.service["graphql-gateway"].name
  pattern        = "{ $.event = \"SAGA_COMPENSATION_FAILED\" }"

  metric_transformation {
    name          = "SagaCompensationFailed"
    namespace     = local.namespace
    value         = "1"
    default_value = "0"
  }
}

# Alarma: tasa de errores HTTP 5xx de cada servicio (métrica publicada por los
# propios pods con la dimensión Version=stable|canary). El análisis canary
# evalúa la misma métrica para la versión canary.
resource "aws_cloudwatch_metric_alarm" "http_5xx" {
  for_each            = toset(var.services)
  alarm_name          = "${var.project}-${var.env}-${each.key}-5xx"
  namespace           = local.namespace
  metric_name         = "Http5xx"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    Service = each.key
  }
  alarm_description = "Errores 5xx de ${each.key} en ${var.env}"
  tags              = var.tags
}

resource "aws_cloudwatch_metric_alarm" "saga_compensation_failed" {
  alarm_name          = "${var.project}-${var.env}-saga-compensation-failed"
  namespace           = local.namespace
  metric_name         = "SagaCompensationFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_description   = "Una SAGA no pudo compensarse: requiere intervención manual"
  tags                = var.tags
}
