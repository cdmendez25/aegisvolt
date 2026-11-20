variable "aws_endpoint" {
  description = "Endpoint de Floci. En AWS real se eliminan los endpoints del provider."
  type        = string
  default     = "http://localhost:4566"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

variable "enable_eks" {
  description = "Crear EKS real + VPC Link. En local (Floci) se usa kind."
  type        = bool
  default     = false
}

variable "enable_nat_gateway" {
  type    = bool
  default = false
}

variable "kind_node_host" {
  description = "Hostname del nodo kind alcanzable desde Floci (red docker 'kind')"
  type        = string
  default     = "aegisvolt-control-plane"
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
  default = [
    { station_id = "BOG-01", station_name = "Electrolinera Bogotá Norte", city = "Bogotá", connector_id = "C1", type = "FAST", power_kw = 50 },
    { station_id = "BOG-01", station_name = "Electrolinera Bogotá Norte", city = "Bogotá", connector_id = "C2", type = "FAST", power_kw = 50 },
    { station_id = "BOG-01", station_name = "Electrolinera Bogotá Norte", city = "Bogotá", connector_id = "C3", type = "ULTRA", power_kw = 350 },
    { station_id = "MDE-01", station_name = "Hub Logístico Medellín", city = "Medellín", connector_id = "C1", type = "FAST", power_kw = 60 },
    { station_id = "MDE-01", station_name = "Hub Logístico Medellín", city = "Medellín", connector_id = "C2", type = "ULTRA", power_kw = 300 },
    { station_id = "CDMX-01", station_name = "Centro de Carga Vallejo", city = "Ciudad de México", connector_id = "C1", type = "FAST", power_kw = 50 },
    { station_id = "CDMX-01", station_name = "Centro de Carga Vallejo", city = "Ciudad de México", connector_id = "C2", type = "FAST", power_kw = 50 },
    { station_id = "CDMX-01", station_name = "Centro de Carga Vallejo", city = "Ciudad de México", connector_id = "C3", type = "ULTRA", power_kw = 350 },
  ]
}

variable "seed_wallets" {
  type = map(object({
    name    = string
    balance = number
  }))
  default = {
    "FLEET-ANDES"    = { name = "Andes Última Milla", balance = 500 }
    "FLEET-PACIFICO" = { name = "Pacífico Reparto Urbano", balance = 150 }
    "FLEET-SINSALDO" = { name = "Flota Demo Sin Saldo", balance = 2 }
  }
}
