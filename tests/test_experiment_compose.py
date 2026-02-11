from pathlib import Path

import yaml

from src.experiment_compose import prepare_experiment_compose


def test_prepare_experiment_compose_merges_ext_and_patches_env(tmp_path: Path):
    duet = {
        "services": {
            "app-base": {
                "container_name": "duet-base",
                "build": {"context": "/abs/base", "dockerfile": "Dockerfile"},
                "environment": {
                    "OTEL_AGENT_VERSION": "base",
                    "MONGO_URL": "mongodb://${SUPPORT_IP}:${MONGO_PORT}/test",
                    "PERMIFY_URL": "x",
                    "JWT_SECRET": "${JWT_SECRET}",
                },
            },
            "app-head": {
                "container_name": "duet-head",
                "build": {"context": "/abs/head", "dockerfile": "Dockerfile"},
                "environment": {
                    "OTEL_AGENT_VERSION": "head",
                    "MONGO_URL": "mongodb://${SUPPORT_IP}:${MONGO_PORT}/test",
                },
            },
        },
        "networks": {"duet-network": {"driver": "bridge"}},
    }

    ext = {
        "services": {"mongodb": {"image": "mongo:7", "healthcheck": {"test": "true"}}},
        "volumes": {"dbdata": {}},
    }

    wd = tmp_path / ".otel-agent"
    (wd / "base").mkdir(parents=True)
    duet_path = wd / "docker-compose.duet.yml"
    ext_path = wd / "base" / "docker-compose.ext.yml"

    duet_path.write_text(yaml.safe_dump(duet, sort_keys=False), encoding="utf-8")
    ext_path.write_text(yaml.safe_dump(ext, sort_keys=False), encoding="utf-8")

    out_path = tmp_path / "docker-compose.experiment.yml"
    prepare_experiment_compose(
        duet_compose_path=duet_path,
        ext_compose_path=ext_path,
        output_path=out_path,
    )

    composed = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert "mongodb" in composed["services"]

    base = composed["services"]["app-base"]
    assert base["build"]["context"] == "${BASE_DIR}"
    assert base["environment"]["MONGO_URL"] == "mongodb://mongodb:27017/test"
    assert "PERMIFY_URL" not in base["environment"]
    assert base["environment"]["JWT_SECRET"] == "unused"
    assert base["depends_on"]["mongodb"]["condition"] == "service_healthy"

    head = composed["services"]["app-head"]
    assert head["build"]["context"] == "${HEAD_DIR}"
    assert head["environment"]["MONGO_URL"] == "mongodb://mongodb:27017/test"
    assert head["depends_on"]["mongodb"]["condition"] == "service_healthy"


