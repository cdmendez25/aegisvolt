terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region                      = var.region
  access_key                  = "test"
  secret_key                  = "test"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    apigateway = var.aws_endpoint
    cloudwatch = var.aws_endpoint
    dynamodb   = var.aws_endpoint
    ec2        = var.aws_endpoint
    eks        = var.aws_endpoint
    elbv2      = var.aws_endpoint
    iam        = var.aws_endpoint
    logs       = var.aws_endpoint
    s3         = var.aws_endpoint
    sts        = var.aws_endpoint
  }

  default_tags {
    tags = local.tags
  }
}

# -----------------------------------------------------------------------------
# El AMBIENTE sale del WORKSPACE activo. No existe variable "env": así es
# imposible aplicar la config de prod estando en el workspace test.
# -----------------------------------------------------------------------------
locals {
  project = "aegisvolt"
  env     = terraform.workspace

  # Configuración por ambiente. Si el workspace no es dev/test/prod
  # (ej. "default") el plan falla: no hay entrada en este mapa.
  env_config = {
    dev = {
      vpc_cidr          = "10.10.0.0/16"
      graphql_node_port = 30081
      log_retention     = 7
      flags = {
        ENABLE_DYNAMIC_SURGE_PRICING    = true
        ENABLE_FAST_CHARGE_BOOST        = true
        SIMULATE_CHARGER_HARDWARE_FAULT = false
      }
    }
    test = {
      vpc_cidr          = "10.20.0.0/16"
      graphql_node_port = 30082
      log_retention     = 14
      flags = {
        ENABLE_DYNAMIC_SURGE_PRICING    = true
        ENABLE_FAST_CHARGE_BOOST        = true
        SIMULATE_CHARGER_HARDWARE_FAULT = false
      }
    }
    prod = {
      vpc_cidr          = "10.30.0.0/16"
      graphql_node_port = 30083
      log_retention     = 30
      flags = {
        ENABLE_DYNAMIC_SURGE_PRICING    = false
        ENABLE_FAST_CHARGE_BOOST        = false
        SIMULATE_CHARGER_HARDWARE_FAULT = false
      }
    }
  }

  cfg = local.env_config[local.env]

  flag_descriptions = {
    ENABLE_DYNAMIC_SURGE_PRICING    = "Tarifa dinámica (x1.5) en horas pico"
    ENABLE_FAST_CHARGE_BOOST        = "Permite reservar conectores de ultra-carga (ULTRA) con prioridad"
    SIMULATE_CHARGER_HARDWARE_FAULT = "Inyección de fallos: el cargador reporta falla física (dispara compensación SAGA)"
  }

  services = ["station-reservation-service", "billing-wallet-service", "energy-dispatch-service", "graphql-gateway"]

  tags = {
    Project     = local.project
    Environment = local.env
    Workspace   = terraform.workspace
    ManagedBy   = "terraform"
  }
}

module "network" {
  source             = "../modules/network"
  project            = local.project
  env                = local.env
  region             = var.region
  vpc_cidr           = local.cfg.vpc_cidr
  availability_zones = var.availability_zones
  graphql_node_port  = local.cfg.graphql_node_port
  enable_nat_gateway = var.enable_nat_gateway
  tags               = local.tags
}

module "dynamodb" {
  source  = "../modules/dynamodb"
  project = local.project
  env     = local.env
  tags    = local.tags

  feature_flags = {
    for name, enabled in local.cfg.flags : name => {
      enabled     = enabled
      description = local.flag_descriptions[name]
    }
  }
  seed_connectors = var.seed_connectors
  seed_wallets    = var.seed_wallets
}

module "observability" {
  source         = "../modules/observability"
  project        = local.project
  env            = local.env
  retention_days = local.cfg.log_retention
  services       = local.services
  log_sources    = concat(local.services, ["canary-analysis", "prefect-observability"])
  tags           = local.tags
}

module "eks" {
  source             = "../modules/eks"
  project            = local.project
  env                = local.env
  enable_eks         = var.enable_eks
  private_subnet_ids = module.network.private_subnet_ids
  nodes_sg_id        = module.network.k8s_nodes_sg_id
  flags_table_arn    = module.dynamodb.table_arns["flags"]
  service_tables = {
    station-reservation-service = [module.dynamodb.table_arns["reservations"]]
    billing-wallet-service      = [module.dynamodb.table_arns["billing"]]
    energy-dispatch-service     = [module.dynamodb.table_arns["dispatch"]]
    graphql-gateway             = [module.dynamodb.table_arns["sagas"], module.dynamodb.table_arns["flags"]]
  }
  tags = local.tags
}

module "apigateway" {
  source              = "../modules/apigateway"
  project             = local.project
  env                 = local.env
  vpc_id              = module.network.vpc_id
  private_subnet_ids  = module.network.private_subnet_ids
  execute_api_vpce_id = module.network.execute_api_vpce_id
  graphql_node_port   = local.cfg.graphql_node_port
  enable_vpc_link     = var.enable_eks
  backend_base_url    = "http://${var.kind_node_host}:${local.cfg.graphql_node_port}"
  tags                = local.tags
}
