/**
 * noetix stop — stop services.
 */

import chalk from 'chalk';
import ora from 'ora';
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

function loadState() {
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

export async function stop(service) {
  const state = loadState();
  if (!state) {
    console.log(chalk.red('No noetix installation found. Run `noetix init` first.'));
    process.exit(1);
  }

  const installDir = state.installDir;
  const mode = state.mode;
  const stopFrontend = !service || service === 'frontend' || service === 'all';
  const stopBackend = !service || service === 'backend' || service === 'all';

  const hasFrontend = mode === 'full' || mode === 'frontend';
  const hasBackend = mode === 'full' || mode === 'backend';

  if (stopFrontend && hasFrontend) {
    await stopGateway(installDir, state);
  }

  if (stopBackend && hasBackend) {
    await stopBackendService(installDir, state);
  }
}

async function stopGateway(installDir, state) {
  // Try systemd first
  const uiService = state?.uiServiceName || 'noetix-ui';
  try {
    const svcStatus = execSync(`systemctl --user is-active ${uiService} 2>/dev/null`, { encoding: 'utf-8' }).trim();
    if (svcStatus === 'active') {
      const spinner = ora(`Stopping gateway (systemd: ${uiService})`).start();
      execSync(`systemctl --user stop ${uiService}`, { stdio: 'pipe' });
      spinner.succeed('Gateway stopped');
      return;
    }
  } catch { /* not systemd managed */ }

  // Try PID file
  const pidFile = path.join(installDir, '.noetix-gateway.pid');
  if (fs.existsSync(pidFile)) {
    const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim(), 10);
    const spinner = ora('Stopping gateway').start();
    try {
      process.kill(pid, 'SIGTERM');
      // Wait briefly for graceful shutdown
      await new Promise(resolve => setTimeout(resolve, 1000));
      try { process.kill(pid, 0); process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
      spinner.succeed('Gateway stopped');
    } catch {
      spinner.info('Gateway was not running');
    }
    fs.unlinkSync(pidFile);
  } else {
    console.log(chalk.dim('  Gateway: no PID file found (not running or managed externally)'));
  }
}

async function stopBackendService(installDir, state) {
  const composePath = path.join(installDir, 'docker-compose.yml');

  // Try systemd first
  const beService = state?.backendServiceName || 'noetix-knowledge';
  try {
    const svcStatus = execSync(`systemctl --user is-active ${beService} 2>/dev/null`, { encoding: 'utf-8' }).trim();
    if (svcStatus === 'active') {
      const spinner = ora(`Stopping backend (systemd: ${beService})`).start();
      execSync(`systemctl --user stop ${beService}`, { stdio: 'pipe' });
      spinner.succeed('Backend stopped');
      return;
    }
  } catch { /* not systemd managed */ }

  if (!fs.existsSync(composePath)) {
    console.log(chalk.dim('  Backend: no docker-compose.yml found'));
    return;
  }

  const spinner = ora('Stopping backend').start();
  try {
    execSync(`docker compose -f ${composePath} down`, {
      cwd: installDir,
      stdio: 'pipe',
      timeout: 30000,
    });
    spinner.succeed('Backend stopped');
  } catch (err) {
    spinner.fail('Failed to stop backend');
    console.log(chalk.dim(err.stderr?.toString() || err.message));
  }
}
