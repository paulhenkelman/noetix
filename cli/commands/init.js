/**
 * noetix init — interactive setup command.
 *
 * Walks the user through full / frontend-only / backend-only installation,
 * checking prerequisites, generating configs, installing deps.
 */

import { confirm, select, input, password } from '@inquirer/prompts';
import chalk from 'chalk';
import ora from 'ora';
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import { fileURLToPath } from 'url';
import {
  isCodexInstalled, getCodexVersion, isCodexLoggedIn,
  installCodex, launchCodexLogin,
  isDockerInstalled, isDockerRunning, hasNvidiaGpu,
  isFfmpegInstalled, isNodeVersionOk,
} from '../lib/checks.js';

const __filename = fileURLToPath(import.meta.url);
const CLI_ROOT = path.resolve(path.dirname(__filename), '..', '..');

function templatePath(name) {
  return path.join(CLI_ROOT, 'cli', 'templates', name);
}

function readTemplate(name) {
  return fs.readFileSync(templatePath(name), 'utf-8');
}

function expandHome(p) {
  if (typeof p === 'string' && p.startsWith('~/')) {
    return path.join(process.env.HOME || '/root', p.slice(2));
  }
  return p;
}

export async function init(options) {
  console.log('');
  console.log(chalk.bold('  Noetix Setup'));
  console.log(chalk.dim('  AI-powered knowledge platform'));
  console.log('');

  // --- Mode selection ---
  const mode = options.mode || await select({
    message: 'Installation mode',
    choices: [
      { name: 'Full installation      — Frontend + Gateway + Backend (single machine)', value: 'full' },
      { name: 'Frontend only          — UI + Gateway (connects to remote backend)', value: 'frontend' },
      { name: 'Backend only           — Knowledge backend (GPU server)', value: 'backend' },
    ],
  });

  const installDir = path.resolve(options.dir || '.');
  const needsFrontend = mode === 'full' || mode === 'frontend';
  const needsBackend = mode === 'full' || mode === 'backend';

  console.log('');
  console.log(chalk.cyan(`Mode: ${mode}`));
  console.log(chalk.cyan(`Directory: ${installDir}`));
  console.log('');

  // =================================================================
  // Prerequisites
  // =================================================================

  if (needsFrontend) {
    await checkCodex();
  }

  if (needsBackend) {
    await checkBackendPrereqs();
  }

  // =================================================================
  // Configuration prompts
  // =================================================================

  const config = { mode };

  if (needsFrontend) {
    config.gatewayPort = await input({ message: 'Gateway port', default: '8788' });
    config.vitePort = await input({ message: 'Frontend dev port', default: '5174' });
  }

  if (mode === 'frontend') {
    config.backendUrl = await input({
      message: 'Backend URL (where the knowledge server is running)',
      default: 'http://10.0.0.50:8001',
    });
    // Normalize URL
    if (!/^https?:\/\//.test(config.backendUrl)) {
      config.backendUrl = `http://${config.backendUrl}`;
    }
    config.socksProxy = await input({
      message: 'SOCKS proxy (leave empty if not needed)',
      default: '',
    });
  }

  if (needsBackend) {
    config.backendPort = await input({ message: 'Backend port', default: '8001' });
    config.backendHost = await input({ message: 'Backend listen host', default: '0.0.0.0' });
    config.openaiKey = await password({
      message: 'OpenAI API key (or press Enter to configure later)',
      mask: '*',
    });
  }

  if (mode === 'full') {
    config.backendUrl = `http://127.0.0.1:${config.backendPort}`;
    config.socksProxy = '';
  }

  // =================================================================
  // Generate config files
  // =================================================================

  console.log('');
  const spinner = ora('Generating configuration files').start();

  fs.mkdirSync(installDir, { recursive: true });

  // noetix.config
  const noetixConfig = readTemplate('noetix.config');
  fs.writeFileSync(path.join(installDir, 'noetix.config'), noetixConfig);

  // ui.config
  if (needsFrontend) {
    let uiConfig = readTemplate('ui.config');
    uiConfig = uiConfig.replace(/^port = 8788$/m, `port = ${config.gatewayPort}`);
    uiConfig = uiConfig.replace(/^vite_port = 5174$/m, `vite_port = ${config.vitePort}`);
    uiConfig = uiConfig.replace(/^api_base = .*$/m, `api_base = "http://127.0.0.1:${config.gatewayPort}"`);
    if (config.backendUrl) {
      uiConfig = uiConfig.replace(/^url = .*$/m, `url = "${config.backendUrl}"`);
    }
    if (config.socksProxy !== undefined) {
      uiConfig = uiConfig.replace(/^socks_proxy = .*$/m, `socks_proxy = "${config.socksProxy}"`);
    }
    uiConfig = uiConfig.replace(/localhost:5174/g, `localhost:${config.vitePort}`);
    uiConfig = uiConfig.replace(/127\.0\.0\.1:5174/g, `127.0.0.1:${config.vitePort}`);
    fs.writeFileSync(path.join(installDir, 'ui.config'), uiConfig);
  }

  // knowledge.config
  if (needsBackend) {
    let knowledgeConfig = readTemplate('knowledge.config');
    knowledgeConfig = knowledgeConfig.replace(/^port = 8001$/m, `port = ${config.backendPort}`);
    knowledgeConfig = knowledgeConfig.replace(/^host = .*$/m, `host = "${config.backendHost}"`);
    if (config.openaiKey) {
      knowledgeConfig = knowledgeConfig.replace(/^api_key = ""$/m, `api_key = "${config.openaiKey}"`);
    }
    fs.writeFileSync(path.join(installDir, 'knowledge.config'), knowledgeConfig);
  }

  spinner.succeed('Configuration files generated');

  // =================================================================
  // Install frontend
  // =================================================================

  if (needsFrontend) {
    await installFrontend(installDir, config);
  }

  // =================================================================
  // Install backend
  // =================================================================

  if (needsBackend) {
    await installBackend(installDir, config);
  }

  // =================================================================
  // Generate codex config
  // =================================================================

  if (needsFrontend) {
    const spinner = ora('Generating codex config').start();
    try {
      execSync(`node ${path.join(installDir, 'scripts', 'generate-codex-config.js')}`, {
        cwd: installDir, stdio: 'pipe',
      });
      spinner.succeed('Codex config generated (~/.codex/config.toml)');
    } catch (err) {
      spinner.warn('Codex config generation skipped (run manually: node scripts/generate-codex-config.js)');
    }
  }

  // =================================================================
  // Create systemd services
  // =================================================================

  if (process.platform === 'linux') {
    await createSystemdServices(installDir, config);
  }

  // =================================================================
  // Summary
  // =================================================================

  console.log('');
  console.log(chalk.bold.green('  Setup complete!'));
  console.log('');

  if (needsFrontend) {
    console.log(chalk.white('  Frontend/Gateway:'));
    console.log(chalk.dim(`    Start:   noetix start frontend`));
    console.log(chalk.dim(`    Or:      cd ${installDir} && cd ui && npm run dev`));
    console.log(chalk.dim(`    Gateway: http://127.0.0.1:${config.gatewayPort}`));
    console.log('');
  }

  if (needsBackend) {
    console.log(chalk.white('  Backend:'));
    console.log(chalk.dim(`    Start:   noetix start backend`));
    console.log(chalk.dim(`    Or:      docker compose -f ${path.join(installDir, 'docker-compose.yml')} up -d`));
    console.log(chalk.dim(`    API:     http://${config.backendHost}:${config.backendPort}`));
    if (!config.openaiKey) {
      console.log(chalk.yellow(`    Note:    Set OpenAI API key in ${path.join(installDir, 'knowledge.config')}`));
    }
    console.log('');
  }

  if (mode === 'frontend') {
    console.log(chalk.dim(`  Backend URL: ${config.backendUrl}`));
    console.log(chalk.dim(`  Change in: ${path.join(installDir, 'ui.config')} [backend] section`));
    console.log('');
  }

  console.log(chalk.dim('  Quick start: noetix start'));
  console.log(chalk.dim('  Status:      noetix status'));
  console.log('');

  // Write install state so start/stop/status know what's configured
  const state = {
    mode,
    installDir,
    gatewayPort: config.gatewayPort,
    backendPort: config.backendPort,
    backendUrl: config.backendUrl,
    vitePort: config.vitePort,
    installedAt: new Date().toISOString(),
  };
  fs.writeFileSync(path.join(installDir, '.noetix-state.json'), JSON.stringify(state, null, 2));
}

