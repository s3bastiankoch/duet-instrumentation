# GCP Terraform Infrastructure for Duet Benchmarks

This Terraform configuration sets up a complete infrastructure on Google Cloud Platform (GCP) to run the duet benchmark system. It creates two virtual machines:

- **Benchmark VM**: Ubuntu 24.04 LTS with Docker installed
- **Loadgen VM**: Ubuntu 24.04 LTS with k6 installed

Both VMs expose a lightweight readiness endpoint at `http://<public-ip>/ready` once cloud-init finished.

## Prerequisites

### 1. GCP Project
- A GCP project with billing enabled
- Compute Engine API enabled

### 2. Required Tools
- Terraform installed
- gcloud CLI installed (recommended for auth)

### 3. Authentication

This setup uses the standard Google provider authentication chain. The simplest option is Application Default Credentials:

```bash
gcloud auth application-default login
gcloud config set project <your-gcp-project-id>
```

Alternatively, you can use a service account JSON key via `GOOGLE_APPLICATION_CREDENTIALS`.

### 4. SSH Key Pair

You need an SSH public key file. The Terraform config injects it into instance metadata as user `ubuntu` so the VM home paths match the benchmark scripts.

Example:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gcp_duet_key
```

## Configuration

1. Copy `terraform.tfvars.example` to `terraform.tfvars`
2. Set at least `project_id`, `region`, `zone`, and `ssh_public_key_path`

## Cloud-init

Cloud-init scripts are shared across providers and live in `infrastructure/common/`.

## Deployment

```bash
cd infrastructure/gcp
terraform init
terraform plan
terraform apply
```

## Usage

After apply, get connection info:

```bash
terraform output ssh_connection_command
terraform output ssh_connection_command_loadgen
terraform output vm_public_ip
terraform output loadgen_vm_public_ip
```

### Readiness

```bash
curl "http://$(terraform output -raw vm_public_ip)/ready"
curl "http://$(terraform output -raw loadgen_vm_public_ip)/ready"
```

### Cloud-init logs

On either VM:

```bash
sudo tail -f /var/log/cloud-init-output.log
sudo cloud-init status
```

## Cleanup

```bash
terraform destroy
```

