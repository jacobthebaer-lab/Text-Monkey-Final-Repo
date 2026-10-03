output "instance_ocid" {
  value       = oci_core_instance.cloud.id
  description = "Created instance identifier, not an indication of app or Google readiness."
}

output "ssh_ipv4" {
  value       = oci_core_instance.cloud.public_ip
  description = "Ephemeral IPv4: only the supplied admin /32 can connect to SSH."
}

output "reviewed_allocation" {
  value = {
    shape                  = local.vm_shape
    instances              = 1
    allocated_ocpus        = local.vm_ocpus
    allocated_memory_gb    = local.vm_ram_gb
    allocated_boot_gb      = local.vm_boot_gb
    total_reviewed_ocpus   = var.free_tier_review.other_a1_ocpus + local.vm_ocpus
    total_reviewed_ram_gb  = var.free_tier_review.other_a1_memory_gb + local.vm_ram_gb
    total_reviewed_disk_gb = var.free_tier_review.other_boot_and_block_gb + local.vm_boot_gb
  }
  description = "Arithmetic over supplied review totals. This does not independently attest billing or inventory completeness."
}
