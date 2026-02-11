# Variables for GCP Duet Benchmark Infrastructure

variable "project_id" {
  description = "GCP project ID where resources will be created"
  type        = string
}

variable "region" {
  description = "GCP region for resources"
  type        = string
  default     = "europe-west3"
}

variable "zone" {
  description = "GCP zone for VM placement"
  type        = string
  default     = "europe-west3-a"
}

variable "project_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "duet-benchmark"
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.project_name))
    error_message = "Project name must contain only lowercase letters, numbers, and hyphens."
  }
}

variable "owner_name" {
  description = "Name of the owner/user for resource naming"
  type        = string
  default     = "user"
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.owner_name))
    error_message = "Owner name must contain only lowercase letters, numbers, and hyphens."
  }
}

variable "network_cidr" {
  description = "CIDR range for the VPC subnet"
  type        = string
  default     = "10.10.0.0/24"
}

variable "ssh_public_key_path" {
  description = "Path to SSH public key file for VM access"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "vm_machine_type" {
  description = "GCP machine type for the benchmark VM"
  type        = string
  default     = "e2-standard-4" # 4 vCPUs, 16 GB RAM
}

variable "loadgen_vm_machine_type" {
  description = "GCP machine type for the load generator VM"
  type        = string
  default     = "e2-standard-2"
}

variable "vm_disk_size_gb" {
  description = "Boot disk size for the VMs in GB"
  type        = number
  default     = 100
  validation {
    condition     = var.vm_disk_size_gb >= 20 && var.vm_disk_size_gb <= 2000
    error_message = "VM disk size must be between 20 and 2000 GB."
  }
}

variable "image_project" {
  description = "Image project for Ubuntu image"
  type        = string
  default     = "ubuntu-os-cloud"
}

variable "image_family" {
  description = "Image family for Ubuntu image"
  type        = string
  default     = "ubuntu-2404-lts-amd64"
}

variable "ssh_allowed_cidr" {
  description = "CIDR blocks allowed for SSH access"
  type        = string
  default     = "0.0.0.0/0"
}

variable "http_allowed_cidr" {
  description = "CIDR blocks allowed for HTTP/HTTPS access"
  type        = string
  default     = "0.0.0.0/0"
}

variable "sut_ports_allowed_cidr" {
  description = "CIDR blocks allowed to access the SUT benchmark ports (base/head host ports, e.g. 3001-3002)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "additional_labels" {
  description = "Additional labels to apply to all resources"
  type        = map(string)
  default     = {}
}

