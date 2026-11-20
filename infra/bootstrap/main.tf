# -----------------------------------------------------------------------------
# BOOTSTRAP: crea el backend remoto donde viven los tfstate de dev/test/prod.
#
# Es el único módulo con estado LOCAL (huevo y gallina: el bucket que guarda
# los estados no puede guardar su propio estado antes de existir).
# Se ejecuta una sola vez:  terraform -chdir=infra/bootstrap init && apply
# -----------------------------------------------------------------------------

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "aws_endpoint" {
  description = "Endpoint de Floci (emulador AWS). Vacío = AWS real."
  type        = string
  default     = "http://localhost:4566"
}

variable "state_bucket_name" {
  type    = string
  default = "aegisvolt-tfstate"
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    s3       = var.aws_endpoint
    dynamodb = var.aws_endpoint
    sts      = var.aws_endpoint
    iam      = var.aws_endpoint
  }
}

# Bucket de estados remotos
resource "aws_s3_bucket" "tfstate" {
  bucket = var.state_bucket_name

  # Nunca destruir por accidente el bucket que contiene la historia de la infra
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project   = "aegisvolt"
    Purpose   = "terraform-remote-state"
    ManagedBy = "terraform"
  }
}

# VERSIONAMIENTO: cada `terraform apply` crea una nueva versión del objeto
# env:/<workspace>/aegisvolt/terraform.tfstate y las anteriores quedan como
# backups recuperables (list-object-versions + get-object --version-id).
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Retener backups antiguos 90 días (las versiones no actuales se limpian luego)
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
  depends_on = [aws_s3_bucket_versioning.tfstate]
}

# Nota: el lock de estado usa `use_lockfile = true` del backend S3
# (archivo <state>.tflock con escritura condicional), no requiere DynamoDB.

output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

