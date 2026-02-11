# Outputs for STACKIT Duet Benchmark Infrastructure

# VM Information
output "vm_public_ip" {
  description = "Public IP address of the duet benchmark VM"
  value       = stackit_public_ip.duet_benchmark_public_ip.ip
}

output "loadgen_vm_public_ip" {
  description = "Public IP address of the load generator VM"
  value       = stackit_public_ip.duet_loadgen_public_ip.ip
}

output "vm_private_ip" {
  description = "Private IP address of the duet benchmark VM"
  value       = stackit_network_interface.duet_benchmark_nic.ipv4
}

output "vm_id" {
  description = "ID of the duet benchmark VM"
  value       = stackit_server.duet_benchmark_vm.server_id
}

output "vm_name" {
  description = "Name of the duet benchmark VM"
  value       = stackit_server.duet_benchmark_vm.name
}

# SSH Connection Information
output "ssh_connection_command" {
  description = "SSH command to connect to the VM"
  value       = "ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ubuntu@${stackit_public_ip.duet_benchmark_public_ip.ip}"
}

output "ssh_connection_command_loadgen" {
  description = "SSH command to connect to the load generator VM"
  value       = "ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ubuntu@${stackit_public_ip.duet_loadgen_public_ip.ip}"
}

output "ssh_user" {
  description = "SSH username for connecting to the VM"
  value       = "ubuntu"
}

# Network Information
output "network_id" {
  description = "ID of the created network"
  value       = stackit_network.duet_benchmark_network.network_id
}

output "security_group_id" {
  description = "ID of the security group"
  value       = stackit_security_group.duet_benchmark_sg.security_group_id
}

output "network_interface_id" {
  description = "ID of the network interface"
  value       = stackit_network_interface.duet_benchmark_nic.network_interface_id
}

# Duet Benchmark Usage Information
output "duet_benchmark_setup_status" {
  description = "Status information about the duet benchmark setup"
  value = {
    vm_ready      = "VM should be ready in 5-10 minutes after creation"
    docker_status = "Check with: docker --version"
  }
}

output "loadgen_setup_status" {
  description = "Status information about the load generator setup"
  value = {
    vm_ready  = "VM should be ready in 5-10 minutes after creation"
    k6_status = "Check with: k6 version"
  }
}

# Resource URLs (for STACKIT portal access)
output "stackit_resource_urls" {
  description = "URLs to view resources in STACKIT portal"
  value = {
    vm_url      = "https://portal.stackit.cloud/projects/${var.project_id}/compute/servers/${stackit_server.duet_benchmark_vm.server_id}"
    network_url = "https://portal.stackit.cloud/projects/${var.project_id}/networking/networks/${stackit_network.duet_benchmark_network.network_id}"
    project_url = "https://portal.stackit.cloud/projects/${var.project_id}"
  }
}

# System Information
output "system_specs" {
  description = "System specifications of the created VM"
  value = {
    machine_type      = var.vm_machine_type
    boot_disk_size_gb = var.vm_disk_size_gb
    availability_zone = var.availability_zone
    region            = var.region
  }
}

# Next Steps
output "next_steps" {
  description = "Next steps after infrastructure creation"
  value = [
    "1. Wait 5-10 minutes for VM initialization to complete",
    "2. Connect to benchmark VM: ${replace(var.ssh_public_key_path, ".pub", "") != "" ? "ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ubuntu@${stackit_public_ip.duet_benchmark_public_ip.ip}" : "ssh ubuntu@${stackit_public_ip.duet_benchmark_public_ip.ip}"}",
    "3. Connect to loadgen VM: ${replace(var.ssh_public_key_path, ".pub", "") != "" ? "ssh -i ${replace(var.ssh_public_key_path, ".pub", "")} ubuntu@${stackit_public_ip.duet_loadgen_public_ip.ip}" : "ssh ubuntu@${stackit_public_ip.duet_loadgen_public_ip.ip}"}",
    "4. Verify benchmark VM: docker --version",
    "5. Verify loadgen VM: k6 version"
  ]
}
