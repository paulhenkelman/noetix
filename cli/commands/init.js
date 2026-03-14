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
  installCodex, launchCodexLogin, loginCodexWithApiKey,
  isDockerInstalled, isDockerRunning, hasNvidiaGpu,
  isFfmpegInstalled, isNodeVersionOk, isPythonInstalled, getPythonVersion,
  isPortInUse, findAvailablePort,
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

function safeReadJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  } catch { return null; }
}

export async function init(options) {
  const auto = !!options.yes;

  console.log('');
  console.log(chalk.bold('  Noetix Setup'));
  console.log(chalk.dim('  AI-powered knowledge platform'));
  console.log('');

  // --- Check for existing installation ---
  let installDir = path.resolve(options.dir || '.');
  const existingState = safeReadJson(path.join(installDir, '.noetix-state.json'));

  if (existingState) {
    console.log(chalk.yellow(`  Existing deployment detected in ${installDir}`));
    console.log(chalk.dim(`    Mode: ${existingState.mode} | Installed: ${existingState.installedAt || 'unknown'}`));
    console.log('');

    const action = auto ? 'update' : await select({
      message: 'What would you like to do?',
      choices: [
        { name: 'Update existing installation', value: 'update' },
        { name: 'Create alternate installation (side-by-side)', value: 'alternate' },
        { name: 'Cancel', value: 'cancel' },
      ],
    });

    if (action === 'cancel') {
      console.log(chalk.dim('  Cancelled.'));
      process.exit(0);
    }

    if (action === 'alternate') {
      let suffix = 2;
      let altDir = `${installDir}-${suffix}`;
      while (fs.existsSync(altDir)) {
        suffix++;
        altDir = `${installDir}-${suffix}`;
      }
      installDir = auto ? altDir : await input({
        message: 'Alternate installation directory',
        default: altDir,
      });
      installDir = path.resolve(installDir);
    }
    console.log('');
  }

  // --- Mode selection ---
  const mode = options.mode || (auto ? 'full' : await select({
    message: 'Installation mode',
    choices: [
      { name: 'Full installation      — Frontend + Gateway + Backend (single machine)', value: 'full' },
      { name: 'Frontend only          — UI + Gateway (connects to remote backend)', value: 'frontend' },
      { name: 'Backend only           — Knowledge backend (GPU server)', value: 'backend' },
    ],
  }));

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
    await checkCodex(auto);
  }

  if (needsBackend) {
    await checkBackendPrereqs();
  }

  // =================================================================
  // Port conflict detection & configuration prompts
  // =================================================================

  const config = { mode };

  if (needsFrontend) {
    const defaultGw = findAvailablePort(Number(options.gatewayPort) || 8788, 10);
    if (defaultGw !== 8788 && !options.gatewayPort) {
      console.log(chalk.yellow(`  Port 8788 is in use — suggesting ${defaultGw}`));
    }
    config.gatewayPort = options.gatewayPort || (auto ? String(defaultGw) : await input({ message: 'Gateway port', default: String(defaultGw) }));

    const defaultVite = findAvailablePort(Number(options.vitePort) || 5174, 1);
    if (defaultVite !== 5174 && !options.vitePort) {
      console.log(chalk.yellow(`  Port 5174 is in use — suggesting ${defaultVite}`));
    }
    config.vitePort = options.vitePort || (auto ? String(defaultVite) : await input({ message: 'Frontend dev port', default: String(defaultVite) }));
  }

  if (mode === 'frontend') {
    config.backendUrl = options.backendUrl || (auto ? 'http://10.0.0.50:8001' : await input({
      message: 'Backend URL (where the knowledge server is running)',
      default: 'http://10.0.0.50:8001',
    }));
    if (!/^https?:\/\//.test(config.backendUrl)) {
      config.backendUrl = `http://${config.backendUrl}`;
    }
    config.socksProxy = auto ? '' : await input({
      message: 'SOCKS proxy (leave empty if not needed)',
      default: '',
    });
  }

  if (needsBackend) {
    const defaultBe = findAvailablePort(Number(options.backendPort) || 8001, 10);
    if (defaultBe !== 8001 && !options.backendPort) {
      console.log(chalk.yellow(`  Port 8001 is in use — suggesting ${defaultBe}`));
    }
    config.backendPort = options.backendPort || (auto ? String(defaultBe) : await input({ message: 'Backend port', default: String(defaultBe) }));
    config.backendHost = auto ? '0.0.0.0' : await input({ message: 'Backend listen host', default: '0.0.0.0' });
    config.openaiKey = auto ? '' : await password({
      message: 'OpenAI API key (or press Enter to configure later)',
      mask: '*',
    });
  }

  if (mode === 'full') {
    config.backendUrl = `http://127.0.0.1:${config.backendPort}`;
    config.socksProxy = '';
  }

  // Final port conflict warning for chosen ports
  const chosenPorts = [config.gatewayPort, config.vitePort, config.backendPort].filter(Boolean);
  const conflicts = chosenPorts.filter(p => isPortInUse(Number(p)));
  if (conflicts.length > 0) {
    console.log(chalk.red(`  Warning: port(s) ${conflicts.join(', ')} are currently in use.`));
    if (!auto) {
      const proceed = await confirm({ message: 'Continue anyway?', default: false });
      if (!proceed) process.exit(1);
    }
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
    await installBackend(installDir, config, auto, options.backendDeploy);
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
    backendDeploy: config.backendDeploy,
    uiServiceName: config.uiServiceName,
    backendServiceName: config.backendServiceName,
    installedAt: new Date().toISOString(),
  };
  fs.writeFileSync(path.join(installDir, '.noetix-state.json'), JSON.stringify(state, null, 2));

  // =================================================================
  // Auto-start services
  // =================================================================

  if (process.platform === 'linux' && config.uiServiceName) {
    const startNow = auto || await confirm({
      message: 'Start services now?',
      default: true,
    });
    if (startNow) {
      try {
        execSync('systemctl --user daemon-reload', { stdio: 'pipe' });
        if (needsFrontend && config.uiServiceName) {
          execSync(`systemctl --user enable --now ${config.uiServiceName}`, { stdio: 'pipe' });
          console.log(chalk.green(`  ${config.uiServiceName} started`));
        }
        if (needsBackend && config.backendServiceName) {
          execSync(`systemctl --user enable --now ${config.backendServiceName}`, { stdio: 'pipe' });
          console.log(chalk.green(`  ${config.backendServiceName} started`));
        }
      } catch (err) {
        console.log(chalk.yellow('  Failed to start services automatically'));
        console.log(chalk.dim(`  Start manually: systemctl --user enable --now ${config.uiServiceName || ''} ${config.backendServiceName || ''}`));
      }
    }
  }
}

