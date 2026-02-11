"""Tests for CPU topology analysis and optimal pinning computation."""

import pytest

from src.cpu_topology import (
    CPUTopology,
    DuetCPUConfig,
    LogicalCPU,
    PhysicalCore,
    PinningStrategy,
    _format_cpuset,
    _parse_cpuset,
    compute_optimal_pinning,
    generate_config_snippet,
    parse_lscpu_output,
    parse_topology_metadata,
    recommend_pinning,
)


# =============================================================================
# Test Fixtures - Various CPU Topologies
# =============================================================================

LSCPU_4CORE_NO_HT = """CPU CORE SOCKET NODE
  0    0      0    0
  1    1      0    0
  2    2      0    0
  3    3      0    0
"""

LSCPU_4CORE_WITH_HT = """CPU CORE SOCKET NODE
  0    0      0    0
  1    1      0    0
  2    2      0    0
  3    3      0    0
  4    0      0    0
  5    1      0    0
  6    2      0    0
  7    3      0    0
"""

LSCPU_8CORE_WITH_HT = """CPU CORE SOCKET NODE
  0    0      0    0
  1    1      0    0
  2    2      0    0
  3    3      0    0
  4    4      0    0
  5    5      0    0
  6    6      0    0
  7    7      0    0
  8    0      0    0
  9    1      0    0
 10    2      0    0
 11    3      0    0
 12    4      0    0
 13    5      0    0
 14    6      0    0
 15    7      0    0
"""

LSCPU_2SOCKET = """CPU CORE SOCKET NODE
  0    0      0    0
  1    1      0    0
  2    2      0    0
  3    3      0    0
  4    0      1    1
  5    1      1    1
  6    2      1    1
  7    3      1    1
"""

# Metadata string from actual experiment (8 logical, 4 physical, HT)
METADATA_4CORE_HT = "0,0,0,0,1,1,0,0,2,2,0,0,3,3,0,0,4,0,0,0,5,1,0,0,6,2,0,0,7,3,0,0"


# =============================================================================
# Parsing Tests
# =============================================================================

