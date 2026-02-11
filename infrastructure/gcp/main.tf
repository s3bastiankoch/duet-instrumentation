terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "6.21.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  name_prefix = "${var.project_name}-${var.owner_name}"
  base_labels = merge({
    project = var.project_name
  }, var.additional_labels)

  benchmark_labels = merge(local.base_labels, {
    purpose = "duet-benchmark"
    role    = "benchmark-runner"
  })

  loadgen_labels = merge(local.base_labels, {
    purpose = "duet-loadgen"
    role    = "loadgen"
  })

  ssh_user = "ubuntu"

  ssh_public_key_content = chomp(file(var.ssh_public_key_path))
  ssh_keys_metadata      = "${local.ssh_user}:${local.ssh_public_key_content}"

  # C4 machine types require Hyperdisk; Persistent Disk isn't supported.
  benchmark_disk_type = can(regex("^c4-", var.vm_machine_type)) ? "hyperdisk-balanced" : "pd-balanced"
  # Keep loadgen on non-SSD storage unless it's C4.
  loadgen_disk_type = can(regex("^c4-", var.loadgen_vm_machine_type)) ? "hyperdisk-balanced" : "pd-standard"
}

resource "google_compute_network" "duet_network" {
  name                    = "${local.name_prefix}-net"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "duet_subnet" {
  name          = "${local.name_prefix}-subnet"
  ip_cidr_range = var.network_cidr
  region        = var.region
  network       = google_compute_network.duet_network.id
}

resource "google_compute_firewall" "allow_internal" {
  name    = "${local.name_prefix}-allow-internal"
  network = google_compute_network.duet_network.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = [var.network_cidr]

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "icmp"
  }

  target_tags = ["duet-benchmark"]
}

resource "google_compute_firewall" "allow_ssh" {
  name    = "${local.name_prefix}-allow-ssh"
  network = google_compute_network.duet_network.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = [var.ssh_allowed_cidr]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  target_tags = ["duet-benchmark"]
}

resource "google_compute_firewall" "allow_http_https" {
  name    = "${local.name_prefix}-allow-http-https"
  network = google_compute_network.duet_network.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = [var.http_allowed_cidr]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  target_tags = ["duet-benchmark"]
}

resource "google_compute_firewall" "allow_sut_ports" {
  name    = "${local.name_prefix}-allow-sut-ports"
  network = google_compute_network.duet_network.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = [var.sut_ports_allowed_cidr]

  allow {
    protocol = "tcp"
    ports    = ["3001-3002"]
  }

  target_tags = ["duet-benchmark"]
}

resource "google_compute_address" "benchmark_ip" {
  name   = "${local.name_prefix}-benchmark-ip"
  region = var.region
}

resource "google_compute_address" "loadgen_ip" {
  name   = "${local.name_prefix}-loadgen-ip"
  region = var.region
}

resource "google_compute_instance" "benchmark_vm" {
  name         = "${local.name_prefix}-vm"
  machine_type = var.vm_machine_type
  zone         = var.zone

  dynamic "advanced_machine_features" {
    for_each = can(regex("^c4-", var.vm_machine_type)) ? [1] : []
    content {
      turbo_mode = "ALL_CORE_MAX"
    }
  }

  tags = ["duet-benchmark", "benchmark-runner"]
  labels = local.benchmark_labels

  boot_disk {
    initialize_params {
      image = "${var.image_project}/${var.image_family}"
      size  = var.vm_disk_size_gb
      type  = local.benchmark_disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.duet_subnet.id
    access_config {
      nat_ip = google_compute_address.benchmark_ip.address
    }
  }

  metadata = {
    # Enable cloud-init via GCE metadata.
    "user-data" = file("${path.module}/../common/cloud-init.yaml")
    "ssh-keys"  = local.ssh_keys_metadata
  }
}

resource "google_compute_instance" "loadgen_vm" {
  name         = "${local.name_prefix}-loadgen-vm"
  machine_type = var.loadgen_vm_machine_type
  zone         = var.zone

  tags = ["duet-benchmark", "loadgen"]
  labels = local.loadgen_labels

  boot_disk {
    initialize_params {
      image = "${var.image_project}/${var.image_family}"
      size  = var.vm_disk_size_gb
      type  = local.loadgen_disk_type
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.duet_subnet.id
    access_config {
      nat_ip = google_compute_address.loadgen_ip.address
    }
  }

  metadata = {
    "user-data" = file("${path.module}/../common/cloud-init-loadgen.yaml")
    "ssh-keys"  = local.ssh_keys_metadata
  }
}

