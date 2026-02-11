from __future__ import annotations

import atexit
import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.status import Status, StatusCode


_INITIALIZED = False


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name, "")
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def otel_enabled() -> bool:
    """
    Decide whether to enable OpenTelemetry for the CLI.

    Design goals:
    - Never accidentally block local/dev runs or tests.
    - Enable with a single env var for debugging.
    - Respect standard OTEL knobs.
    """
    if _env_truthy("OTEL_SDK_DISABLED"):
        return False

    # Avoid network/exporter flakiness during tests unless explicitly forced.
    if os.environ.get("PYTEST_CURRENT_TEST") and not _env_truthy("OTEL_AGENT_FORCE_OTEL"):
        return False

    # Enable explicitly, or when a standard OTEL exporter is configured.
    if _env_truthy("OTEL_AGENT_OTEL"):
        return True

    if os.environ.get("OTEL_TRACES_EXPORTER"):
        return True

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return True

    return False


def init_otel(*, service_name: str, service_version: str | None = None) -> None:
    """
    Initialize OTel tracing for the current process.

    Defaults are chosen for SigNoz (OTLP):
    - If OTEL_TRACES_EXPORTER=console => prints spans to stderr.
    - Else uses OTLP exporter (protocol selected via OTEL_EXPORTER_OTLP_PROTOCOL).

    To enable quickly with SigNoz running locally:
      OTEL_AGENT_OTEL=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    if not otel_enabled():
        return

    resource_attrs: dict[str, Any] = {"service.name": service_name}
    if service_version:
        resource_attrs["service.version"] = service_version

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)

    traces_exporter = (os.environ.get("OTEL_TRACES_EXPORTER") or "").strip().lower()
    if traces_exporter == "console":
        exporter = ConsoleSpanExporter()
    else:
        protocol = (os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf").strip().lower()
        # SigNoz default OTLP ports:
        # - gRPC: 4317
        # - HTTP/protobuf: 4318
        if protocol in {"grpc"}:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        else:
            # default: http/protobuf
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    def _shutdown() -> None:
        try:
            provider.shutdown()
        except Exception:
            pass

    atexit.register(_shutdown)
    _INITIALIZED = True


@contextmanager
def cli_span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    """
    Create a span for CLI work, recording exceptions and adding attributes.
    """
    tracer = trace.get_tracer("otel-agent.cli")
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            if v is None:
                continue
            try:
                span.set_attribute(k, v)
            except Exception:
                span.set_attribute(k, str(v))
        try:
            yield span
        except Exception as e:
            try:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
            except Exception:
                pass
            raise


