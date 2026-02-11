"""Tests for duet_prepare module."""

from pathlib import Path
import tempfile
import yaml

import pytest

from src.duet_prepare import (
    DuetConfig,
    DuetPrepareError,
    prepare_duet_compose,
    _find_main_service,
    _extract_port,
)


class TestExtractPort:
    """Tests for _extract_port function."""

    def test_simple_port(self):
        host, container = _extract_port("3000")
        assert host is None
        assert container == 3000

    def test_int_port(self):
        host, container = _extract_port(3000)
        assert host is None
        assert container == 3000

    def test_host_container_mapping(self):
        host, container = _extract_port("8080:3000")
        assert host == 8080
        assert container == 3000

    def test_variable_host_port(self):
        host, container = _extract_port("${PORT}:3000")
        assert host is None
        assert container == 3000

    def test_port_with_protocol(self):
        host, container = _extract_port("3000/tcp")
        assert container == 3000

    def test_mapping_with_protocol(self):
        host, container = _extract_port("8080:3000/tcp")
        assert host == 8080
        assert container == 3000


class TestFindMainService:
    """Tests for _find_main_service function."""

    def test_app_service(self):
        compose = {"services": {"app": {}, "redis": {}}}
        assert _find_main_service(compose) == "app"

    def test_api_service(self):
        compose = {"services": {"api": {}, "db": {}}}
        assert _find_main_service(compose) == "api"

    def test_server_service(self):
        compose = {"services": {"server": {}, "cache": {}}}
        assert _find_main_service(compose) == "server"

    def test_service_with_build(self):
        compose = {
            "services": {
                "redis": {"image": "redis:latest"},
                "backend": {"build": "."},
            }
        }
        assert _find_main_service(compose) == "backend"

    def test_single_service(self):
        compose = {"services": {"my-custom-app": {}}}
        assert _find_main_service(compose) == "my-custom-app"

    def test_no_services(self):
        compose = {"services": {}}
        assert _find_main_service(compose) is None

    def test_priority_order(self):
        compose = {"services": {"api": {}, "app": {}, "server": {}}}
        assert _find_main_service(compose) == "app"


