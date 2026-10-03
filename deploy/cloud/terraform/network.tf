locals {
  # Official global IPv4 tunnel endpoints, verified 2026-10-03. Exact /32s,
  # not Cloudflare-wide ranges. Service contract: --protocol http2 and IPv4.
  # https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/
  tunnel_ipv4 = toset([
    "198.41.192.167/32", "198.41.192.67/32", "198.41.192.57/32", "198.41.192.107/32", "198.41.192.27/32",
    "198.41.192.7/32", "198.41.192.227/32", "198.41.192.47/32", "198.41.192.37/32", "198.41.192.77/32",
    "198.41.200.13/32", "198.41.200.193/32", "198.41.200.33/32", "198.41.200.233/32", "198.41.200.53/32",
    "198.41.200.63/32", "198.41.200.113/32", "198.41.200.73/32", "198.41.200.43/32", "198.41.200.23/32"
  ])
}

resource "oci_core_vcn" "cloud" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.77.0.0/24"]
  display_name   = "text-monkey-cloud-experiment"
  dns_label      = "textycloud"
  freeform_tags  = local.tags
  depends_on     = [terraform_data.review]
}

resource "oci_core_internet_gateway" "cloud" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.cloud.id
  display_name   = "text-monkey-cloud-experiment"
  enabled        = true
  freeform_tags  = local.tags
}

resource "oci_core_route_table" "cloud" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.cloud.id
  display_name   = "text-monkey-cloud-experiment"
  freeform_tags  = local.tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.cloud.id
  }
}

resource "oci_core_security_list" "cloud" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.cloud.id
  display_name   = "text-monkey-cloud-experiment"
  freeform_tags  = local.tags

  # Only the operator's single public IPv4 can initiate SSH. No 8000/8765
  # ingress, public web server, Docker API, VNC or browser-debugging port.
  ingress_security_rules {
    source      = var.admin_ipv4_cidr
    source_type = "CIDR_BLOCK"
    protocol    = "6"
    stateless   = false
    tcp_options {
      min = 22
      max = 22
    }
  }

  # Google, Gloo, package repositories and identity endpoints use HTTPS.
  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "6"
    stateless        = false
    tcp_options {
      min = 443
      max = 443
    }
  }

  # OCI's link-local VCN resolver supports UDP and TCP DNS.
  egress_security_rules {
    destination      = "169.254.169.254/32"
    destination_type = "CIDR_BLOCK"
    protocol         = "17"
    stateless        = false
    udp_options {
      min = 53
      max = 53
    }
  }
  egress_security_rules {
    destination      = "169.254.169.254/32"
    destination_type = "CIDR_BLOCK"
    protocol         = "6"
    stateless        = false
    tcp_options {
      min = 53
      max = 53
    }
  }
  egress_security_rules {
    destination      = "169.254.169.254/32"
    destination_type = "CIDR_BLOCK"
    protocol         = "17"
    stateless        = false
    udp_options {
      min = 123
      max = 123
    }
  }

  dynamic "egress_security_rules" {
    for_each = local.tunnel_ipv4
    content {
      destination      = egress_security_rules.value
      destination_type = "CIDR_BLOCK"
      protocol         = "6"
      stateless        = false
      tcp_options {
        min = 7844
        max = 7844
      }
    }
  }
}

resource "oci_core_subnet" "cloud" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.cloud.id
  cidr_block                 = "10.77.0.0/24"
  display_name               = "text-monkey-cloud-experiment"
  dns_label                  = "app"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.cloud.id
  dhcp_options_id            = oci_core_vcn.cloud.default_dhcp_options_id
  # Do not attach the VCN's permissive default security list or extra NSGs.
  security_list_ids = [oci_core_security_list.cloud.id]
  freeform_tags     = local.tags
}