// -----------------------------------------------------------------
// Codex prerequisite check
// -----------------------------------------------------------------

async function checkCodex() {
  console.log(chalk.dim('Checking prerequisites...'));

  if (!isCodexInstalled()) {
    console.log(chalk.yellow('  Codex CLI is not installed.'));
    const doInstall = await confirm({
      message: 'Install Codex CLI now? (npm install -g @openai/codex)',
      default: true,
    });
    if (doInstall) {
      const spinner = ora('Installing Codex CLI').start();
      try {
        installCodex();
        spinner.succeed(`Codex CLI installed (${getCodexVersion()})`);
      } catch (err) {
        spinner.fail('Failed to install Codex CLI');
        console.log(chalk.red(`  Error: ${err.message}`));
        console.log(chalk.dim('  Install manually: npm install -g @openai/codex'));
        const proceed = await confirm({ message: 'Continue without Codex?', default: false });
        if (!proceed) process.exit(1);
      }
    } else {
      console.log(chalk.dim('  Skipping Codex install. Install later: npm install -g @openai/codex'));
    }
  } else {
    console.log(chalk.green(`  Codex CLI: ${getCodexVersion()}`));
  }

  // Check login
  if (isCodexInstalled() && !isCodexLoggedIn()) {
    console.log(chalk.yellow('  Codex is not logged in.'));
    const doLogin = await confirm({
      message: 'Log in to Codex now? (opens browser)',
      default: true,
    });
    if (doLogin) {
      console.log(chalk.dim('  Opening browser for Codex authentication...'));
      launchCodexLogin();
      if (isCodexLoggedIn()) {
        console.log(chalk.green('  Codex login successful'));
      } else {
        console.log(chalk.yellow('  Codex login may not have completed. You can retry with: codex login'));
      }
    } else {
      console.log(chalk.dim('  Log in later: codex login'));
    }
  } else if (isCodexInstalled()) {
    console.log(chalk.green('  Codex: logged in'));
  }

  if (!isNodeVersionOk()) {
    console.log(chalk.red('  Node.js 18+ is required'));
    process.exit(1);
  }

  console.log('');
}