class TestPrepareDuetCompose:
    """Tests for prepare_duet_compose function."""

    def test_basic_compose_generation(self, tmp_path: Path):
        """Test basic duet compose generation."""
        base_dir = tmp_path / "base"
        head_dir = tmp_path / "head"
        output_dir = tmp_path / "output"

        base_dir.mkdir()
        head_dir.mkdir()

        # Create a simple compose file in base
        compose = {
            "services": {
                "app": {
                    "build": ".",
                    "ports": ["3000:3000"],
                }
            }
        }
        (base_dir / "docker-compose.yml").write_text(yaml.dump(compose))

        # Create Dockerfiles
        (base_dir / "Dockerfile").write_text("FROM node:18\n")
        (head_dir / "Dockerfile").write_text("FROM node:18\n")

        result = prepare_duet_compose(
            base_dir=base_dir,
            head_dir=head_dir,
            output_dir=output_dir,
        )

        assert result.compose_path.exists()
        assert result.env_path.exists()
        assert result.base_container == "duet-base"
        assert result.head_container == "duet-head"
        assert result.base_port == 3001
        assert result.head_port == 3002

        # Load and verify the generated compose
        with open(result.compose_path) as f:
            content = f.read()
            # Skip comment lines
            yaml_start = content.find("services:")
            generated = yaml.safe_load(content[yaml_start:])

        assert "app-base" in generated["services"]
        assert "app-head" in generated["services"]
        assert generated["services"]["app-base"]["container_name"] == "duet-base"
        assert generated["services"]["app-head"]["container_name"] == "duet-head"

    def test_custom_config(self, tmp_path: Path):
        """Test with custom configuration."""
        base_dir = tmp_path / "base"
        head_dir = tmp_path / "head"
        output_dir = tmp_path / "output"

        base_dir.mkdir()
        head_dir.mkdir()

        compose = {"services": {"app": {"build": "."}}}
        (base_dir / "docker-compose.yml").write_text(yaml.dump(compose))

        config = DuetConfig(
            cpu_limit="4",
            memory_limit="4G",
            base_port=5001,
            head_port=5002,
            prefix="test",
            base_cpuset="0,1",
            head_cpuset="2,3",
        )

        result = prepare_duet_compose(
            base_dir=base_dir,
            head_dir=head_dir,
            output_dir=output_dir,
            config=config,
        )

        assert result.base_port == 5001
        assert result.head_port == 5002
        assert result.base_container == "test-base"
        assert result.head_container == "test-head"

        # Verify cpuset is included
        with open(result.compose_path) as f:
            content = f.read()
            yaml_start = content.find("services:")
            generated = yaml.safe_load(content[yaml_start:])

        assert generated["services"]["app-base"]["cpuset"] == "0,1"
        assert generated["services"]["app-head"]["cpuset"] == "2,3"
        assert generated["services"]["app-base"]["deploy"]["resources"]["limits"]["cpus"] == "4"
        assert generated["services"]["app-base"]["deploy"]["resources"]["limits"]["memory"] == "4G"

    def test_no_compose_file_generates_minimal(self, tmp_path: Path):
        """Test that a minimal compose is generated when no compose file exists."""
        base_dir = tmp_path / "base"
        head_dir = tmp_path / "head"
        output_dir = tmp_path / "output"

        base_dir.mkdir()
        head_dir.mkdir()

        result = prepare_duet_compose(
            base_dir=base_dir,
            head_dir=head_dir,
            output_dir=output_dir,
        )

        assert result.compose_path.exists()

    def test_preserves_environment_variables(self, tmp_path: Path):
        """Test that original environment variables are preserved."""
        base_dir = tmp_path / "base"
        head_dir = tmp_path / "head"
        output_dir = tmp_path / "output"

        base_dir.mkdir()
        head_dir.mkdir()

        compose = {
            "services": {
                "app": {
                    "build": ".",
                    "environment": {
                        "DATABASE_URL": "postgres://localhost",
                        "API_KEY": "${API_KEY}",
                    },
                }
            }
        }
        (base_dir / "docker-compose.yml").write_text(yaml.dump(compose))

        result = prepare_duet_compose(
            base_dir=base_dir,
            head_dir=head_dir,
            output_dir=output_dir,
        )

        with open(result.compose_path) as f:
            content = f.read()
            yaml_start = content.find("services:")
            generated = yaml.safe_load(content[yaml_start:])

        env = generated["services"]["app-base"]["environment"]
        assert env["DATABASE_URL"] == "postgres://localhost"
        assert env["API_KEY"] == "${API_KEY}"
        assert env["VERSION_TAG"] == "base"
        assert env["OTEL_AGENT_VERSION"] == "base"

    def test_list_style_environment(self, tmp_path: Path):
        """Test that list-style environment variables are converted."""
        base_dir = tmp_path / "base"
        head_dir = tmp_path / "head"
        output_dir = tmp_path / "output"

        base_dir.mkdir()
        head_dir.mkdir()

        compose = {
            "services": {
                "app": {
                    "build": ".",
                    "environment": [
                        "DATABASE_URL=postgres://localhost",
                        "NODE_ENV=production",
                    ],
                }
            }
        }
        (base_dir / "docker-compose.yml").write_text(yaml.dump(compose))

        result = prepare_duet_compose(
            base_dir=base_dir,
            head_dir=head_dir,
            output_dir=output_dir,
        )

        with open(result.compose_path) as f:
            content = f.read()
            yaml_start = content.find("services:")
            generated = yaml.safe_load(content[yaml_start:])

        env = generated["services"]["app-base"]["environment"]
        assert env["DATABASE_URL"] == "postgres://localhost"
        assert env["NODE_ENV"] == "production"
        assert env["VERSION_TAG"] == "base"


class TestDuetConfig:
    """Tests for DuetConfig dataclass."""

    def test_defaults(self):
        config = DuetConfig()
        assert config.cpu_limit == "2"
        assert config.cpu_reservation == "1"
        assert config.memory_limit == "2G"
        assert config.base_port == 3001
        assert config.head_port == 3002
        assert config.prefix == "duet"
        assert config.base_cpuset is None
        assert config.head_cpuset is None

    def test_custom_values(self):
        config = DuetConfig(
            cpu_limit="4",
            base_port=8001,
            head_port=8002,
            prefix="perf",
            base_cpuset="0-3",
        )
        assert config.cpu_limit == "4"
        assert config.base_port == 8001
        assert config.head_port == 8002
        assert config.prefix == "perf"
        assert config.base_cpuset == "0-3"

