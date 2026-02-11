from __future__ import annotations

from pathlib import Path

from src.perf_instrument_pipeline import (
    _inject_express_tracing_middleware,
    _inject_measure_import,
)


def test_inject_express_tracing_middleware_basic(tmp_path: Path) -> None:
    """Test that withTracing middleware is injected for Express apps."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    
    # Create a basic Express app file
    app_ts = '''import express from "express";

const app = express();

app.use(express.json());

app.get("/", (req, res) => {
  res.send("Hello");
});

app.listen(3000);
'''
    app_file = src_dir / "app.ts"
    app_file.write_text(app_ts)
    
    # First inject the measure import
    _inject_measure_import(tmp_path, "src/app.ts")
    
    # Then inject the Express tracing middleware
    result = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    
    assert result is True
    
    content = app_file.read_text()
    
    # Should have withTracing import
    assert "withTracing" in content
    assert 'from "./measure"' in content
    
    # Should have app.use(withTracing)
    assert "app.use(withTracing)" in content
    
    # withTracing should be before express.json middleware
    assert content.find("app.use(withTracing)") < content.find("app.use(express.json())")


def test_inject_express_tracing_middleware_idempotent(tmp_path: Path) -> None:
    """Test that running injection twice doesn't duplicate the middleware."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    
    app_ts = '''import express from "express";

const app = express();

app.listen(3000);
'''
    app_file = src_dir / "app.ts"
    app_file.write_text(app_ts)
    
    _inject_measure_import(tmp_path, "src/app.ts")
    
    # First injection
    result1 = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    assert result1 is True
    content1 = app_file.read_text()
    
    # Second injection should be a no-op
    result2 = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    assert result2 is False
    content2 = app_file.read_text()
    
    assert content1 == content2
    assert content1.count("app.use(withTracing)") == 1


def test_inject_express_tracing_skips_non_express(tmp_path: Path) -> None:
    """Test that non-Express files are not modified."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    
    # A non-Express TypeScript file
    app_ts = '''import http from "http";

const server = http.createServer((req, res) => {
  res.end("Hello");
});

server.listen(3000);
'''
    app_file = src_dir / "app.ts"
    app_file.write_text(app_ts)
    
    result = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    
    assert result is False
    content = app_file.read_text()
    assert "withTracing" not in content


def test_inject_express_tracing_preserves_indentation(tmp_path: Path) -> None:
    """Test that middleware injection preserves code indentation."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    
    # Express app with conditional creation (nested)
    app_ts = '''import express from "express";
import cluster from "cluster";

if (cluster.isPrimary) {
  console.log("Master");
} else {
  const app = express();

  app.use(express.json());

  app.listen(3000);
}
'''
    app_file = src_dir / "app.ts"
    app_file.write_text(app_ts)
    
    _inject_measure_import(tmp_path, "src/app.ts")
    result = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    
    assert result is True
    content = app_file.read_text()
    
    # Should have proper indentation (2 spaces, matching the else block)
    assert "  app.use(withTracing);" in content


def test_inject_express_tracing_different_app_var_name(tmp_path: Path) -> None:
    """Test that injection works with different variable names for the Express app."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    
    app_ts = '''import express from "express";

const server = express();

server.listen(3000);
'''
    app_file = src_dir / "app.ts"
    app_file.write_text(app_ts)
    
    _inject_measure_import(tmp_path, "src/app.ts")
    result = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    
    assert result is True
    content = app_file.read_text()
    
    # Should use the correct variable name
    assert "server.use(withTracing)" in content


def test_inject_express_tracing_single_quotes(tmp_path: Path) -> None:
    """Test that Express apps using single quotes are detected."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    
    app_ts = """import express from 'express';

const app = express();

app.listen(3000);
"""
    app_file = src_dir / "app.ts"
    app_file.write_text(app_ts)
    
    _inject_measure_import(tmp_path, "src/app.ts")
    result = _inject_express_tracing_middleware(tmp_path, "src/app.ts")
    
    assert result is True
    content = app_file.read_text()
    assert "app.use(withTracing)" in content

