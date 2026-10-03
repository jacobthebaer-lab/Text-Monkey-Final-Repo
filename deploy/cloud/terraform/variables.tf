variable "tenancy_ocid" {
  type        = string
  nullable    = false
  description = "Exact tenancy OCID for the reviewed Always Free account."
  validation {
    condition     = can(regex("^ocid1\\.tenancy\\.", var.tenancy_ocid))
    error_message = "Supply the actual tenancy OCID."
  }
}

variable "compartment_ocid" {
  type        = string
  nullable    = false
  description = "Existing deployment compartment or tenancy root OCID. No compartment is created."
  validation {
    condition     = can(regex("^ocid1\\.(compartment|tenancy)\\.", var.compartment_ocid))
    error_message = "Supply an existing compartment or tenancy root OCID."
  }
}

variable "oci_config_profile" {
  type        = string
  nullable    = false
  description = "Explicit profile name in the operator's private OCI CLI/provider configuration."
  validation {
    condition     = length(trimspace(var.oci_config_profile)) > 0
    error_message = "Select an explicit private OCI configuration profile."
  }
}

variable "region" {
  type        = string
  nullable    = false
  description = "The account's existing home region, checked against OCI at plan time."
  validation {
    condition     = can(regex("^[a-z]+-[a-z0-9-]+-[0-9]+$", var.region))
    error_message = "Supply the actual OCI home-region name."
  }
}

variable "availability_domain" {
  type        = string
  nullable    = false
  description = "Exact existing AD name in that region. No automatic capacity fallback."
  validation {
    condition     = length(trimspace(var.availability_domain)) > 0
    error_message = "Choose an explicit availability domain in the home region."
  }
}

variable "ubuntu_image_ocid" {
  type        = string
  nullable    = false
  description = "Reviewed Oracle platform Canonical Ubuntu 24.04 ARM image OCID, not a Marketplace/custom image."
  validation {
    condition     = can(regex("^ocid1\\.image\\.", var.ubuntu_image_ocid))
    error_message = "Select an explicit Ubuntu ARM platform image OCID."
  }
}

variable "ssh_public_key" {
  type        = string
  nullable    = false
  description = "One OpenSSH public key. Never provide a private key."
  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}( [^\\r\\n]+)?$", trimspace(var.ssh_public_key)))
    error_message = "Supply a single OpenSSH public key, not a password or private key."
  }
}

variable "admin_ipv4_cidr" {
  type        = string
  nullable    = false
  description = "Administrator's exact public IPv4 address as a /32. No wildcard/range ingress."
  validation {
    condition     = can(cidrnetmask(var.admin_ipv4_cidr)) && endswith(var.admin_ipv4_cidr, "/32")
    error_message = "SSH ingress must be exactly one IPv4 address with /32 prefix."
  }
}

variable "free_tier_review" {
  nullable = false
  type = object({
    reviewed_at_utc             = string
    full_tenancy_reviewed       = bool
    always_free_console_checked = bool
    platform_image_checked      = bool
    other_a1_ocpus              = number
    other_a1_memory_gb          = number
    other_boot_and_block_gb     = number
    other_vcns                  = number
  })
  description = "Fresh reviewed totals across all tenancy compartments, excluding only this same Terraform deployment's existing resources. Include stopped instances and unattached/retained disks. This is an explicit review record, not automated inventory or a billing guarantee."
  validation {
    condition = try(
      var.free_tier_review.full_tenancy_reviewed &&
      var.free_tier_review.always_free_console_checked &&
      var.free_tier_review.platform_image_checked &&
      can(timeadd(var.free_tier_review.reviewed_at_utc, "30m")) &&
      alltrue([for amount in [
        var.free_tier_review.other_a1_ocpus,
        var.free_tier_review.other_a1_memory_gb,
        var.free_tier_review.other_boot_and_block_gb,
        var.free_tier_review.other_vcns
      ] : amount >= 0]),
      false
    )
    error_message = "Provide fresh, complete tenancy-wide usage totals and confirm Always Free eligibility and the platform image. Unknown/null totals and unchecked review are rejected."
  }
}
