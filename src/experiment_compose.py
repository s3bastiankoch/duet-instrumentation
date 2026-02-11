"""Generate an experiment-ready docker compose file.

Goal: merge the duet compose (base/head) with optional "ext" dependencies
(e.g. MongoDB) into a single compose file that can be used by the benchmark
runner scripts.

Key design choice: use `${BASE_DIR}` and `${HEAD_DIR}` in build contexts so the
same compose file works for:
- local runs (BASE_DIR=.otel-agent/base, HEAD_DIR=.otel-agent/head)
- remote runs after syncing artifacts (BASE_DIR=/tmp/.../base, HEAD_DIR=/tmp/.../head)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ExperimentComposeError(Exception):
    """Error during experiment compose generation."""


@dataclass(frozen=True)
class ExperimentComposeResult:
    compose_path: Path


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ExperimentComposeError(f"YAML file not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ExperimentComposeError(f"Invalid YAML format: {path}")
        return data
    except yaml.YAMLError as e:
        raise ExperimentComposeError(f"Failed to parse {path}: {e}")


def _ensure_env_dict(env: Any) -> dict[str, str]:
    if env is None:
        return {}
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    if isinstance(env, list):
        out: dict[str, str] = {}
        for item in env:
            s = str(item)
            if "=" in s:
                k, v = s.split("=", 1)
                out[k] = v
            else:
                out[s] = ""
        return out
    return {}


def _patch_app_service(
    svc: dict[str, Any],
    *,
    build_context_var: str,
    mongo_url: str,
    mongo_service: str,
) -> dict[str, Any]:
    # Build context: set to ${BASE_DIR}/${HEAD_DIR}
    build = svc.get("build")
    if isinstance(build, str):
        svc["build"] = {"context": build_context_var, "dockerfile": "Dockerfile"}
    elif isinstance(build, dict):
        svc["build"] = {**build, "context": build_context_var}
    else:
        svc["build"] = {"context": build_context_var, "dockerfile": "Dockerfile"}

    # Env cleanup + in-compose wiring
    env = _ensure_env_dict(svc.get("environment"))
    env["MONGO_URL"] = mongo_url
    # Avoid forcing users to provide JWT_SECRET at compose-time
    if env.get("JWT_SECRET", "").strip() in ("${JWT_SECRET}", "${JWT_SECRET:-}", ""):
        env["JWT_SECRET"] = "unused"
    # Remove variables that were used by older infra templates but aren't needed here
    env.pop("PERMIFY_URL", None)
    svc["environment"] = env

    # Ensure mongodb dependency if present in the composed file
    if mongo_service:
        depends = svc.get("depends_on") or {}
        if isinstance(depends, list):
            depends = {name: {"condition": "service_started"} for name in depends}
        if isinstance(depends, dict):
            depends[mongo_service] = {"condition": "service_healthy"}
        svc["depends_on"] = depends

    return svc


def prepare_experiment_compose(
    *,
    duet_compose_path: Path,
    ext_compose_path: Path | None,
    output_path: Path,
    mongo_service_name: str = "mongodb",
    mongo_url: str = "mongodb://mongodb:27017/test",
    mongo_cpuset: str | None = None,
    separate_mongo: bool = False,
) -> ExperimentComposeResult:
    """Merge duet compose + ext compose into a single experiment compose."""

    duet = _load_yaml(duet_compose_path)
    services = duet.get("services")
    if not isinstance(services, dict) or not services:
        raise ExperimentComposeError(f"No services found in duet compose: {duet_compose_path}")

    ext_services: dict[str, Any] = {}
    ext_volumes: dict[str, Any] = {}
    if ext_compose_path is not None and ext_compose_path.exists():
        ext = _load_yaml(ext_compose_path)
        ext_services = ext.get("services") if isinstance(ext.get("services"), dict) else {}
        ext_volumes = ext.get("volumes") if isinstance(ext.get("volumes"), dict) else {}

    # Merge services: ext first (so duet can override if needed, but we avoid collisions)
    merged_services: dict[str, Any] = {}
    for name, svc in ext_services.items():
        if name in services:
            raise ExperimentComposeError(
                f"Service name collision while merging ext compose: '{name}' exists in duet compose"
            )
        merged_services[name] = svc

    def _clone_mongo_service(
        base_service: dict[str, Any],
        *,
        suffix: str,
        host_port_offset: int,
    ) -> dict[str, Any]:
        svc = dict(base_service)
        svc["container_name"] = f"{mongo_service_name}-{suffix}"
        container_port = 27017

        # Avoid host port collisions by offsetting numeric host ports.
        ports = svc.get("ports")
        if isinstance(ports, list):
            new_ports: list[str] = []
            for port in ports:
                if isinstance(port, int):
                    new_ports.append(str(port + host_port_offset))
                    continue
                port_str = str(port)
                if ":" in port_str:
                    host_part, container_part = port_str.split(":", 1)
                    try:
                        host_port = int(host_part)
                        new_ports.append(f"{host_port + host_port_offset}:{container_port}")
                        continue
                    except ValueError:
                        # Non-numeric host port; drop host mapping to avoid conflict.
                        continue
                new_ports.append(f"{port_str}:{container_port}")
            svc["ports"] = new_ports
        else:
            svc["ports"] = [f"{container_port + host_port_offset}:{container_port}"]

        # Update healthcheck for non-default port.
        healthcheck = svc.get("healthcheck")
        if isinstance(healthcheck, dict):
            test = healthcheck.get("test")
            if isinstance(test, str):
                healthcheck["test"] = test.replace("27017", str(container_port))
            elif isinstance(test, list):
                healthcheck["test"] = [t.replace("27017", str(container_port)) for t in test]
            svc["healthcheck"] = healthcheck

        # Ensure separate data volumes per service.
        volumes = svc.get("volumes")
        if isinstance(volumes, list):
            new_vols: list[str] = []
            for vol in volumes:
                vol_str = str(vol)
                if ":" in vol_str:
                    source, target = vol_str.split(":", 1)
                    new_vols.append(f"{source}-{suffix}:{target}")
                else:
                    new_vols.append(f"{vol_str}-{suffix}")
            svc["volumes"] = new_vols
        return svc

    if separate_mongo and mongo_service_name in merged_services:
        base_mongo = merged_services.pop(mongo_service_name)
        if not isinstance(base_mongo, dict):
            raise ExperimentComposeError(f"Mongo service '{mongo_service_name}' must be a mapping")

        merged_services[f"{mongo_service_name}-base"] = _clone_mongo_service(
            base_mongo,
            suffix="base",
            host_port_offset=0,
        )
        merged_services[f"{mongo_service_name}-head"] = _clone_mongo_service(
            base_mongo,
            suffix="head",
            host_port_offset=1,
        )

        # Add cloned volumes if the source volumes were defined.
        if isinstance(ext_volumes, dict):
            new_volumes: dict[str, Any] = {}
            for name, cfg in ext_volumes.items():
                new_volumes[f"{name}-base"] = cfg
                new_volumes[f"{name}-head"] = cfg
            ext_volumes = new_volumes

    # Optionally pin MongoDB to specific CPUs.
    if mongo_cpuset:
        mongo_names = (
            [f"{mongo_service_name}-base", f"{mongo_service_name}-head"]
            if separate_mongo
            else [mongo_service_name]
        )
        for name in mongo_names:
            mongo_svc = merged_services.get(name)
            if isinstance(mongo_svc, dict) and "cpuset" not in mongo_svc:
                mongo_svc["cpuset"] = mongo_cpuset

    # Patch duet services (base/head) for portable build contexts and in-compose deps.
    for name, svc in services.items():
        if not isinstance(svc, dict):
            merged_services[name] = svc
            continue

        # Identify base/head services by container_name or OTEL_AGENT_VERSION
        env = _ensure_env_dict(svc.get("environment"))
        version = env.get("OTEL_AGENT_VERSION") or env.get("VERSION_TAG") or ""
        container_name = str(svc.get("container_name", ""))

        if version == "base" or container_name.endswith("-base") or container_name == "duet-base":
            merged_services[name] = _patch_app_service(
                svc,
                build_context_var="${BASE_DIR}",
                mongo_url=mongo_url if not separate_mongo else f"mongodb://{mongo_service_name}-base:27017/test",
                mongo_service=(
                    f"{mongo_service_name}-base"
                    if separate_mongo and f"{mongo_service_name}-base" in merged_services
                    else (mongo_service_name if mongo_service_name in ext_services else "")
                ),
            )
        elif version == "head" or container_name.endswith("-head") or container_name == "duet-head":
            merged_services[name] = _patch_app_service(
                svc,
                build_context_var="${HEAD_DIR}",
                mongo_url=mongo_url if not separate_mongo else f"mongodb://{mongo_service_name}-head:27017/test",
                mongo_service=(
                    f"{mongo_service_name}-head"
                    if separate_mongo and f"{mongo_service_name}-head" in merged_services
                    else (mongo_service_name if mongo_service_name in ext_services else "")
                ),
            )
        else:
            merged_services[name] = svc

    duet["services"] = merged_services

    # Merge volumes
    merged_volumes: dict[str, Any] = {}
    if isinstance(duet.get("volumes"), dict):
        merged_volumes.update(duet["volumes"])
    merged_volumes.update(ext_volumes)
    if merged_volumes:
        duet["volumes"] = merged_volumes

    # Ensure ext services join the duet network if it exists
    networks = duet.get("networks")
    if isinstance(networks, dict) and networks:
        net_name = next(iter(networks.keys()))
        for svc_name in ext_services.keys():
            svc = duet["services"].get(svc_name)
            if isinstance(svc, dict):
                svc["networks"] = [net_name]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(duet, f, sort_keys=False, default_flow_style=False)

    return ExperimentComposeResult(compose_path=output_path)


