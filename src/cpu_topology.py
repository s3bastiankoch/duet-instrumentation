"""CPU Topology Analysis and Optimal Pinning for Duet Benchmarking.

This module analyzes CPU topology from `lscpu -e` output and computes optimal
CPU pinning configurations for duet benchmarking experiments.

Design principles (from DUET_ISOLATION_CHEATSHEET.md):
1. Avoid CPU 0: Reserve Core 0 for OS interrupts, networking, SSH
2. Hyperthreading awareness: Never share a physical CORE ID between base and head
3. L3 Locality: Pin both versions to the same SOCKET and NODE
4. Separate infrastructure: Dedicate CPUs for MongoDB/other services
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PinningStrategy(Enum):
    """Strategy for assigning CPUs to base and head."""

    # Both base and head share the same CPUs (time-multiplexed)
    # Good for: Reducing cache pollution differences, same thermal conditions
    SHARED = "shared"

    # Base and head get separate physical cores (no HT siblings)
    # Good for: Maximum isolation, but may have cache/thermal differences
    ISOLATED = "isolated"

    # Base and head get separate HT siblings of the same physical cores
    # Good for: Similar L1/L2 cache behavior while maintaining some isolation
    SIBLING = "sibling"


@dataclass
class LogicalCPU:
    """Represents a logical CPU (thread) in the system."""

    cpu_id: int  # Logical CPU number
    core_id: int  # Physical core number
    socket_id: int  # Socket/package number
    node_id: int  # NUMA node number

    def __hash__(self) -> int:
        return hash(self.cpu_id)


@dataclass
class PhysicalCore:
    """Represents a physical CPU core with its logical CPUs."""

    core_id: int
    socket_id: int
    node_id: int
    logical_cpus: list[LogicalCPU] = field(default_factory=list)

    @property
    def is_hyperthreaded(self) -> bool:
        """Check if this core has hyperthreading enabled."""
        return len(self.logical_cpus) > 1

    @property
    def cpu_ids(self) -> list[int]:
        """Get all logical CPU IDs for this core."""
        return sorted(cpu.cpu_id for cpu in self.logical_cpus)

    @property
    def primary_cpu(self) -> int:
        """Get the primary (lowest ID) logical CPU."""
        return min(self.cpu_ids)

    @property
    def sibling_cpus(self) -> list[int]:
        """Get the sibling (non-primary) logical CPUs."""
        primary = self.primary_cpu
        return [cpu_id for cpu_id in self.cpu_ids if cpu_id != primary]


@dataclass
class CPUTopology:
    """Complete CPU topology of a system."""

    logical_cpus: list[LogicalCPU] = field(default_factory=list)
    physical_cores: dict[tuple[int, int], PhysicalCore] = field(default_factory=dict)
    # Cached properties
    _sockets: list[int] = field(default_factory=list, repr=False)
    _nodes: list[int] = field(default_factory=list, repr=False)

    @property
    def num_logical_cpus(self) -> int:
        """Total number of logical CPUs."""
        return len(self.logical_cpus)

    @property
    def num_physical_cores(self) -> int:
        """Total number of physical cores."""
        return len(self.physical_cores)

    @property
    def has_hyperthreading(self) -> bool:
        """Check if hyperthreading is enabled."""
        return any(core.is_hyperthreaded for core in self.physical_cores.values())

    @property
    def sockets(self) -> list[int]:
        """Get list of socket IDs."""
        if not self._sockets:
            self._sockets = sorted(set(cpu.socket_id for cpu in self.logical_cpus))
        return self._sockets

    @property
    def numa_nodes(self) -> list[int]:
        """Get list of NUMA node IDs."""
        if not self._nodes:
            self._nodes = sorted(set(cpu.node_id for cpu in self.logical_cpus))
        return self._nodes

    def cores_in_socket(self, socket_id: int) -> list[PhysicalCore]:
        """Get all physical cores in a specific socket."""
        return [
            core
            for (sid, _), core in self.physical_cores.items()
            if sid == socket_id
        ]

    def cores_in_node(self, node_id: int) -> list[PhysicalCore]:
        """Get all physical cores in a specific NUMA node."""
        return [core for core in self.physical_cores.values() if core.node_id == node_id]

    def get_core(self, socket_id: int, core_id: int) -> PhysicalCore | None:
        """Get a specific physical core."""
        return self.physical_cores.get((socket_id, core_id))


def parse_lscpu_output(output: str) -> CPUTopology:
    """Parse output from `lscpu -e=CPU,CORE,SOCKET,NODE`.

    Args:
        output: Raw output from lscpu command (with or without header)

    Returns:
        Parsed CPUTopology object

    Example input:
        CPU CORE SOCKET NODE
          0    0      0    0
          1    1      0    0
          2    2      0    0
          3    3      0    0
          4    0      0    0
          5    1      0    0
          6    2      0    0
          7    3      0    0
    """
    topology = CPUTopology()

    # Parse each line
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("CPU"):
            continue

        # Parse space-separated values: CPU CORE SOCKET NODE
        parts = line.split()
        if len(parts) < 4:
            continue

        try:
            cpu_id = int(parts[0])
            core_id = int(parts[1])
            socket_id = int(parts[2])
            node_id = int(parts[3])
        except ValueError:
            continue

        # Create logical CPU
        logical_cpu = LogicalCPU(
            cpu_id=cpu_id,
            core_id=core_id,
            socket_id=socket_id,
            node_id=node_id,
        )
        topology.logical_cpus.append(logical_cpu)

        # Create or update physical core
        core_key = (socket_id, core_id)
        if core_key not in topology.physical_cores:
            topology.physical_cores[core_key] = PhysicalCore(
                core_id=core_id,
                socket_id=socket_id,
                node_id=node_id,
            )
        topology.physical_cores[core_key].logical_cpus.append(logical_cpu)

    return topology


def parse_topology_metadata(metadata_str: str) -> CPUTopology:
    """Parse the flattened topology string from experiment metadata.

    The metadata string is a comma-separated list of values from lscpu output,
    with each 4 values representing (CPU, CORE, SOCKET, NODE).

    Args:
        metadata_str: e.g., "0,0,0,0,1,1,0,0,2,2,0,0,..."

    Returns:
        Parsed CPUTopology object
    """
    values = [int(x) for x in metadata_str.split(",")]

    # Reconstruct as lscpu-like output
    lines = ["CPU CORE SOCKET NODE"]
    for i in range(0, len(values), 4):
        if i + 3 < len(values):
            cpu, core, socket, node = values[i : i + 4]
            lines.append(f"{cpu} {core} {socket} {node}")

    return parse_lscpu_output("\n".join(lines))


def get_local_topology() -> CPUTopology:
    """Get CPU topology of the local machine.

    Returns:
        CPUTopology of the local system

    Raises:
        RuntimeError: If lscpu command fails
    """
    try:
        result = subprocess.run(
            ["lscpu", "-e=CPU,CORE,SOCKET,NODE"],
            capture_output=True,
            text=True,
            check=True,
        )
        return parse_lscpu_output(result.stdout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get CPU topology: {e.stderr}") from e
    except FileNotFoundError:
        raise RuntimeError("lscpu command not found - are you running on Linux?")


@dataclass
class DuetCPUConfig:
    """Optimal CPU pinning configuration for duet benchmarking."""

    # CPUs for the OS/system (reserved)
    os_cpuset: str

    # CPUs for base version (application under test - baseline)
    base_cpuset: str

    # CPUs for head version (application under test - with changes)
    head_cpuset: str

    # CPUs for infrastructure (MongoDB, etc.)
    infra_cpuset: str

    # Strategy used
    strategy: PinningStrategy

    # Warnings/notes about the configuration
    warnings: list[str] = field(default_factory=list)

    # Topology info for reference
    topology_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "os_cpuset": self.os_cpuset,
            "base_cpuset": self.base_cpuset,
            "head_cpuset": self.head_cpuset,
            "infra_cpuset": self.infra_cpuset,
            "strategy": self.strategy.value,
            "warnings": self.warnings,
            "topology_summary": self.topology_summary,
        }


def _format_cpuset(cpu_ids: list[int]) -> str:
    """Format a list of CPU IDs as a cpuset string.

    Examples:
        [0, 1, 2, 3] -> "0-3"
        [0, 2, 4, 6] -> "0,2,4,6"
        [1, 2, 3, 5, 6] -> "1-3,5-6"
    """
    if not cpu_ids:
        return ""

    cpu_ids = sorted(set(cpu_ids))

    ranges = []
    start = cpu_ids[0]
    end = start

    for cpu in cpu_ids[1:]:
        if cpu == end + 1:
            end = cpu
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = end = cpu

    # Add final range
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def _parse_cpuset(cpuset_str: str) -> list[int]:
    """Parse a cpuset string into a list of CPU IDs.

    Examples:
        "0-3" -> [0, 1, 2, 3]
        "0,2,4,6" -> [0, 2, 4, 6]
        "1-3,5-6" -> [1, 2, 3, 5, 6]
    """
    cpu_ids = []
    for part in cpuset_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            cpu_ids.extend(range(int(start), int(end) + 1))
        else:
            cpu_ids.append(int(part))
    return sorted(set(cpu_ids))


def compute_optimal_pinning(
    topology: CPUTopology,
    strategy: PinningStrategy = PinningStrategy.SHARED,
    num_app_cores: int | None = None,
    num_infra_cores: int = 1,
    prefer_socket: int | None = None,
    prefer_node: int | None = None,
) -> DuetCPUConfig:
    """Compute optimal CPU pinning for duet benchmarking.

    This algorithm follows the isolation principles from DUET_ISOLATION_CHEATSHEET.md:
    1. Reserve CPU 0 (and its HT sibling) for OS interrupts
    2. Select cores from the same socket/node for L3 cache locality
    3. Apply the selected strategy (shared, isolated, or sibling)
    4. Dedicate remaining cores to infrastructure

    Args:
        topology: CPU topology of the system
        strategy: Pinning strategy to use
        num_app_cores: Number of physical cores per app version (auto-calculated if None)
        num_infra_cores: Number of physical cores for infrastructure (default: 1)
        prefer_socket: Preferred socket ID (auto-selected if None)
        prefer_node: Preferred NUMA node ID (auto-selected if None)

    Returns:
        Optimal DuetCPUConfig

    Raises:
        ValueError: If topology doesn't have enough cores
    """
    warnings: list[str] = []

    # Generate topology summary
    topology_summary = (
        f"Logical CPUs: {topology.num_logical_cpus}, "
        f"Physical Cores: {topology.num_physical_cores}, "
        f"Sockets: {len(topology.sockets)}, "
        f"NUMA Nodes: {len(topology.numa_nodes)}, "
        f"Hyperthreading: {topology.has_hyperthreading}"
    )

    # Validate minimum requirements
    min_cores_needed = 2  # At minimum: OS shared with app + 1 app core
    if topology.num_physical_cores < min_cores_needed:
        raise ValueError(
            f"Insufficient cores: need at least {min_cores_needed} physical cores, "
            f"got {topology.num_physical_cores}"
        )

    # Step 1: Identify OS/system cores (CPU 0 and its siblings)
    os_cores: list[PhysicalCore] = []
    for core in topology.physical_cores.values():
        if 0 in core.cpu_ids:
            os_cores.append(core)
            break

    if not os_cores:
        # Fallback: use the first core in socket 0
        socket_0_cores = topology.cores_in_socket(0)
        if socket_0_cores:
            os_cores.append(socket_0_cores[0])
        else:
            # Just use the first core
            os_cores.append(list(topology.physical_cores.values())[0])

    os_cpu_ids = []
    for core in os_cores:
        os_cpu_ids.extend(core.cpu_ids)

    # Step 2: Select socket/node for benchmarking
    target_socket = prefer_socket if prefer_socket is not None else topology.sockets[0]
    target_node = prefer_node

    # Get available cores (excluding OS cores) from target socket
    available_cores = [
        core
        for core in topology.cores_in_socket(target_socket)
        if core not in os_cores
    ]

    # Filter by NUMA node if specified
    if target_node is not None:
        node_cores = [c for c in available_cores if c.node_id == target_node]
        if node_cores:
            available_cores = node_cores
        else:
            warnings.append(
                f"No cores available in NUMA node {target_node}, using all from socket {target_socket}"
            )

    # If we filtered too aggressively, fall back to all non-OS cores
    if len(available_cores) < 2:
        available_cores = [
            core
            for core in topology.physical_cores.values()
            if core not in os_cores
        ]
        warnings.append(
            f"Not enough cores in socket {target_socket}, using cores from all sockets"
        )

    # Sort by core ID for deterministic selection
    available_cores.sort(key=lambda c: (c.socket_id, c.core_id))

    # Step 3: Calculate core allocation
    total_available = len(available_cores)

    if num_app_cores is None:
        # Auto-calculate: reserve infra, split rest between base+head or share
        if strategy == PinningStrategy.SHARED:
            # Both apps share same cores
            num_app_cores = max(1, (total_available - num_infra_cores))
        elif strategy == PinningStrategy.ISOLATED:
            # Each app gets its own cores
            num_app_cores = max(1, (total_available - num_infra_cores) // 2)
        elif strategy == PinningStrategy.SIBLING:
            # Each app gets HT siblings of same cores
            num_app_cores = max(1, (total_available - num_infra_cores))

    # Validate allocation
    if strategy == PinningStrategy.ISOLATED:
        cores_needed = num_app_cores * 2 + num_infra_cores
    else:
        cores_needed = num_app_cores + num_infra_cores

    if cores_needed > total_available:
        warnings.append(
            f"Requested {cores_needed} cores but only {total_available} available, "
            "reducing allocation"
        )
        if strategy == PinningStrategy.ISOLATED:
            num_app_cores = max(1, (total_available - num_infra_cores) // 2)
        else:
            num_app_cores = max(1, total_available - num_infra_cores)

    # Step 4: Assign cores based on strategy
    base_cpu_ids: list[int] = []
    head_cpu_ids: list[int] = []
    infra_cpu_ids: list[int] = []

    if strategy == PinningStrategy.SHARED:
        # Both base and head share the same cores
        app_cores = available_cores[:num_app_cores]
        infra_cores = available_cores[num_app_cores : num_app_cores + num_infra_cores]

        for core in app_cores:
            base_cpu_ids.extend(core.cpu_ids)
            head_cpu_ids.extend(core.cpu_ids)

        for core in infra_cores:
            infra_cpu_ids.extend(core.cpu_ids)

    elif strategy == PinningStrategy.ISOLATED:
        # Base and head get completely separate physical cores
        base_cores = available_cores[:num_app_cores]
        head_cores = available_cores[num_app_cores : num_app_cores * 2]
        infra_cores = available_cores[num_app_cores * 2 : num_app_cores * 2 + num_infra_cores]

        for core in base_cores:
            base_cpu_ids.extend(core.cpu_ids)

        for core in head_cores:
            head_cpu_ids.extend(core.cpu_ids)

        for core in infra_cores:
            infra_cpu_ids.extend(core.cpu_ids)

        if not topology.has_hyperthreading:
            warnings.append(
                "Isolated strategy on non-HT system may have cache behavior differences"
            )

    elif strategy == PinningStrategy.SIBLING:
        # Base gets primary CPUs, head gets sibling CPUs of same physical cores
        if not topology.has_hyperthreading:
            warnings.append(
                "Sibling strategy requires hyperthreading, falling back to shared"
            )
            # Fall back to shared
            app_cores = available_cores[:num_app_cores]
            infra_cores = available_cores[num_app_cores : num_app_cores + num_infra_cores]

            for core in app_cores:
                base_cpu_ids.extend(core.cpu_ids)
                head_cpu_ids.extend(core.cpu_ids)

            for core in infra_cores:
                infra_cpu_ids.extend(core.cpu_ids)
        else:
            # Use HT siblings
            app_cores = [c for c in available_cores if c.is_hyperthreaded][:num_app_cores]
            if len(app_cores) < num_app_cores:
                # Not enough HT cores, supplement with non-HT
                non_ht_cores = [c for c in available_cores if not c.is_hyperthreaded]
                app_cores.extend(non_ht_cores[: num_app_cores - len(app_cores)])

            used_cores = set(id(c) for c in app_cores)
            infra_cores = [
                c for c in available_cores if id(c) not in used_cores
            ][:num_infra_cores]

            for core in app_cores:
                # Primary CPU goes to base
                base_cpu_ids.append(core.primary_cpu)
                # Siblings go to head (if available)
                siblings = core.sibling_cpus
                if siblings:
                    head_cpu_ids.extend(siblings)
                else:
                    # Non-HT core, share it
                    head_cpu_ids.append(core.primary_cpu)

            for core in infra_cores:
                infra_cpu_ids.extend(core.cpu_ids)

    # Handle edge case: no infra cores available
    if not infra_cpu_ids and num_infra_cores > 0:
        # Use the last app cores for infra as fallback
        if base_cpu_ids:
            infra_cpu_ids = [base_cpu_ids[-1]]
            warnings.append("No dedicated infra cores available, sharing with app")

    # Step 5: Validate and create config
    if not base_cpu_ids:
        raise ValueError("Failed to allocate CPUs for base version")
    if not head_cpu_ids:
        raise ValueError("Failed to allocate CPUs for head version")

    # Check for accidental sharing of physical cores between base and head
    if strategy == PinningStrategy.ISOLATED:
        base_physical = set()
        head_physical = set()

        for cpu_id in base_cpu_ids:
            for lcpu in topology.logical_cpus:
                if lcpu.cpu_id == cpu_id:
                    base_physical.add((lcpu.socket_id, lcpu.core_id))

        for cpu_id in head_cpu_ids:
            for lcpu in topology.logical_cpus:
                if lcpu.cpu_id == cpu_id:
                    head_physical.add((lcpu.socket_id, lcpu.core_id))

        shared_physical = base_physical & head_physical
        if shared_physical:
            warnings.append(
                f"WARNING: Base and head share physical cores: {shared_physical}. "
                "This violates isolation requirements!"
            )

    return DuetCPUConfig(
        os_cpuset=_format_cpuset(os_cpu_ids),
        base_cpuset=_format_cpuset(base_cpu_ids),
        head_cpuset=_format_cpuset(head_cpu_ids),
        infra_cpuset=_format_cpuset(infra_cpu_ids),
        strategy=strategy,
        warnings=warnings,
        topology_summary=topology_summary,
    )


def recommend_pinning(
    topology: CPUTopology,
    workload_type: str = "io_bound",
) -> tuple[DuetCPUConfig, str]:
    """Recommend optimal pinning configuration based on topology and workload.

    This provides an opinionated recommendation based on best practices.

    Args:
        topology: CPU topology of the system
        workload_type: Type of workload - "cpu_bound", "io_bound", or "mixed"

    Returns:
        Tuple of (DuetCPUConfig, explanation string)
    """
    explanations = []

    # Analyze topology
    num_cores = topology.num_physical_cores
    has_ht = topology.has_hyperthreading
    num_sockets = len(topology.sockets)

    explanations.append(f"Detected: {num_cores} physical cores")
    if has_ht:
        explanations.append(f"Hyperthreading: enabled ({topology.num_logical_cpus} logical CPUs)")
    if num_sockets > 1:
        explanations.append(f"Multi-socket: {num_sockets} sockets")

    # Decision logic
    if num_cores <= 2:
        # Very small VM: share everything, no dedicated infra
        strategy = PinningStrategy.SHARED
        num_app_cores = num_cores  # Use all cores (OS shares with app)
        num_infra_cores = 0  # Infra shares with app
        explanations.append(
            "Very small VM detected - using SHARED strategy, infra shares with app"
        )

    elif num_cores <= 4:
        # Small VM: use shared strategy to maximize available compute
        strategy = PinningStrategy.SHARED
        num_app_cores = max(1, num_cores - 2)  # Reserve 1 for OS, 1 for infra
        num_infra_cores = 1
        explanations.append(
            "Small VM detected - using SHARED strategy for maximum compute utilization"
        )

    elif num_cores <= 8:
        # Medium VM: can afford some isolation
        if workload_type == "cpu_bound":
            # CPU-bound workloads benefit more from isolation
            strategy = PinningStrategy.ISOLATED
            num_app_cores = max(1, (num_cores - 2) // 2)
            num_infra_cores = 1
            explanations.append(
                "Medium VM with CPU-bound workload - using ISOLATED for benchmark accuracy"
            )
        else:
            # I/O bound can use sibling strategy if HT available
            if has_ht:
                strategy = PinningStrategy.SIBLING
                num_app_cores = max(2, num_cores - 2)
                num_infra_cores = 1
                explanations.append(
                    "Medium VM with I/O workload and HT - using SIBLING for cache locality"
                )
            else:
                strategy = PinningStrategy.SHARED
                num_app_cores = max(2, num_cores - 2)
                num_infra_cores = 1
                explanations.append(
                    "Medium VM without HT - using SHARED strategy"
                )

    else:
        # Large VM: full isolation is preferred
        strategy = PinningStrategy.ISOLATED
        num_app_cores = max(2, (num_cores - 3) // 2)  # More headroom for OS/infra
        num_infra_cores = 2
        explanations.append(
            "Large VM detected - using ISOLATED with dedicated infrastructure cores"
        )

    config = compute_optimal_pinning(
        topology,
        strategy=strategy,
        num_app_cores=num_app_cores,
        num_infra_cores=num_infra_cores,
    )

    # Add specific recommendations
    if config.warnings:
        explanations.append("Warnings:")
        explanations.extend(f"  - {w}" for w in config.warnings)

    explanations.append("")
    explanations.append("Recommended configuration:")
    explanations.append(f"  OS reserved:    {config.os_cpuset}")
    explanations.append(f"  Base cpuset:    {config.base_cpuset}")
    explanations.append(f"  Head cpuset:    {config.head_cpuset}")
    explanations.append(f"  Infra cpuset:   {config.infra_cpuset}")
    explanations.append(f"  Strategy:       {config.strategy.value}")

    return config, "\n".join(explanations)


def generate_config_snippet(config: DuetCPUConfig) -> str:
    """Generate a config.yml snippet for the recommended configuration.

    Args:
        config: DuetCPUConfig to generate snippet for

    Returns:
        YAML-formatted config snippet
    """
    lines = [
        "# CPU pinning configuration (auto-generated)",
        f"# Strategy: {config.strategy.value}",
        f"# Topology: {config.topology_summary}",
    ]

    if config.warnings:
        lines.append("#")
        lines.append("# Warnings:")
        for w in config.warnings:
            lines.append(f"#   - {w}")

    lines.append("")
    lines.append(f'duet_base_cpuset: "{config.base_cpuset}"')
    lines.append(f'duet_head_cpuset: "{config.head_cpuset}"')
    lines.append(f'duet_mongo_cpuset: "{config.infra_cpuset}"')

    return "\n".join(lines)


# =============================================================================
# CLI Interface
# =============================================================================
def main():
    """CLI entry point for CPU topology analysis."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Analyze CPU topology and compute optimal pinning for duet benchmarking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze local machine
  python -m cpu_topology

  # Analyze from lscpu output file
  python -m cpu_topology --from-file lscpu_output.txt

  # Analyze from experiment metadata
  python -m cpu_topology --from-metadata "0,0,0,0,1,1,0,0,..."

  # Specify strategy
  python -m cpu_topology --strategy isolated

  # Output as JSON
  python -m cpu_topology --json
