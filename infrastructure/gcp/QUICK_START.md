# Quick Start Guide (GCP)

## Prerequisites
- GCP project with billing enabled
- Compute Engine API enabled
- Terraform installed
- Auth configured (recommended):

```bash
gcloud auth application-default login
gcloud config set project <your-gcp-project-id>
```

## Quick Commands

### Deploy

```bash
cd infrastructure/gcp
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
terraform init
terraform apply
```

### Get VM Info

```bash
terraform output vm_public_ip
terraform output loadgen_vm_public_ip
terraform output ssh_connection_command
terraform output ssh_connection_command_loadgen
terraform output system_specs
```

### Check readiness

```bash
curl "http://$(terraform output -raw vm_public_ip)/ready"
curl "http://$(terraform output -raw loadgen_vm_public_ip)/ready"
```

### Troubleshooting

```bash
# On VM - check setup status
sudo cloud-init status
sudo tail -f /var/log/cloud-init-output.log

# Check services
docker --version
systemctl status docker
k6 version
```

