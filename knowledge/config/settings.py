"""
Noetix knowledge backend configuration.

Reads knowledge.config (TOML) from the project root, with env var overrides
for secrets and runtime settings.
"""

import os
import json
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "knowledge.config"


def _load_config():
    if CONFIG_PATH.exists() and tomllib is not None:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


_cfg = _load_config()


class Settings:
    """Application settings, loaded from knowledge.config with env var overrides."""

    # Paths (relative to knowledge/ directory)
    BASE_DIR = KNOWLEDGE_ROOT
    UPLOAD_DIR = KNOWLEDGE_ROOT / _cfg.get("storage", {}).get("uploads_dir", "uploads")
    LIBRARY_DIR = KNOWLEDGE_ROOT / _cfg.get("storage", {}).get("library_dir", "library")
    KB_DIR = KNOWLEDGE_ROOT / _cfg.get("storage", {}).get("knowledge_bases_dir", "knowledge_bases")
    LANCEDB_PATH = KB_DIR / "lancedb"

    # Server
    host: str = os.getenv("HOST", _cfg.get("server", {}).get("host", "0.0.0.0"))
    port: int = int(os.getenv("PORT", _cfg.get("server", {}).get("port", 8001)))
    external_host: str = _cfg.get("server", {}).get("external_host", "10.0.0.50")

    # Storage dir names (for server.py config reads)
    uploads_dir: str = str(UPLOAD_DIR)
    library_dir: str = str(LIBRARY_DIR)
    knowledge_bases_dir: str = str(KB_DIR)

    # OpenAI
    OPENAI_API_KEY_FILE = Path.home() / ".openai"
    openai_api_key: str = os.getenv(
        "OPENAI_API_KEY", _cfg.get("openai", {}).get("api_key", "")
    )
    extraction_model: str = _cfg.get("openai", {}).get("extraction_model", "gpt-5.1")
    extraction_model_mini: str = _cfg.get("openai", {}).get(
        "extraction_model_mini", "gpt-5-mini"
    )
    OPENAI_EMBEDDING_MODEL: str = _cfg.get("openai", {}).get(
        "embedding_model", "text-embedding-3-large"
    )
    OPENAI_EMBEDDING_DIMENSIONS: int = _cfg.get("openai", {}).get(
        "embedding_dimensions", 3072
    )

    # Chunking
    CHUNK_SIZE: int = _cfg.get("chunking", {}).get("chunk_size", 512)
    CHUNK_OVERLAP: int = _cfg.get("chunking", {}).get("chunk_overlap", 50)

    # Neo4j
    neo4j_enabled: bool = _cfg.get("neo4j", {}).get("enabled", False)
    NEO4J_URI: str = os.getenv(
        "NEO4J_URI", _cfg.get("neo4j", {}).get("uri", "bolt://localhost:7687")
    )
    NEO4J_USER: str = os.getenv(
        "NEO4J_USER", _cfg.get("neo4j", {}).get("user", "neo4j")
    )
    NEO4J_PASSWORD: str = os.getenv(
        "NEO4J_PASSWORD", _cfg.get("neo4j", {}).get("password", "password")
    )

    # MCP
    MCP_SERVER_PORT: int = int(
        os.getenv("MCP_PORT", _cfg.get("mcp", {}).get("port", 8002))
    )

    # Playwright
    playwright_mcp_url: str = _cfg.get("playwright", {}).get(
        "mcp_url", "http://localhost:3000/sse"
    )

    @classmethod
    def get_openai_key(cls) -> str:
        """Load OpenAI API key from env, config, or ~/.openai file."""
        # Check env var first
        env_key = os.getenv("OPENAI_API_KEY", "")
        if env_key:
            return env_key
        # Check config value
        if cls.openai_api_key:
            return cls.openai_api_key
        # Fall back to ~/.openai file
        key_file = cls.OPENAI_API_KEY_FILE
        if key_file.exists():
            content = key_file.read_text().strip()
            if content.startswith('{'):
                return json.loads(content).get('api_key', content)
            return content
        raise ValueError(f"OpenAI API key not found in env, config, or {key_file}")

    @classmethod
    def ensure_directories(cls):
        """Create required directories if they don't exist."""
        cls.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        cls.KB_DIR.mkdir(parents=True, exist_ok=True)
        cls.LANCEDB_PATH.mkdir(parents=True, exist_ok=True)


# Singleton instance
settings = Settings()