""",
    )

    parser.add_argument(
        "--from-file",
        type=str,
        help="Read lscpu output from file instead of running locally",
    )
    parser.add_argument(
        "--from-metadata",
        type=str,
        help="Parse topology from experiment metadata string",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        choices=["shared", "isolated", "sibling", "auto"],
        default="auto",
        help="Pinning strategy (default: auto)",
    )
    parser.add_argument(
        "--workload",
        type=str,
        choices=["cpu_bound", "io_bound", "mixed"],
        default="io_bound",
        help="Workload type for auto-recommendation (default: io_bound)",
    )
    parser.add_argument(
        "--app-cores",
        type=int,
        default=None,
        help="Number of physical cores per app version (auto if not specified)",
    )
    parser.add_argument(
        "--infra-cores",
        type=int,
        default=1,
        help="Number of physical cores for infrastructure (default: 1)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--config-snippet",
        action="store_true",
        help="Output as config.yml snippet",
    )

    args = parser.parse_args()

    # Get topology
    try:
        if args.from_file:
            with open(args.from_file, encoding="utf-8") as f:
                topology = parse_lscpu_output(f.read())
        elif args.from_metadata:
            topology = parse_topology_metadata(args.from_metadata)
        else:
            topology = get_local_topology()
    except Exception as e:
        print(f"Error getting topology: {e}", file=sys.stderr)
        sys.exit(1)

    # Compute pinning
    try:
        if args.strategy == "auto":
            config, explanation = recommend_pinning(topology, args.workload)
        else:
            strategy = PinningStrategy(args.strategy)
            config = compute_optimal_pinning(
                topology,
                strategy=strategy,
                num_app_cores=args.app_cores,
                num_infra_cores=args.infra_cores,
            )
            explanation = None
    except Exception as e:
        print(f"Error computing pinning: {e}", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.json:
        print(json.dumps(config.to_dict(), indent=2))
    elif args.config_snippet:
        print(generate_config_snippet(config))
    else:
        if explanation:
            print(explanation)
        else:
            print(f"Topology: {config.topology_summary}")
            print(f"Strategy: {config.strategy.value}")
            print(f"OS cpuset: {config.os_cpuset}")
            print(f"Base cpuset: {config.base_cpuset}")
            print(f"Head cpuset: {config.head_cpuset}")
            print(f"Infra cpuset: {config.infra_cpuset}")
            if config.warnings:
                print("\nWarnings:")
                for w in config.warnings:
                    print(f"  - {w}")


if __name__ == "__main__":
    main()