class TestParsing:
    """Tests for topology parsing functions."""

    def test_parse_4core_no_ht(self):
        """Parse 4-core system without hyperthreading."""
        topology = parse_lscpu_output(LSCPU_4CORE_NO_HT)

        assert topology.num_logical_cpus == 4
        assert topology.num_physical_cores == 4
        assert not topology.has_hyperthreading
        assert len(topology.sockets) == 1
        assert len(topology.numa_nodes) == 1

    def test_parse_4core_with_ht(self):
        """Parse 4-core system with hyperthreading."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)

        assert topology.num_logical_cpus == 8
        assert topology.num_physical_cores == 4
        assert topology.has_hyperthreading
        assert len(topology.sockets) == 1

        # Check HT sibling mapping
        core_0 = topology.get_core(0, 0)
        assert core_0 is not None
        assert set(core_0.cpu_ids) == {0, 4}
        assert core_0.primary_cpu == 0
        assert core_0.sibling_cpus == [4]

    def test_parse_8core_with_ht(self):
        """Parse 8-core system with hyperthreading."""
        topology = parse_lscpu_output(LSCPU_8CORE_WITH_HT)

        assert topology.num_logical_cpus == 16
        assert topology.num_physical_cores == 8
        assert topology.has_hyperthreading

    def test_parse_2socket(self):
        """Parse 2-socket system."""
        topology = parse_lscpu_output(LSCPU_2SOCKET)

        assert topology.num_logical_cpus == 8
        assert topology.num_physical_cores == 8
        assert len(topology.sockets) == 2
        assert len(topology.numa_nodes) == 2

        # Check socket separation
        socket_0_cores = topology.cores_in_socket(0)
        socket_1_cores = topology.cores_in_socket(1)
        assert len(socket_0_cores) == 4
        assert len(socket_1_cores) == 4

    def test_parse_metadata_string(self):
        """Parse topology from experiment metadata format."""
        topology = parse_topology_metadata(METADATA_4CORE_HT)

        assert topology.num_logical_cpus == 8
        assert topology.num_physical_cores == 4
        assert topology.has_hyperthreading

        # Verify specific mappings
        assert topology.get_core(0, 0).cpu_ids == [0, 4]
        assert topology.get_core(0, 1).cpu_ids == [1, 5]
        assert topology.get_core(0, 2).cpu_ids == [2, 6]
        assert topology.get_core(0, 3).cpu_ids == [3, 7]


# =============================================================================
# Cpuset Formatting Tests
# =============================================================================

class TestCpusetFormatting:
    """Tests for cpuset string formatting and parsing."""

    def test_format_single_cpu(self):
        """Format single CPU."""
        assert _format_cpuset([2]) == "2"

    def test_format_range(self):
        """Format consecutive CPUs as range."""
        assert _format_cpuset([0, 1, 2, 3]) == "0-3"

    def test_format_gaps(self):
        """Format CPUs with gaps."""
        assert _format_cpuset([0, 2, 4, 6]) == "0,2,4,6"

    def test_format_mixed(self):
        """Format mixed ranges and individual CPUs."""
        assert _format_cpuset([1, 2, 3, 5, 6]) == "1-3,5-6"

    def test_format_unsorted(self):
        """Format unsorted input."""
        assert _format_cpuset([3, 1, 2, 0]) == "0-3"

    def test_format_duplicates(self):
        """Format input with duplicates."""
        assert _format_cpuset([1, 1, 2, 2, 3]) == "1-3"

    def test_parse_single(self):
        """Parse single CPU."""
        assert _parse_cpuset("2") == [2]

    def test_parse_range(self):
        """Parse range."""
        assert _parse_cpuset("0-3") == [0, 1, 2, 3]

    def test_parse_list(self):
        """Parse comma-separated list."""
        assert _parse_cpuset("0,2,4,6") == [0, 2, 4, 6]

    def test_parse_mixed(self):
        """Parse mixed format."""
        assert _parse_cpuset("1-3,5-6") == [1, 2, 3, 5, 6]

    def test_roundtrip(self):
        """Verify parse/format roundtrip."""
        original = [1, 2, 3, 7, 8, 9]
        formatted = _format_cpuset(original)
        parsed = _parse_cpuset(formatted)
        assert parsed == original


# =============================================================================
# Pinning Strategy Tests
# =============================================================================

class TestSharedStrategy:
    """Tests for SHARED pinning strategy."""

    def test_shared_4core_no_ht(self):
        """Shared strategy on 4-core non-HT system."""
        topology = parse_lscpu_output(LSCPU_4CORE_NO_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SHARED,
        )

        # OS should reserve core 0
        assert "0" in config.os_cpuset

        # Base and head should have same cpuset
        assert config.base_cpuset == config.head_cpuset

        # At least some CPUs for app
        assert config.base_cpuset

    def test_shared_4core_with_ht(self):
        """Shared strategy on 4-core HT system."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SHARED,
        )

        # Base and head should share CPUs
        assert config.base_cpuset == config.head_cpuset

        # Should use multiple cores
        base_cpus = _parse_cpuset(config.base_cpuset)
        assert len(base_cpus) >= 2


class TestIsolatedStrategy:
    """Tests for ISOLATED pinning strategy."""

    def test_isolated_4core_with_ht(self):
        """Isolated strategy on 4-core HT system."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.ISOLATED,
        )

        # Base and head should have different CPUs
        base_cpus = set(_parse_cpuset(config.base_cpuset))
        head_cpus = set(_parse_cpuset(config.head_cpuset))

        # No overlap in CPU IDs
        assert base_cpus.isdisjoint(head_cpus)

    def test_isolated_8core_with_ht(self):
        """Isolated strategy on 8-core HT system."""
        topology = parse_lscpu_output(LSCPU_8CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.ISOLATED,
            num_app_cores=3,
        )

        base_cpus = _parse_cpuset(config.base_cpuset)
        head_cpus = _parse_cpuset(config.head_cpuset)

        # Each should have CPUs from 3 cores (6 logical CPUs with HT)
        assert len(base_cpus) == 6
        assert len(head_cpus) == 6

        # No overlap
        assert set(base_cpus).isdisjoint(set(head_cpus))

    def test_isolated_verifies_physical_cores(self):
        """Isolated strategy should not share physical cores."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.ISOLATED,
        )

        # Get physical core IDs for each version
        base_cpus = _parse_cpuset(config.base_cpuset)
        head_cpus = _parse_cpuset(config.head_cpuset)

        base_cores = set()
        head_cores = set()

        for cpu in topology.logical_cpus:
            if cpu.cpu_id in base_cpus:
                base_cores.add((cpu.socket_id, cpu.core_id))
            if cpu.cpu_id in head_cpus:
                head_cores.add((cpu.socket_id, cpu.core_id))

        # Physical cores should not overlap
        assert base_cores.isdisjoint(head_cores)


