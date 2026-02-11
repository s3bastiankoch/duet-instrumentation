"""Prepare Docker Compose configuration for duet benchmarking.

This module transforms existing docker-compose.yml files to enable
side-by-side benchmarking of base and head versions with:
- Resource isolation (CPU limits/reservations)
- Unique container names
- Separate ports for each version
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class DuetPrepareError(Exception):
    """Error during duet preparation."""


@dataclass
class DuetConfig:
    """Configuration for duet benchmark preparation."""
    
    # Resource limits
    cpu_limit: str = "2"
    cpu_reservation: str = "1"
    memory_limit: str = "4G"
    
    # Port mapping: base version starts at this port, head at base_port + 1
    base_port: int = 3001
    head_port: int = 3002
    
    # Container name prefix
    prefix: str = "duet"
    
    # Network configuration
    network_name: str = "duet-network"
    network_subnet: str = "172.28.0.0/16"
    
    # Optional: use cpuset for pinning
    base_cpuset: str | None = None
    head_cpuset: str | None = None


@dataclass
class DuetComposeResult:
    """Result of preparing Docker Compose for duet benchmark."""
    
    compose_path: Path
    env_path: Path
    base_container: str
    head_container: str
    base_port: int
    head_port: int


def _load_compose(path: Path) -> dict[str, Any]:
    """Load a docker-compose.yml file."""
    if not path.exists():
        raise DuetPrepareError(f"Docker Compose file not found: {path}")
    
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise DuetPrepareError(f"Invalid Docker Compose format: {path}")
        return data
    except yaml.YAMLError as e:
        raise DuetPrepareError(f"Failed to parse {path}: {e}")


def _find_main_service(compose: dict[str, Any]) -> str | None:
    """Find the main application service in a compose file.
    
    Heuristics:
    1. Service named 'app' or 'api' or 'server' or 'web'
    2. Service with a build context
    3. First service if only one exists
    """
    services = compose.get("services", {})
    if not services:
        return None
    
    # Priority names
    for name in ["app", "api", "server", "web", "application"]:
        if name in services:
            return name
    
    # Service with build context
    for name, svc in services.items():
        if isinstance(svc, dict) and "build" in svc:
            return name
    
    # First service
    if len(services) == 1:
        return next(iter(services.keys()))
    
    return None


def _extract_port(port_spec: str | int) -> tuple[int | None, int]:
    """Extract host and container ports from a port specification.
    
    Examples:
    - "3000" -> (None, 3000)
    - "8080:3000" -> (8080, 3000)
    - "${PORT}:3000" -> (None, 3000)
    """
    if isinstance(port_spec, int):
        return None, port_spec
    
    port_str = str(port_spec)
    
    # Handle formats like "3000/tcp" or "3000:3000/tcp"
    port_str = re.sub(r"/(tcp|udp)$", "", port_str)
    
    if ":" in port_str:
        parts = port_str.split(":")
        host_part = parts[0]
        container_part = parts[-1]
        
        # Extract numeric port from container part
        try:
            container_port = int(container_part)
        except ValueError:
            # Could be a variable
            container_port = 3000  # default
        
        # Check if host part is numeric
        try:
            host_port = int(host_part)
            return host_port, container_port
        except ValueError:
            # Variable reference like ${PORT}
            return None, container_port
    else:
        try:
            port = int(port_str)
            return None, port
        except ValueError:
            return None, 3000


def _transform_service(
    service: dict[str, Any],
    version: str,
    config: DuetConfig,
    build_context: Path | str,
) -> dict[str, Any]:
    """Transform a service definition for duet benchmarking.
    
    Args:
        service: Original service definition
        version: 'base' or 'head'
        config: Duet configuration
        build_context: Path to the version's directory
    """
    svc = copy.deepcopy(service)
    
    # Set unique container name
    original_name = svc.get("container_name", "app")
    # Remove any variable references
    if "${" in str(original_name):
        original_name = "app"
    svc["container_name"] = f"{config.prefix}-{version}"
    
    # Update build context
    if "build" in svc:
        build = svc["build"]
        if isinstance(build, str):
            svc["build"] = {
                "context": str(build_context),
                "dockerfile": "Dockerfile",
            }
        elif isinstance(build, dict):
            svc["build"] = {
                **build,
                "context": str(build_context),
            }
    else:
        svc["build"] = {
            "context": str(build_context),
            "dockerfile": "Dockerfile",
        }
    
    # Update ports
    port = config.base_port if version == "base" else config.head_port
    original_ports = svc.get("ports", [])
    if original_ports:
        # Extract container port from first port mapping
        _, container_port = _extract_port(original_ports[0])
        svc["ports"] = [f"{port}:{container_port}"]
    else:
        # Default to common ports
        svc["ports"] = [f"{port}:3000"]
    
    # Add version environment variable
    env = svc.get("environment", {})
    if isinstance(env, list):
        # Convert list to dict
        env_dict = {}
        for item in env:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                env_dict[k] = v
            else:
                env_dict[str(item)] = ""
        env = env_dict
    
    env["VERSION_TAG"] = version
    env["OTEL_AGENT_VERSION"] = version
    svc["environment"] = env
    
    # Set resource limits (Compose mode)
    svc["cpus"] = config.cpu_limit
    svc["mem_limit"] = config.memory_limit
    
    # Add cpuset if specified
    cpuset = config.base_cpuset if version == "base" else config.head_cpuset
    if cpuset:
        svc["cpuset"] = cpuset

    # Network behavior:
    # We intentionally rely on a configured compose `default` network so that *all*
    # services (including merged/cloned dependency services that may not explicitly
    # declare `networks`) end up on the same bridge network.
    svc.pop("networks", None)
    
    # Set restart policy
    svc["restart"] = "unless-stopped"
    
    return svc


def _relative_build_context(*, target_dir: Path, output_dir: Path) -> str:
    """Return a docker-compose build.context string relative to output_dir.

    Prefer a "./<path>" form when the target is inside output_dir.
    """
    rel = os.path.relpath(str(target_dir), str(output_dir))
    rel_posix = Path(rel).as_posix()
    if rel_posix == ".":
        return "."
    if rel_posix.startswith("..") or rel_posix.startswith("./"):
        return rel_posix
    return f"./{rel_posix}"


def prepare_duet_compose(
    base_dir: Path,
    head_dir: Path,
    output_dir: Path,
    config: DuetConfig | None = None,
    compose_filename: str = "docker-compose.yml",
) -> DuetComposeResult:
    """Prepare a combined Docker Compose file for duet benchmarking.
    
    Args:
        base_dir: Directory containing the base version
        head_dir: Directory containing the head version  
        output_dir: Directory to write the duet compose file
        config: Configuration options (uses defaults if None)
        compose_filename: Name of the compose file to look for
    
    Returns:
        DuetComposeResult with paths and container names
    """
    if config is None:
        config = DuetConfig()
    
    # Find compose file in either directory
    base_compose_path = base_dir / compose_filename
    head_compose_path = head_dir / compose_filename
    
    # Try to load either compose file (prefer base)
    compose_data = None
    source_path = None
    
    if base_compose_path.exists():
        compose_data = _load_compose(base_compose_path)
        source_path = base_compose_path
    elif head_compose_path.exists():
        compose_data = _load_compose(head_compose_path)
        source_path = head_compose_path
    else:
        # No compose file found - generate a minimal one
        compose_data = {
            "services": {
                "app": {
                    "build": ".",
                    "ports": ["3000:3000"],
                }
            }
        }
    
    # Find main service
    main_service_name = _find_main_service(compose_data)
    if main_service_name is None:
        raise DuetPrepareError("Could not identify main application service")
    
    main_service = compose_data["services"][main_service_name]
    
    # Create duet compose
    # IMPORTANT: configure the *default* network, named `config.network_name`.
    # This makes networking robust when other services are merged in later
    # (e.g. MongoDB), even if they don't explicitly specify `networks`.
    duet_compose: dict[str, Any] = {
        "services": {},
        "networks": {
            "default": {
                "name": config.network_name,
                "driver": "bridge",
                "ipam": {
                    "config": [{"subnet": config.network_subnet}]
                },
            },
        },
    }

    base_build_context = _relative_build_context(target_dir=base_dir, output_dir=output_dir)
    head_build_context = _relative_build_context(target_dir=head_dir, output_dir=output_dir)
    
    # Transform base service
    duet_compose["services"][f"{main_service_name}-base"] = _transform_service(
        main_service,
        "base",
        config,
        base_build_context,
    )
    
    # Transform head service
    duet_compose["services"][f"{main_service_name}-head"] = _transform_service(
        main_service,
        "head",
        config,
        head_build_context,
    )
    
    # Copy any volumes from original
    if "volumes" in compose_data:
        duet_compose["volumes"] = compose_data["volumes"]
    
    # Write duet compose file
    output_dir.mkdir(parents=True, exist_ok=True)
    compose_output = output_dir / "docker-compose.duet.yml"
    
    with open(compose_output, "w", encoding="utf-8") as f:
        # Add header comment
        f.write("# Duet Benchmark Docker Compose\n")
        f.write("# Auto-generated by otel-agent\n")
        f.write("# Runs base and head versions in isolated containers\n")
        f.write("#\n")
        f.write(f"# Base version: {base_dir}\n")
        f.write(f"# Head version: {head_dir}\n")
        f.write("#\n")
        f.write(f"# Base container: {config.prefix}-base (port {config.base_port})\n")
        f.write(f"# Head container: {config.prefix}-head (port {config.head_port})\n")
        f.write("#\n\n")
        yaml.dump(duet_compose, f, default_flow_style=False, sort_keys=False)
    
    # Generate .env file with common variables
    env_output = output_dir / ".env.duet"
    env_content = f"""# Duet Benchmark Environment Variables
# Auto-generated by otel-agent

# Container names
BASE_CONTAINER={config.prefix}-base
HEAD_CONTAINER={config.prefix}-head

# Ports
BASE_PORT={config.base_port}
HEAD_PORT={config.head_port}

# URLs for load testing
BASE_URL=http://localhost:{config.base_port}
HEAD_URL=http://localhost:{config.head_port}

# Resource limits
CPU_LIMIT={config.cpu_limit}
CPU_RESERVATION={config.cpu_reservation}
MEMORY_LIMIT={config.memory_limit}
"""
    
    env_output.write_text(env_content, encoding="utf-8")
    
    return DuetComposeResult(
        compose_path=compose_output,
        env_path=env_output,
        base_container=f"{config.prefix}-base",
        head_container=f"{config.prefix}-head",
        base_port=config.base_port,
        head_port=config.head_port,
    )

