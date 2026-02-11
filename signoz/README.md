# SigNoz (local) for `otel-agent`

This folder contains a vendored **SigNoz Docker Compose** setup that exposes:

- **SigNoz UI**: `http://localhost:3301`
- **OTLP gRPC**: `localhost:4317`
- **OTLP HTTP**: `localhost:4318`

## Start / stop

From repo root:

```bash
cd signoz
docker compose up -d
```

Stop:

```bash
cd signoz
docker compose down
```

## Send traces from the CLI

Use OTLP HTTP (works great with the default SigNoz collector):

```bash
OTEL_AGENT_OTEL=1 \
OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318" \
uv run otel-agent doctor
```

If you prefer OTLP gRPC:

```bash
OTEL_AGENT_OTEL=1 \
OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317" \
OTEL_EXPORTER_OTLP_PROTOCOL="grpc" \
uv run otel-agent doctor
```

## Notes

- Config files used by the compose live in `./common/` and are vendored from the SigNoz upstream repo.
- The stack persists data via Docker volumes (`signoz-clickhouse`, `signoz-sqlite`, `signoz-zookeeper-1`).