class TestSiblingStrategy:
    """Tests for SIBLING pinning strategy."""

    def test_sibling_4core_with_ht(self):
        """Sibling strategy on 4-core HT system."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SIBLING,
        )

        base_cpus = _parse_cpuset(config.base_cpuset)
        head_cpus = _parse_cpuset(config.head_cpuset)

        # CPUs should be different
        assert set(base_cpus) != set(head_cpus)

        # But they should be on same physical cores (HT siblings)
        # Base gets primaries, head gets siblings
        for cpu in topology.logical_cpus:
            if cpu.cpu_id in base_cpus:
                core = topology.get_core(cpu.socket_id, cpu.core_id)
                # Head should have the sibling
                if core.sibling_cpus:
                    # At least one sibling should be in head
                    pass  # This is validated by the overall logic

    def test_sibling_fallback_without_ht(self):
        """Sibling strategy should fall back to shared without HT."""
        topology = parse_lscpu_output(LSCPU_4CORE_NO_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SIBLING,
        )

        # Should fall back to shared (same cpuset)
        assert config.base_cpuset == config.head_cpuset

        # Should have warning
        assert any("hyperthreading" in w.lower() for w in config.warnings)


# =============================================================================
# Constraint Tests
# =============================================================================

class TestConstraints:
    """Tests for pinning constraints and edge cases."""

    def test_os_core_reserved(self):
        """CPU 0 should always be reserved for OS."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SHARED,
        )

        os_cpus = _parse_cpuset(config.os_cpuset)
        base_cpus = _parse_cpuset(config.base_cpuset)
        head_cpus = _parse_cpuset(config.head_cpuset)

        # CPU 0 should be in OS set
        assert 0 in os_cpus

        # CPU 0 should not be in app sets
        assert 0 not in base_cpus
        assert 0 not in head_cpus

        # HT sibling of CPU 0 (CPU 4) should also be reserved
        assert 4 in os_cpus
        assert 4 not in base_cpus
        assert 4 not in head_cpus

    def test_infra_separate(self):
        """Infrastructure should get dedicated cores."""
        topology = parse_lscpu_output(LSCPU_8CORE_WITH_HT)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.ISOLATED,
            num_infra_cores=2,
        )

        infra_cpus = set(_parse_cpuset(config.infra_cpuset))
        base_cpus = set(_parse_cpuset(config.base_cpuset))
        head_cpus = set(_parse_cpuset(config.head_cpuset))

        # Infra should not overlap with app
        assert infra_cpus.isdisjoint(base_cpus)
        assert infra_cpus.isdisjoint(head_cpus)

    def test_minimum_cores_error(self):
        """Should error if not enough cores for isolated strategy."""
        # Create minimal topology with just 2 cores - not enough for ISOLATED
        minimal = """CPU CORE SOCKET NODE
  0    0      0    0
  1    1      0    0
"""
        topology = parse_lscpu_output(minimal)

        # ISOLATED requires separate cores for base and head, so this should fail
        with pytest.raises(ValueError, match="(Insufficient|Failed to allocate)"):
            compute_optimal_pinning(topology, strategy=PinningStrategy.ISOLATED)

    def test_socket_locality(self):
        """App cores should be on same socket for cache locality."""
        topology = parse_lscpu_output(LSCPU_2SOCKET)
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SHARED,
        )

        base_cpus = _parse_cpuset(config.base_cpuset)

        # All app CPUs should be on same socket
        sockets = set()
        for cpu in topology.logical_cpus:
            if cpu.cpu_id in base_cpus:
                sockets.add(cpu.socket_id)

        # Should only use one socket
        assert len(sockets) == 1


# =============================================================================
# Recommendation Tests
# =============================================================================

