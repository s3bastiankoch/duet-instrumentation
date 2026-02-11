# Variables for STACKIT Duet Benchmark Infrastructure

# Required variables
variable "project_id" {
  description = "STACKIT project ID where resources will be created"
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.project_id))
    error_message = "Project ID must be a valid UUID."
  }
}

variable "ssh_public_key_path" {
  description = "Path to SSH public key file for VM access"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

# Infrastructure configuration
variable "region" {
  description = "STACKIT region for resources"
  type        = string
  default     = "eu01"
  validation {
    condition     = contains(["eu01", "eu02", "westeurope", "northeurope"], var.region)
    error_message = "Region must be one of: eu01, eu02, westeurope, northeurope."
  }
}

variable "availability_zone" {
  description = "Availability zone for VM placement"
  type        = string
  default     = "eu01-1"
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

# VM Configuration
variable "vm_machine_type" {
  description = "STACKIT machine type for the benchmark VM"
  type        = string
  default     = "s1.4"
  validation {
    condition = contains([
      "g1.1", "g1.2", "g1.3", "g1.4", # General purpose
      "s1.1", "s1.2", "s1.3", "s1.4", # Compute optimized
    ], var.vm_machine_type)
    error_message = "Machine type must be a valid STACKIT machine type."
  }
}

variable "loadgen_vm_machine_type" {
  description = "STACKIT machine type for the load generator VM (if created)"
  type        = string
  default     = "g1.2" # smaller is often fine for loadgen; adjust as needed
  validation {
    condition = contains([
      "g1.1", "g1.2", "g1.3", "g1.4", # General purpose
    ], var.loadgen_vm_machine_type)
    error_message = "Machine type must be a valid STACKIT machine type."
  }
}

variable "sut_ports_allowed_cidr" {
  description = "CIDR blocks allowed to access the SUT benchmark ports (base/head host ports, e.g. 3001-3002)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "vm_disk_size_gb" {
  description = "Boot disk size for the VM in GB"
  type        = number
  default     = 100
  validation {
    condition     = var.vm_disk_size_gb >= 20 && var.vm_disk_size_gb <= 2000
    error_message = "VM disk size must be between 20 and 2000 GB."
  }
}

variable "boot_volume_performance_class" {
  description = "Performance class for the VM boot volumes. Set to null to let STACKIT choose the default for your region/project."
  type        = string
  default     = null
}

variable "vm_image_id" {
  description = "STACKIT image ID for the VM (Ubuntu 24.04 LTS by default)"
  type        = string
  default     = "59838a89-51b1-4892-b57f-b3caf598ee2f" # Ubuntu 24.04 LTS
}

# Network Security Configuration
variable "ssh_allowed_cidr" {
  description = "CIDR blocks allowed for SSH access"
  type        = string
  default     = "0.0.0.0/0" # Consider restricting this in production
}

variable "http_allowed_cidr" {
  description = "CIDR blocks allowed for HTTP/HTTPS access"
  type        = string
  default     = "0.0.0.0/0"
}

# Owner/User Configuration
variable "owner_name" {
  description = "Name of the owner/user for resource naming"
  type        = string
  default     = "user"
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.owner_name))
    error_message = "Owner name must contain only lowercase letters, numbers, and hyphens."
  }
}

# Tags and Labels
variable "additional_labels" {
  description = "Additional labels to apply to all resources"
  type        = map(string)
  default     = {}
}

# Monitoring and Logging (optional)
variable "enable_monitoring" {
  description = "Whether to enable basic monitoring for the VM"
  type        = bool
  default     = false
}

variable "stackit_service_account_key_path" {
  description = "Path to the Stackit service account key"
  type        = string
}