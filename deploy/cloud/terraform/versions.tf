terraform {
  required_version = ">= 1.5.0, < 2.0.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 9.3.0"
    }
  }
}

# API signing credentials stay in the operator's private ~/.oci/config profile.
provider "oci" {
  tenancy_ocid        = var.tenancy_ocid
  region              = var.region
  config_file_profile = var.oci_config_profile
}
