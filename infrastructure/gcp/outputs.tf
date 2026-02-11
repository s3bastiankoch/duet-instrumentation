# Outputs for GCP Duet Benchmark Infrastructure

output "vm_public_ip" {
  description = "Public IP address of the duet benchmark VM"
  value       = google_compute_address.benchmark_ip.address
}

output "loadgen_vm_public_ip" {
  description = "Public IP address of the load generator VM"
  value       = google_compute_address.loadgen_ip.address
}

output "vm_private_ip" {
  description = "Private IP address of the duet benchmark VM"
  value       = google_compute_instance.benchmark_vm.network_interface[0].network_ip
}

output "loadgen_vm_private_ip" {
  description = "Private IP address of the load generator VM"
  value       = google_compute_instance.loadgen_vm.network_interface[0].network_ip
}

output "vm_id" {
  description = "ID of the duet benchmark VM"
  value       = google_compute_instance.benchmark_vm.instance_id
}

output "vm_name" {
  description = "Name of the duet benchmark VM"
  value       = google_compute_instance.benchmark_vm.name
}

output "ssh_user" {
  description = "SSH username for connecting to the VM"
  value       = local.ssh_user
}

output "ssh_connection_command" {
  description = "SSH command to connect to the benchmark VM"
  value       = "ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ${local.ssh_user}@${google_compute_address.benchmark_ip.address}"
}

output "ssh_connection_command_loadgen" {
  description = "SSH command to connect to the load generator VM"
  value       = "ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ${local.ssh_user}@${google_compute_address.loadgen_ip.address}"
}

output "network" {
  description = "Network and subnet info"
  value = {
    network_name = google_compute_network.duet_network.name
    subnet_name  = google_compute_subnetwork.duet_subnet.name
    subnet_cidr  = var.network_cidr
    region       = var.region
    zone         = var.zone
  }
}

output "system_specs" {
  description = "System specifications of the created VMs"
  value = {
    benchmark_machine_type = var.vm_machine_type
    loadgen_machine_type   = var.loadgen_vm_machine_type
    boot_disk_size_gb      = var.vm_disk_size_gb
    image_family           = var.image_family
    zone                   = var.zone
    region                 = var.region
  }
}

output "next_steps" {
  description = "Next steps after infrastructure creation"
  value = [
    "1. Wait 5-10 minutes for VM initialization to complete",
    "2. (Optional) Poll readiness endpoint: curl http://${google_compute_address.benchmark_ip.address}/ready",
    "3. Connect to benchmark VM: ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ${local.ssh_user}@${google_compute_address.benchmark_ip.address}",
    "4. Connect to loadgen VM: ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ${local.ssh_user}@${google_compute_address.loadgen_ip.address}",
    "5. Verify benchmark VM: docker --version",
    "6. Verify loadgen VM: k6 version"
  ]
}

