locals {
  # Deliberately fixed: one small ARM VM; no shape/count/paid fallback inputs.
  vm_shape   = "VM.Standard.A1.Flex"
  vm_ocpus   = 1
  vm_ram_gb  = 6
  vm_boot_gb = 50
  tags       = { Project = "TextMonkey", Purpose = "isolated-cloud-experiment" }
}

data "oci_identity_region_subscriptions" "tenancy" {
  tenancy_id = var.tenancy_ocid
}

data "oci_identity_availability_domains" "home" {
  compartment_id = var.tenancy_ocid
}

data "oci_core_image" "ubuntu" {
  image_id = var.ubuntu_image_ocid
}

data "oci_core_image_shapes" "ubuntu" {
  image_id = var.ubuntu_image_ocid
}

# Every mutating resource depends on this guard through the VCN. Missing
# permissions or failed read-only eligibility checks stop before VM creation.
resource "terraform_data" "review" {
  input = var.free_tier_review

  lifecycle {
    precondition {
      condition = contains([
        for subscription in data.oci_identity_region_subscriptions.tenancy.region_subscriptions :
        subscription.region_name if subscription.is_home_region
      ], var.region)
      error_message = "Only the actual tenancy home region is permitted."
    }
    precondition {
      condition     = contains([for ad in data.oci_identity_availability_domains.home.availability_domains : ad.name], var.availability_domain)
      error_message = "The selected availability domain is not in the configured home region."
    }
    precondition {
      condition = (
        data.oci_core_image.ubuntu.operating_system == "Canonical Ubuntu" &&
        data.oci_core_image.ubuntu.operating_system_version == "24.04" &&
        data.oci_core_image.ubuntu.listing_type == "NONE" &&
        data.oci_core_image.ubuntu.size_in_mbs <= local.vm_boot_gb * 1024 &&
        contains([for compatible in data.oci_core_image_shapes.ubuntu.image_shape_compatibilities : compatible.shape], local.vm_shape)
      )
      error_message = "Select a reviewed, non-Marketplace Ubuntu 24.04 platform image compatible with A1 and a 50 GB boot disk."
    }
    precondition {
      condition = (
        var.free_tier_review.other_a1_ocpus + local.vm_ocpus <= 2 &&
        var.free_tier_review.other_a1_memory_gb + local.vm_ram_gb <= 12 &&
        var.free_tier_review.other_boot_and_block_gb + local.vm_boot_gb <= 200 &&
        var.free_tier_review.other_vcns + 1 <= 2
      )
      error_message = "Existing resources plus this deployment exceed the reviewed Always Free limits (2 OCPU, 12 GB RAM, 200 GB boot/block, 2 VCNs). Do not use a paid fallback."
    }
    precondition {
      condition = (
        timecmp(var.free_tier_review.reviewed_at_utc, plantimestamp()) <= 0 &&
        timecmp(plantimestamp(), timeadd(var.free_tier_review.reviewed_at_utc, "30m")) <= 0
      )
      error_message = "Refresh the account-wide usage and pricing review within 30 minutes before planning."
    }
  }
}

resource "oci_core_instance" "cloud" {
  compartment_id       = var.compartment_ocid
  availability_domain  = var.availability_domain
  display_name         = "text-monkey-cloud-experiment"
  shape                = local.vm_shape
  freeform_tags        = local.tags
  preserve_boot_volume = true

  shape_config {
    ocpus         = local.vm_ocpus
    memory_in_gbs = local.vm_ram_gb
  }

  source_details {
    source_type             = "image"
    source_id               = var.ubuntu_image_ocid
    boot_volume_size_in_gbs = local.vm_boot_gb
    boot_volume_vpus_per_gb = 10
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.cloud.id
    assign_public_ip = true
    hostname_label   = "texty"
  }

  metadata = {
    ssh_authorized_keys = trimspace(var.ssh_public_key)
    user_data           = base64encode(file("${path.module}/cloud-init.yaml"))
  }

  lifecycle {
    # Protect the private database/browser profile from casual replacement.
    prevent_destroy = true
  }
}
