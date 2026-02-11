# STACKIT Infrastructure for Duet Benchmark

terraform {
  required_providers {
    stackit = {
      source  = "stackitcloud/stackit"
      version = "0.61.0"
    }
  }
}


# Configure the STACKIT Provider
provider "stackit" {
  default_region = var.region

  service_account_key_path = var.stackit_service_account_key_path
}

# Create SSH key pair for server access
resource "stackit_key_pair" "duet_benchmark_keypair" {
  name       = "${var.project_name}-${var.owner_name}-keypair"
  public_key = chomp(file(var.ssh_public_key_path))
}

# Create network for the VMs
resource "stackit_network" "duet_benchmark_network" {
  project_id       = var.project_id
  name             = "${var.project_name}-${var.owner_name}-network"
  ipv4_nameservers = ["8.8.8.8", "8.8.4.4"]
  ipv4_prefix      = "192.168.1.0/24"
  labels = merge({
    purpose = "duet-benchmark"
    project = var.project_name
  }, var.additional_labels)
  routed = true
}

# Create security group for the VM
resource "stackit_security_group" "duet_benchmark_sg" {
  project_id = var.project_id
  name       = "${var.project_name}-${var.owner_name}-security-group"
  stateful   = true
  labels = merge({
    purpose = "duet-benchmark"
    project = var.project_name
  }, var.additional_labels)
}

# Allow SSH access (port 22)
resource "stackit_security_group_rule" "ssh_ingress" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.duet_benchmark_sg.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  protocol = {
    name = "tcp"
  }
  port_range = {
    min = 22
    max = 22
  }
  ip_range = var.ssh_allowed_cidr
}

# Allow HTTP access (port 80) for potential web interfaces
resource "stackit_security_group_rule" "http_ingress" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.duet_benchmark_sg.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  protocol = {
    name = "tcp"
  }
  port_range = {
    min = 80
    max = 80
  }
  ip_range = var.http_allowed_cidr
}

# Allow HTTPS access (port 443)
resource "stackit_security_group_rule" "https_ingress" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.duet_benchmark_sg.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  protocol = {
    name = "tcp"
  }
  port_range = {
    min = 443
    max = 443
  }
  ip_range = var.http_allowed_cidr
}

# Allow benchmark app ports (base/head) so the load generator can reach the SUT
resource "stackit_security_group_rule" "sut_ports_ingress" {
  project_id        = var.project_id
  security_group_id = stackit_security_group.duet_benchmark_sg.security_group_id
  direction         = "ingress"
  ether_type        = "IPv4"
  protocol = {
    name = "tcp"
  }
  port_range = {
    min = 3001
    max = 3002
  }
  ip_range = var.sut_ports_allowed_cidr
}

# Create network interface
resource "stackit_network_interface" "duet_benchmark_nic" {
  project_id         = var.project_id
  network_id         = stackit_network.duet_benchmark_network.network_id
  security_group_ids = [stackit_security_group.duet_benchmark_sg.security_group_id]
  name               = "${var.project_name}-${var.owner_name}-nic"
  labels = merge({
    purpose = "duet-benchmark"
    project = var.project_name
  }, var.additional_labels)
}

# Network interface for load generator VM
resource "stackit_network_interface" "duet_loadgen_nic" {
  project_id         = var.project_id
  network_id         = stackit_network.duet_benchmark_network.network_id
  security_group_ids = [stackit_security_group.duet_benchmark_sg.security_group_id]
  name               = "${var.project_name}-${var.owner_name}-loadgen-nic"
  labels = merge({
    purpose = "duet-loadgen"
    project = var.project_name
    role    = "loadgen"
  }, var.additional_labels)
}

# Create public IP for external access
resource "stackit_public_ip" "duet_benchmark_public_ip" {
  project_id           = var.project_id
  network_interface_id = stackit_network_interface.duet_benchmark_nic.network_interface_id
  labels = merge({
    purpose = "duet-benchmark"
    project = var.project_name
  }, var.additional_labels)
}

# Public IP for load generator VM
resource "stackit_public_ip" "duet_loadgen_public_ip" {
  project_id           = var.project_id
  network_interface_id = stackit_network_interface.duet_loadgen_nic.network_interface_id
  labels = merge({
    purpose = "duet-loadgen"
    project = var.project_name
    role    = "loadgen"
  }, var.additional_labels)
}

# Create named boot volumes (instead of letting the server create anonymous ones)
resource "stackit_volume" "duet_benchmark_boot_volume" {
  project_id        = var.project_id
  name              = "${var.project_name}-${var.owner_name}-boot"
  availability_zone = var.availability_zone
  description       = "Boot volume for duet benchmark VM"
  size              = var.vm_disk_size_gb
  performance_class = var.boot_volume_performance_class

  source = {
    id   = var.vm_image_id
    type = "image"
  }

  labels = merge({
    purpose = "duet-benchmark"
    project = var.project_name
    role    = "benchmark-runner"
  }, var.additional_labels)
}

resource "stackit_volume" "duet_loadgen_boot_volume" {
  project_id        = var.project_id
  name              = "${var.project_name}-${var.owner_name}-loadgen-boot"
  availability_zone = var.availability_zone
  description       = "Boot volume for duet load generator VM"
  size              = var.vm_disk_size_gb
  performance_class = var.boot_volume_performance_class

  source = {
    id   = var.vm_image_id
    type = "image"
  }

  labels = merge({
    purpose = "duet-loadgen"
    project = var.project_name
    role    = "loadgen"
  }, var.additional_labels)
}

# Create the main VM for running duet benchmarks
resource "stackit_server" "duet_benchmark_vm" {
  project_id   = var.project_id
  name         = "${var.project_name}-${var.owner_name}-vm"
  machine_type = var.vm_machine_type

  boot_volume = {
    source_type = "volume"
    source_id   = stackit_volume.duet_benchmark_boot_volume.volume_id
  }

  availability_zone = var.availability_zone
  keypair_name      = stackit_key_pair.duet_benchmark_keypair.name

  network_interfaces = [
    stackit_network_interface.duet_benchmark_nic.network_interface_id
  ]

  # Cloud-init configuration for setting up the VM
  user_data = file("${path.module}/../common/cloud-init.yaml")

  labels = merge({
    purpose = "duet-benchmark"
    project = var.project_name
    role    = "benchmark-runner"
  }, var.additional_labels)
}

# Second VM dedicated to load generation
resource "stackit_server" "duet_loadgen_vm" {
  project_id   = var.project_id
  name         = "${var.project_name}-${var.owner_name}-loadgen-vm"
  machine_type = var.loadgen_vm_machine_type

  boot_volume = {
    source_type = "volume"
    source_id   = stackit_volume.duet_loadgen_boot_volume.volume_id
  }

  availability_zone = var.availability_zone
  keypair_name      = stackit_key_pair.duet_benchmark_keypair.name

  network_interfaces = [
    stackit_network_interface.duet_loadgen_nic.network_interface_id
  ]

  # Cloud-init configuration for the load generator (k6)
  user_data = file("${path.module}/../common/cloud-init-loadgen.yaml")

  labels = merge({
    purpose = "duet-loadgen"
    project = var.project_name
    role    = "loadgen"
  }, var.additional_labels)
}