// -----------------------------------------------------------------
// Codex prerequisite check
// -----------------------------------------------------------------

async function checkCodex(auto = false) {
  console.log(chalk.dim('Checking prerequisites...'));

  if (!isCodexInstalled()) {
    console.log(chalk.yellow('  Codex CLI is not installed.'));
    const doInstall = auto || await confirm({
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
        if (!auto) {
          const proceed = await confirm({ message: 'Continue without Codex?', default: false });
          if (!proceed) process.exit(1);
        }
      }
    } else {
      console.log(chalk.dim('  Skipping Codex install. Install later: npm install -g @openai/codex'));
    }
  } else {
    console.log(chalk.green(`  Codex CLI: ${getCodexVersion()}`));
  }

  // Check login
  if (isCodexInstalled() && !isCodexLoggedIn()) {
    if (auto) {
      console.log(chalk.yellow('  Codex is not logged in. Run `codex login` to authenticate.'));
    } else {
    console.log(chalk.yellow('  Codex is not logged in.'));
    const loginMethod = await select({
      message: 'How would you like to authenticate Codex?',
      choices: [
        { name: 'Browser login (opens browser for OAuth)', value: 'browser' },
        { name: 'API key (enter your OpenAI API key)', value: 'apikey' },
        { name: 'Skip (configure later)', value: 'skip' },
      ],
    });
    if (loginMethod === 'browser') {
      console.log(chalk.dim('  Opening browser for Codex authentication...'));
      launchCodexLogin();
      if (isCodexLoggedIn()) {
        console.log(chalk.green('  Codex login successful'));
      } else {
        console.log(chalk.yellow('  Codex login may not have completed. You can retry with: codex login'));
      }
    } else if (loginMethod === 'apikey') {
      const apiKey = await password({
        message: 'OpenAI API key',
        mask: '*',
      });
      if (apiKey) {
        loginCodexWithApiKey(apiKey);
        if (isCodexLoggedIn()) {
          console.log(chalk.green('  Codex login successful'));
        } else {
          console.log(chalk.yellow('  Codex login may not have completed. You can retry with: codex login --with-api-key'));
        }
      }
    } else {
      console.log(chalk.dim('  Log in later: codex login'));
    }
    } // end else (not auto)
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

  if (isPythonInstalled()) {
    console.log(chalk.green(`  Python: ${getPythonVersion()}`));
  } else {
    console.log(chalk.yellow('  Python 3: not found (needed for native backend)'));
  }

  if (isDockerInstalled()) {
    if (isDockerRunning()) {
      console.log(chalk.green('  Docker: running'));
    } else {
      console.log(chalk.yellow('  Docker: installed but not running'));
    }
  } else {
    console.log(chalk.dim('  Docker: not installed (needed for containerized backend)'));
  }

  if (hasNvidiaGpu()) {
    console.log(chalk.green('  NVIDIA GPU: detected'));
  } else {
    console.log(chalk.yellow('  NVIDIA GPU: not detected — TTS/STT/OCR will use CPU (much slower)'));
  }

  if (isFfmpegInstalled()) {
    console.log(chalk.green('  ffmpeg: installed'));
  } else {
    console.log(chalk.yellow('  ffmpeg: not found'));
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

  // npm install (include devDeps for vite build)
  const spinner2 = ora('Installing frontend dependencies').start();
  try {
    execSync('npm install', { cwd: uiDir, stdio: 'pipe' });
    spinner2.succeed('Frontend dependencies installed');
  } catch (err) {
    spinner2.fail('Failed to install frontend dependencies');
    console.log(chalk.dim(`  Run manually: cd ${uiDir} && npm install`));
    return;
  }

  // Build frontend (vite)
  const buildSpinner = ora('Building frontend').start();
  try {
    execSync('npm run build', { cwd: uiDir, stdio: 'pipe' });
    buildSpinner.succeed('Frontend built');
  } catch (err) {
    buildSpinner.fail('Frontend build failed');
    console.log(chalk.dim(`  Run manually: cd ${uiDir} && npm run build`));
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
// Backend installation
// -----------------------------------------------------------------

async function installBackend(installDir, config, auto = false, deployOption) {
  const knowledgeDir = path.join(installDir, 'knowledge');

  // Copy knowledge/ source from CLI package if not present
  if (!fs.existsSync(path.join(knowledgeDir, 'server.py'))) {
    const spinner = ora('Setting up backend source').start();
    const cliKnowledgeDir = path.join(CLI_ROOT, 'knowledge');
    if (fs.existsSync(cliKnowledgeDir)) {
      fs.cpSync(cliKnowledgeDir, knowledgeDir, {
        recursive: true,
        filter: (src) => !src.includes('.venv') && !src.includes('__pycache__'),
      });
      spinner.succeed('Backend source files installed');
    } else {
      spinner.warn('Backend source not found in CLI package — skipping copy');
    }
  }

  // Create data directories
  for (const d of ['uploads', 'library', 'knowledge_bases', 'data']) {
    fs.mkdirSync(path.join(knowledgeDir, d), { recursive: true });
  }

  // Choose deployment method
  const deployMethod = deployOption || (auto ? 'native' : await select({
    message: 'Backend deployment method',
    choices: [
      { name: 'Native (Python venv — recommended for GPU servers)', value: 'native' },
      { name: 'Docker (containerized with NVIDIA GPU support)', value: 'docker' },
    ],
  }));

  config.backendDeploy = deployMethod;

  if (deployMethod === 'native') {
    await installBackendNative(installDir, config, knowledgeDir);
  } else {
    await installBackendDocker(installDir, config);
  }
}

async function installBackendNative(installDir, config, knowledgeDir) {
  const venvDir = path.join(knowledgeDir, '.venv');

  // Create venv
  if (!fs.existsSync(venvDir)) {
    const spinner = ora('Creating Python virtual environment').start();
    try {
      execSync(`python3 -m venv ${venvDir}`, { stdio: 'pipe', timeout: 30000 });
      spinner.succeed('Python venv created');
    } catch (err) {
      spinner.fail('Failed to create venv');
      console.log(chalk.dim(`  Run manually: python3 -m venv ${venvDir}`));
      return;
    }
  }

  // Upgrade pip
  const pip = path.join(venvDir, 'bin', 'pip');
  const spinner2 = ora('Upgrading pip').start();
  try {
    execSync(`${pip} install --upgrade pip`, { stdio: 'pipe', timeout: 60000 });
    spinner2.succeed('pip upgraded');
  } catch {
    spinner2.warn('pip upgrade failed (continuing)');
  }

  // Install deps
  const spinner3 = ora('Installing Python dependencies (this may take several minutes)').start();
  try {
    execSync(`${pip} install -e "."`, { cwd: knowledgeDir, stdio: 'pipe', timeout: 600000 });
    spinner3.succeed('Python dependencies installed');
  } catch (err) {
    spinner3.fail('Failed to install Python dependencies');
    console.log(chalk.dim(`  Run manually: cd ${knowledgeDir} && ${pip} install -e "."`));
  }

  // Copy .env.example if .env doesn't exist
  const envExample = path.join(knowledgeDir, '.env.example');
  const envFile = path.join(knowledgeDir, '.env');
  if (fs.existsSync(envExample) && !fs.existsSync(envFile)) {
    fs.copyFileSync(envExample, envFile);
  }
}

async function installBackendDocker(installDir, config) {
  const spinner = ora('Setting up Docker backend').start();

  const composeTemplate = readTemplate('docker-compose.yml');
  const compose = composeTemplate
    .replace(/\$\{BACKEND_PORT:-8001\}/g, config.backendPort || '8001')
    .replace(/\$\{BACKEND_HOST:-0\.0\.0\.0\}/g, config.backendHost || '0.0.0.0');
  fs.writeFileSync(path.join(installDir, 'docker-compose.yml'), compose);

  const dockerfileSrc = templatePath('Dockerfile.backend');
  const dockerfileDest = path.join(installDir, 'Dockerfile.backend');
  if (fs.existsSync(dockerfileSrc) && !fs.existsSync(dockerfileDest)) {
    fs.copyFileSync(dockerfileSrc, dockerfileDest);
  }

  spinner.succeed('Docker configuration ready');

  if (isDockerInstalled() && isDockerRunning()) {
    const buildNow = auto || await confirm({
      message: 'Build backend Docker image now? (this may take several minutes)',
      default: false,
    });
    if (buildNow) {
      const buildSpinner = ora('Building backend image').start();
      try {
        execSync(`docker compose -f ${path.join(installDir, 'docker-compose.yml')} build backend`, {
          cwd: installDir, stdio: 'pipe', timeout: 600000,
        });
        buildSpinner.succeed('Backend image built');
      } catch (err) {
        buildSpinner.fail('Image build failed — retry with: docker compose build backend');
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

  // Derive instance suffix from directory name for multi-instance support
  const dirName = path.basename(installDir);
  const instanceSuffix = dirName === 'noetix' ? '' : `-${dirName.replace(/[^a-zA-Z0-9-]/g, '-')}`;
  const uiServiceName = `noetix-ui${instanceSuffix}`;
  const backendServiceName = `noetix-knowledge${instanceSuffix}`;

  if (needsFrontend) {
    const service = `[Unit]
Description=Noetix UI Gateway (${dirName})
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
    fs.writeFileSync(path.join(systemdDir, `${uiServiceName}.service`), service);
  }

  if (needsBackend) {
    let service;
    if (config.backendDeploy === 'native') {
      const venvBin = path.join(installDir, 'knowledge', '.venv', 'bin');
      const beHost = config.backendHost || '0.0.0.0';
      const bePort = config.backendPort || '8001';
      service = `[Unit]
Description=Noetix Knowledge Backend (${dirName})
After=network.target

[Service]
Type=simple
WorkingDirectory=${path.join(installDir, 'knowledge')}
ExecStart=${path.join(venvBin, 'uvicorn')} server:app --host ${beHost} --port ${bePort}
Restart=on-failure
RestartSec=3
Environment=PYTHONPATH=${path.join(installDir, 'knowledge')}

[Install]
WantedBy=default.target
`;
    } else {
      const composePath = path.join(installDir, 'docker-compose.yml');
      service = `[Unit]
Description=Noetix Knowledge Backend (${dirName})
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
    }
    fs.writeFileSync(path.join(systemdDir, `${backendServiceName}.service`), service);
  }

  // Store service names in config for start/stop/status
  config.uiServiceName = uiServiceName;
  config.backendServiceName = backendServiceName;

  console.log(chalk.dim('  Systemd services created. Enable with:'));
  if (needsFrontend) console.log(chalk.dim(`    systemctl --user enable --now ${uiServiceName}`));
  if (needsBackend) console.log(chalk.dim(`    systemctl --user enable --now ${backendServiceName}`));
}