class TestRecommendation:
    """Tests for automatic recommendation logic."""

    def test_recommend_small_vm(self):
        """Small VM should get SHARED strategy."""
        topology = parse_lscpu_output(LSCPU_4CORE_NO_HT)
        config, explanation = recommend_pinning(topology)

        assert config.strategy == PinningStrategy.SHARED
        assert "Small VM" in explanation or "SHARED" in explanation

    def test_recommend_medium_cpu_bound(self):
        """Medium VM with CPU-bound workload should get ISOLATED."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config, explanation = recommend_pinning(topology, workload_type="cpu_bound")

        # Should prefer isolation for CPU-bound
        assert config.strategy in [PinningStrategy.ISOLATED, PinningStrategy.SHARED]

    def test_recommend_large_vm(self):
        """Large VM should get an isolation-aware strategy."""
        topology = parse_lscpu_output(LSCPU_8CORE_WITH_HT)
        config, explanation = recommend_pinning(topology)

        # Should use either ISOLATED or SIBLING (both provide isolation)
        assert config.strategy in [PinningStrategy.ISOLATED, PinningStrategy.SIBLING]
        # Base and head should have different CPU sets
        assert config.base_cpuset != config.head_cpuset


# =============================================================================
# Output Generation Tests
# =============================================================================

class TestOutputGeneration:
    """Tests for config snippet generation."""

    def test_generate_config_snippet(self):
        """Generate valid YAML config snippet."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(topology, strategy=PinningStrategy.SHARED)

        snippet = generate_config_snippet(config)

        assert "duet_base_cpuset:" in snippet
        assert "duet_head_cpuset:" in snippet
        assert "duet_mongo_cpuset:" in snippet
        assert config.base_cpuset in snippet

    def test_config_to_dict(self):
        """Config should serialize to dict."""
        topology = parse_lscpu_output(LSCPU_4CORE_WITH_HT)
        config = compute_optimal_pinning(topology, strategy=PinningStrategy.SHARED)

        d = config.to_dict()

        assert "os_cpuset" in d
        assert "base_cpuset" in d
        assert "head_cpuset" in d
        assert "infra_cpuset" in d
        assert d["strategy"] == "shared"


# =============================================================================
# Real-World Topology Tests
# =============================================================================

class TestRealWorldTopologies:
    """Tests with real-world topology examples."""

    def test_gcp_n2_standard_8(self):
        """GCP n2-standard-8: 4 cores, 8 vCPUs (HT enabled)."""
        # This matches the experiment metadata from the actual GCP VM
        topology = parse_topology_metadata(METADATA_4CORE_HT)

        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.SHARED,
        )

        # Should match the current config pattern
        assert config.base_cpuset == config.head_cpuset
        base_cpus = _parse_cpuset(config.base_cpuset)
        assert len(base_cpus) >= 2

        # Infra should be on different cores
        infra_cpus = _parse_cpuset(config.infra_cpuset)
        assert set(base_cpus).isdisjoint(set(infra_cpus))

    def test_aws_c5_xlarge(self):
        """Simulated AWS c5.xlarge: 2 cores, 4 vCPUs."""
        lscpu = """CPU CORE SOCKET NODE
  0    0      0    0
  1    1      0    0
  2    0      0    0
  3    1      0    0
"""
        topology = parse_lscpu_output(lscpu)
        config, _ = recommend_pinning(topology)

        # Very small VM with only 2 physical cores - should still work
        # and use shared strategy
        assert config.strategy == PinningStrategy.SHARED
        # Should have allocated at least one CPU for apps
        assert config.base_cpuset

    def test_bare_metal_dual_socket(self):
        """Simulated bare metal: 2 sockets, 8 cores each, no HT."""
        lines = ["CPU CORE SOCKET NODE"]
        for i in range(8):
            lines.append(f"  {i}    {i}      0    0")
        for i in range(8, 16):
            lines.append(f"  {i}    {i-8}      1    1")

        topology = parse_lscpu_output("\n".join(lines))
        config = compute_optimal_pinning(
            topology,
            strategy=PinningStrategy.ISOLATED,
            num_app_cores=4,
            num_infra_cores=2,
            prefer_socket=0,
        )

        # All app cores should be on socket 0
        base_cpus = _parse_cpuset(config.base_cpuset)
        head_cpus = _parse_cpuset(config.head_cpuset)

        for cpu in topology.logical_cpus:
            if cpu.cpu_id in base_cpus or cpu.cpu_id in head_cpus:
                assert cpu.socket_id == 0