// -----------------------------------------------------------------
// Backend prerequisite check
// -----------------------------------------------------------------

async function checkBackendPrereqs() {
  console.log(chalk.dim('Checking backend prerequisites...'));

  if (!isDockerInstalled()) {
    console.log(chalk.red('  Docker is not installed.'));
    console.log(chalk.dim('  Install from: https://docs.docker.com/get-docker/'));
    console.log(chalk.dim('  For GPU support also install: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/'));
    const proceed = await confirm({ message: 'Continue anyway? (backend will not work without Docker)', default: false });
    if (!proceed) process.exit(1);
  } else if (!isDockerRunning()) {
    console.log(chalk.yellow('  Docker is installed but not running.'));
    console.log(chalk.dim('  Start it with: sudo systemctl start docker'));
  } else {
    console.log(chalk.green('  Docker: running'));
  }

  if (hasNvidiaGpu()) {
    console.log(chalk.green('  NVIDIA GPU: detected'));
  } else {
    console.log(chalk.yellow('  NVIDIA GPU: not detected — TTS/STT/OCR will use CPU (much slower)'));
  }

  if (!isFfmpegInstalled()) {
    console.log(chalk.yellow('  ffmpeg: not found on host (included in Docker image, but needed for local dev)'));
  } else {
    console.log(chalk.green('  ffmpeg: installed'));
  }

  console.log('');
}

// -----------------------------------------------------------------
// Frontend installation
// -----------------------------------------------------------------

