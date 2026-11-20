# -----------------------------------------------------------------------------
# BACKEND REMOTO S3 (versionado) + WORKSPACES
#
# Con workspaces, el backend S3 guarda cada ambiente en una ruta distinta:
#   s3://aegisvolt-tfstate/env:/dev/aegisvolt/terraform.tfstate
#   s3://aegisvolt-tfstate/env:/test/aegisvolt/terraform.tfstate
#   s3://aegisvolt-tfstate/env:/prod/aegisvolt/terraform.tfstate
# => un apply en "test" jamás lee ni escribe el estado de "prod".
# El bucket tiene versionamiento: cada apply deja la versión anterior como backup.
# -----------------------------------------------------------------------------
terraform {
  backend "s3" {
    bucket               = "aegisvolt-tfstate"
    key                  = "aegisvolt/terraform.tfstate"
    workspace_key_prefix = "env:"
    region               = "us-east-1"
    encrypt              = true

    # Lock por workspace (archivo .tflock junto al estado, escritura condicional S3)
    use_lockfile = true

    # --- Floci (emulador local de AWS) ---
    endpoints = {
      s3       = "http://localhost:4566"
      dynamodb = "http://localhost:4566"
      sts      = "http://localhost:4566"
    }
    access_key                  = "test"
    secret_key                  = "test"
    use_path_style              = true
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    skip_region_validation      = true
    skip_s3_checksum            = true
  }
}
