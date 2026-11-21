terraform {
  backend "s3" {
    bucket               = "aegisvolt-tfstate"
    key                  = "aegisvolt/terraform.tfstate"
    workspace_key_prefix = "env:"
    region               = "us-east-1"
    encrypt              = true

    use_lockfile = true

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
