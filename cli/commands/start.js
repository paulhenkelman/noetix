/**
 * noetix start — start services.
 */

import chalk from 'chalk';
import ora from 'ora';
import fs from 'fs';
import path from 'path';
import { execSync, spawn } from 'child_process';
import { isDockerRunning } from '../lib/checks.js';

function loadState() {
  // Search upward from cwd for .noetix-state.json
  let dir = process.cwd();
  while (dir !== path.dirname(dir)) {
    const stateFile = path.join(dir, '.noetix-state.json');
    if (fs.existsSync(stateFile)) {
      return JSON.parse(fs.readFileSync(stateFile, 'utf-8'));
    }
    dir = path.dirname(dir);
  }
  return null;
}

export async function start(service) {
  const state = loadState();
  if (!state) {
    console.log(chalk.red('No noetix installation found. Run `noetix init` first.'));
    process.exit(1);
  }

  const installDir = state.installDir;
  const mode = state.mode;
  const startFrontend = !service || service === 'frontend' || service === 'all';
  const startBackend = !service || service === 'backend' || service === 'all';

  const hasFrontend = mode === 'full' || mode === 'frontend';
  const hasBackend = mode === 'full' || mode === 'backend';

  if (startFrontend && hasFrontend) {
    await startServer(installDir, state);
  }

  if (startBackend && hasBackend) {
    await startBackendService(installDir, state);
  }

  if (startFrontend && !hasFrontend && service === 'frontend') {
    console.log(chalk.yellow('Frontend not installed in this deployment. Re-run `noetix init` with full or frontend mode.'));
  }
  if (startBackend && !hasBackend && service === 'backend') {
    console.log(chalk.yellow('Backend not installed in this deployment. Re-run `noetix init` with full or backend mode.'));
  }
}

async function startServer(installDir, state) {
  const uiDir = path.join(installDir, 'ui');
  const serverPath = path.join(uiDir, 'src', 'server', 'server.js');

  if (!fs.existsSync(serverPath)) {
    console.log(chalk.red(`Noetix UI server not found at ${serverPath}`));
    return;
  }

  // Check if already running via systemd
  const uiService = state.uiServiceName || 'noetix-ui';
  try {
    const svcStatus = execSync(`systemctl --user is-active ${uiService} 2>/dev/null`, { encoding: 'utf-8' }).trim();
    if (svcStatus === 'active') {
      console.log(chalk.green(`Noetix UI already running (systemd: ${uiService})`));
      return;
    }
  } catch { /* not managed by systemd, start manually */ }

  const spinner = ora('Starting noetix-ui').start();

  const child = spawn('node', ['src/server/server.js'], {
    cwd: uiDir,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
    env: { ...process.env, NODE_ENV: 'production' },
  });

  // Write PID for stop command
  const pidFile = path.join(installDir, '.noetix-ui.pid');
  fs.writeFileSync(pidFile, String(child.pid));
  child.unref();

  // Wait briefly for startup
  await new Promise(resolve => setTimeout(resolve, 1500));

  // Check if it's still running
  try {
    process.kill(child.pid, 0);
    spinner.succeed(`Noetix UI started on port ${state.uiPort || 8788} (PID: ${child.pid})`);
  } catch {
    spinner.fail('Noetix UI failed to start — check logs');
    // Show stderr if available
    child.stderr.on('data', d => console.log(chalk.dim(d.toString())));
  }
}

async function startBackendService(installDir, state) {
  const composePath = path.join(installDir, 'docker-compose.yml');

  if (!fs.existsSync(composePath)) {
    console.log(chalk.red(`docker-compose.yml not found at ${composePath}`));
    return;
  }

  // Check if already running via systemd
  const beService = state.backendServiceName || 'noetix-knowledge';
  try {
    const svcStatus = execSync(`systemctl --user is-active ${beService} 2>/dev/null`, { encoding: 'utf-8' }).trim();
    if (svcStatus === 'active') {
      console.log(chalk.green(`Backend already running (systemd: ${beService})`));
      return;
    }
  } catch { /* not managed by systemd */ }

  if (!isDockerRunning()) {
    console.log(chalk.red('Docker is not running. Start Docker first.'));
    return;
  }

  const spinner = ora('Starting backend').start();
  try {
    execSync(`docker compose -f ${composePath} up -d backend`, {
      cwd: installDir,
      stdio: 'pipe',
      timeout: 120000,
    });
    spinner.succeed(`Backend started on port ${state.backendPort || 8001}`);
  } catch (err) {
    spinner.fail('Backend failed to start');
    console.log(chalk.dim(err.stderr?.toString() || err.message));
  }
}