async function installFrontend(installDir, config) {
  const uiDir = path.join(installDir, 'ui');

  // If ui/ doesn't exist in installDir, copy it from the CLI package
  if (!fs.existsSync(path.join(uiDir, 'package.json'))) {
    const spinner = ora('Setting up frontend').start();
    const cliUiDir = path.join(CLI_ROOT, 'ui');
    if (fs.existsSync(cliUiDir)) {
      fs.cpSync(cliUiDir, uiDir, { recursive: true, filter: (src) => !src.includes('node_modules') });
      spinner.succeed('Frontend source files installed');
    } else {
      spinner.warn('Frontend source not found in CLI package — skipping copy');
      return;
    }
  }

  // Copy scripts/ for codex config generation
  const scriptsDir = path.join(installDir, 'scripts');
  if (!fs.existsSync(scriptsDir)) {
    const cliScriptsDir = path.join(CLI_ROOT, 'scripts');
    if (fs.existsSync(cliScriptsDir)) {
      fs.cpSync(cliScriptsDir, scriptsDir, { recursive: true });
    }
  }

  // npm install
  const spinner2 = ora('Installing frontend dependencies').start();
  try {
    execSync('npm install --production', { cwd: uiDir, stdio: 'pipe' });
    spinner2.succeed('Frontend dependencies installed');
  } catch (err) {
    spinner2.fail('Failed to install frontend dependencies');
    console.log(chalk.dim(`  Run manually: cd ${uiDir} && npm install`));
  }

  // Create data directory
  const dataDir = path.join(uiDir, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  for (const f of ['chat_sessions.json', 'action_requests.json']) {
    const fp = path.join(dataDir, f);
    if (!fs.existsSync(fp)) fs.writeFileSync(fp, '[]');
  }
  const kbDeps = path.join(dataDir, 'kb_dependencies.json');
  if (!fs.existsSync(kbDeps)) fs.writeFileSync(kbDeps, '{}');

  // Create playwright downloads dir
  const playwrightDir = expandHome('~/.cache/noetix-playwright');
  fs.mkdirSync(playwrightDir, { recursive: true });
}

// -----------------------------------------------------------------
// Backend installation (Docker)
// -----------------------------------------------------------------

async function installBackend(installDir, config) {
  // Write docker-compose.yml
  const spinner = ora('Setting up backend').start();

  const composeTemplate = readTemplate('docker-compose.yml');
  const compose = composeTemplate
    .replace(/\$\{BACKEND_PORT:-8001\}/g, config.backendPort || '8001')
    .replace(/\$\{BACKEND_HOST:-0\.0\.0\.0\}/g, config.backendHost || '0.0.0.0');
  fs.writeFileSync(path.join(installDir, 'docker-compose.yml'), compose);

  // Write Dockerfile if not present
  const dockerfileSrc = templatePath('Dockerfile.backend');
  const dockerfileDest = path.join(installDir, 'Dockerfile.backend');
  if (fs.existsSync(dockerfileSrc) && !fs.existsSync(dockerfileDest)) {
    fs.copyFileSync(dockerfileSrc, dockerfileDest);
  }

  // Create backend data directories
  const knowledgeDir = path.join(installDir, 'knowledge');
  for (const d of ['uploads', 'library', 'knowledge_bases', 'data']) {
    fs.mkdirSync(path.join(knowledgeDir, d), { recursive: true });
  }

  spinner.succeed('Backend configuration ready');

  // Offer to build/pull the image now
  if (isDockerInstalled() && isDockerRunning()) {
    const buildNow = await confirm({
      message: 'Build backend Docker image now? (this may take several minutes)',
      default: false,
    });
    if (buildNow) {
      const buildSpinner = ora('Building backend image (this takes a while on first run)').start();
      try {
        execSync(`docker compose -f ${path.join(installDir, 'docker-compose.yml')} build backend`, {
          cwd: installDir, stdio: 'pipe', timeout: 600000,
        });
        buildSpinner.succeed('Backend image built');
      } catch (err) {
        buildSpinner.fail('Image build failed — you can retry with: docker compose build backend');
      }
    } else {
      console.log(chalk.dim('  Build later: docker compose build backend'));
    }
  }
}

// -----------------------------------------------------------------
// Systemd services
// -----------------------------------------------------------------

async function createSystemdServices(installDir, config) {
  const systemdDir = path.join(process.env.HOME || '/root', '.config', 'systemd', 'user');
  fs.mkdirSync(systemdDir, { recursive: true });

  const needsFrontend = config.mode === 'full' || config.mode === 'frontend';
  const needsBackend = config.mode === 'full' || config.mode === 'backend';

  if (needsFrontend) {
    const service = `[Unit]
Description=Noetix UI Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=${path.join(installDir, 'ui')}
ExecStart=/usr/bin/node src/gateway/server.js
Restart=on-failure
RestartSec=3
Environment=NODE_ENV=production

[Install]
WantedBy=default.target
`;
    fs.writeFileSync(path.join(systemdDir, 'noetix-ui.service'), service);
  }

  if (needsBackend) {
    const composePath = path.join(installDir, 'docker-compose.yml');
    const service = `[Unit]
Description=Noetix Knowledge Backend (Docker)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${installDir}
ExecStart=/usr/bin/docker compose -f ${composePath} up
ExecStop=/usr/bin/docker compose -f ${composePath} down
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
`;
    fs.writeFileSync(path.join(systemdDir, 'noetix-knowledge.service'), service);
  }

  console.log(chalk.dim('  Systemd services created. Enable with:'));
  if (needsFrontend) console.log(chalk.dim('    systemctl --user enable --now noetix-ui'));
  if (needsBackend) console.log(chalk.dim('    systemctl --user enable --now noetix-knowledge'));
}
