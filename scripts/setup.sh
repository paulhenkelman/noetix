#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# --- Colors ---
BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

echo -e "${BOLD}=== Noetix Setup ===${NC}"
echo "Project root: $PROJECT_ROOT"
echo ""

# --- Installation mode selection ---
echo -e "${CYAN}Select installation mode:${NC}"
echo "  1) Full installation      — Noetix UI + Knowledge backend (single machine)"
echo "  2) Frontend only          — Noetix UI (connects to remote backend)"
echo "  3) Backend only           — Knowledge backend + MCP server"
echo ""
read -rp "Enter choice [1/2/3]: " INSTALL_MODE

case "$INSTALL_MODE" in
  1) MODE="full" ;;
  2) MODE="frontend" ;;
  3) MODE="backend" ;;
  *)
    echo "Invalid choice. Defaulting to full installation."
    MODE="full"
    ;;
esac

echo ""
echo -e "${GREEN}Installation mode: ${MODE}${NC}"
echo ""

# --- Frontend-only: ask for backend URL ---
if [ "$MODE" = "frontend" ]; then
  echo -e "${CYAN}Where is the backend running?${NC}"
  read -rp "Backend URL (e.g. http://10.0.0.50:8001): " BACKEND_URL
  BACKEND_URL="${BACKEND_URL:-http://10.0.0.50:8001}"

  # Validate URL format
  if [[ ! "$BACKEND_URL" =~ ^https?:// ]]; then
    echo "Warning: URL should start with http:// or https://"
    echo "Adding http:// prefix..."
    BACKEND_URL="http://${BACKEND_URL}"
  fi

  echo -e "${GREEN}Backend URL: ${BACKEND_URL}${NC}"

  # Update ui.config with the backend URL
  if command -v sed &>/dev/null; then
    sed -i "s|^url = .*|url = \"${BACKEND_URL}\"|" "$PROJECT_ROOT/ui.config"
    echo "Updated ui.config with backend URL"
  fi

  # Ask about SOCKS proxy
  read -rp "SOCKS proxy (leave empty if not needed): " SOCKS_PROXY
  if [ -n "$SOCKS_PROXY" ]; then
    sed -i "s|^socks_proxy = .*|socks_proxy = \"${SOCKS_PROXY}\"|" "$PROJECT_ROOT/ui.config"
    echo "Updated ui.config with SOCKS proxy"
  fi

  echo ""
fi

# --- Full install: ask about split deployment ---
if [ "$MODE" = "full" ]; then
  echo -e "${CYAN}Network configuration:${NC}"
  read -rp "Backend host IP for remote access (default: 0.0.0.0): " BACKEND_HOST
  BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"

  read -rp "Noetix UI port (default: 8788): " GW_PORT
  GW_PORT="${GW_PORT:-8788}"

  read -rp "Backend port (default: 8001): " BE_PORT
  BE_PORT="${BE_PORT:-8001}"

  read -rp "Frontend dev port (default: 5174): " FE_PORT
  FE_PORT="${FE_PORT:-5174}"

  # Update configs
  sed -i "s|^port = 8788|port = ${GW_PORT}|" "$PROJECT_ROOT/ui.config"
  sed -i "s|^port = 8001|port = ${BE_PORT}|" "$PROJECT_ROOT/knowledge.config"
  sed -i "s|^vite_port = 5174|vite_port = ${FE_PORT}|" "$PROJECT_ROOT/ui.config"

  # For full install, backend is localhost
  BACKEND_URL="http://127.0.0.1:${BE_PORT}"
  sed -i "s|^url = .*|url = \"${BACKEND_URL}\"|" "$PROJECT_ROOT/ui.config"

  # Update frontend api_base to match server port
  sed -i "s|^api_base = .*|api_base = \"http://127.0.0.1:${GW_PORT}\"|" "$PROJECT_ROOT/ui.config"
  # Update CORS origins
  sed -i "s|localhost:5174|localhost:${FE_PORT}|g" "$PROJECT_ROOT/ui.config"
  sed -i "s|127.0.0.1:5174|127.0.0.1:${FE_PORT}|g" "$PROJECT_ROOT/ui.config"

  echo ""
fi

# --- Backend-only: configure host/port ---
if [ "$MODE" = "backend" ]; then
  echo -e "${CYAN}Backend configuration:${NC}"
  read -rp "Listen host (default: 0.0.0.0): " BE_HOST
  BE_HOST="${BE_HOST:-0.0.0.0}"

  read -rp "Listen port (default: 8001): " BE_PORT
  BE_PORT="${BE_PORT:-8001}"

  sed -i "s|^host = .*|host = \"${BE_HOST}\"|" "$PROJECT_ROOT/knowledge.config"
  sed -i "s|^port = 8001|port = ${BE_PORT}|" "$PROJECT_ROOT/knowledge.config"

  echo ""
fi

# ============================================================
# Install Noetix UI
# ============================================================
if [ "$MODE" = "full" ] || [ "$MODE" = "frontend" ]; then
  echo -e "${BOLD}--- Installing UI dependencies ---${NC}"
  cd "$PROJECT_ROOT/ui"
  npm install
  echo ""

  echo -e "${BOLD}--- Generating codex config ---${NC}"
  cd "$PROJECT_ROOT"
  node scripts/generate-codex-config.js
  echo ""

  echo -e "${BOLD}--- Creating data directories ---${NC}"
  mkdir -p "$PROJECT_ROOT/ui/data"
  for f in chat_sessions.json action_requests.json; do
    [ -f "$PROJECT_ROOT/ui/data/$f" ] || echo '[]' > "$PROJECT_ROOT/ui/data/$f"
  done
  [ -f "$PROJECT_ROOT/ui/data/kb_dependencies.json" ] || echo '{}' > "$PROJECT_ROOT/ui/data/kb_dependencies.json"

  PLAYWRIGHT_DIR="${HOME}/.cache/noetix-playwright"
  mkdir -p "$PLAYWRIGHT_DIR"
  echo "Created $PLAYWRIGHT_DIR"
  echo ""

  # Systemd service for noetix-ui
  SYSTEMD_DIR="${HOME}/.config/systemd/user"
  mkdir -p "$SYSTEMD_DIR"

  cat > "$SYSTEMD_DIR/noetix-ui.service" <<EOF
[Unit]
Description=Noetix UI
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT/ui
ExecStart=/usr/bin/node src/server/server.js
Restart=on-failure
RestartSec=3
Environment=NODE_ENV=production

[Install]
WantedBy=default.target
EOF

  echo "Created $SYSTEMD_DIR/noetix-ui.service"
fi

# ============================================================
# Install Backend
# ============================================================
if [ "$MODE" = "full" ] || [ "$MODE" = "backend" ]; then
  echo ""
  echo -e "${BOLD}--- Setting up backend ---${NC}"
  cd "$PROJECT_ROOT/knowledge"

  # Create data directories
  mkdir -p uploads library knowledge_bases data

  # Create virtual environment if it doesn't exist
  if [ ! -d "$PROJECT_ROOT/knowledge/.venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
  fi

  # Install Python dependencies
  echo "Installing Python dependencies..."
  .venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
  echo ""

  # Copy .env.example if .env doesn't exist
  if [ ! -f "$PROJECT_ROOT/knowledge/.env" ]; then
    cp "$PROJECT_ROOT/knowledge/.env.example" "$PROJECT_ROOT/knowledge/.env"
    echo "Created .env from .env.example — edit to add your OpenAI API key"
  fi

  # Systemd service for backend
  SYSTEMD_DIR="${HOME}/.config/systemd/user"
  mkdir -p "$SYSTEMD_DIR"

  cat > "$SYSTEMD_DIR/noetix-knowledge.service" <<SVCEOF
[Unit]
Description=Noetix Knowledge Backend
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT/knowledge
ExecStart=$PROJECT_ROOT/knowledge/.venv/bin/uvicorn server:app --host ${BE_HOST:-0.0.0.0} --port ${BE_PORT:-8001}
Restart=on-failure
RestartSec=3
Environment=PYTHONPATH=$PROJECT_ROOT/knowledge

[Install]
WantedBy=default.target
SVCEOF

  echo "Created $SYSTEMD_DIR/noetix-knowledge.service"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo -e "${BOLD}=== Setup complete ===${NC}"
echo ""

if [ "$MODE" = "full" ] || [ "$MODE" = "frontend" ]; then
  echo -e "${GREEN}Noetix UI:${NC}"
  echo "  Start server:    cd ui && npm run dev"
  echo "  Start frontend:  cd ui && npm run dev:frontend"
  echo "  Systemd:         systemctl --user enable --now noetix-ui.service"
  echo ""
fi

if [ "$MODE" = "full" ] || [ "$MODE" = "backend" ]; then
  echo -e "${GREEN}Backend:${NC}"
  echo "  Start backend:   cd knowledge && .venv/bin/uvicorn server:app --host 0.0.0.0 --port ${BE_PORT:-8001}"
  echo "  Systemd:         systemctl --user enable --now noetix-knowledge.service"
  echo ""
fi

if [ "$MODE" = "frontend" ]; then
  echo -e "${YELLOW}Note: Backend URL is set to ${BACKEND_URL}${NC}"
  echo "  Edit ui.config [backend] section to change this later."
  echo ""
fi

echo "Config files:"
echo "  Global:   $PROJECT_ROOT/noetix.config"
echo "  Frontend: $PROJECT_ROOT/ui.config"
echo "  Backend:  $PROJECT_ROOT/knowledge.config"
