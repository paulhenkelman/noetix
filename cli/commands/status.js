/**
 * noetix status — show running services and health.
 */

import chalk from 'chalk';
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import http from 'http';

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

function httpCheck(url, timeoutMs = 3000) {
  return new Promise(resolve => {
    const req = http.get(url, { timeout: timeoutMs }, res => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

function checkPid(pidFile) {
  if (!fs.existsSync(pidFile)) return null;
  const pid = parseInt(fs.readFileSync(pidFile, 'utf-8').trim(), 10);
  try {
    process.kill(pid, 0);
    return pid;
  } catch {
    return null;
  }
}

function checkSystemd(unit) {
  try {
    const status = execSync(`systemctl --user is-active ${unit} 2>/dev/null`, { encoding: 'utf-8' }).trim();
    return status === 'active';
  } catch {
    return false;
  }
}

function checkDocker(containerName) {
  try {
    const out = execSync(`docker inspect -f '{{.State.Running}}' ${containerName} 2>/dev/null`, { encoding: 'utf-8' }).trim();
    return out === 'true';
  } catch {
    return false;
  }
}

export async function status() {
  const state = loadState();
  if (!state) {
    console.log(chalk.red('No noetix installation found. Run `noetix init` first.'));
    process.exit(1);
  }

  const installDir = state.installDir;
  const mode = state.mode;
  const hasFrontend = mode === 'full' || mode === 'frontend';
  const hasBackend = mode === 'full' || mode === 'backend';

  console.log('');
  console.log(chalk.bold('  Noetix Status'));
  console.log(chalk.dim(`  Mode: ${mode} | Dir: ${installDir}`));
  console.log('');

  if (hasFrontend) {
    const uiPort = state.uiPort || state.gatewayPort || 8788;
    let uiStatus = 'stopped';
    let detail = '';

    const uiService = state.uiServiceName || 'noetix-ui';
    if (checkSystemd(uiService)) {
      uiStatus = 'running';
      detail = '(systemd)';
    } else {
      const pid = checkPid(path.join(installDir, '.noetix-ui.pid'));
      if (pid) {
        uiStatus = 'running';
        detail = `(PID: ${pid})`;
      }
    }

    // Health check
    if (uiStatus === 'running') {
      const healthy = await httpCheck(`http://127.0.0.1:${uiPort}/health`);
      if (healthy) {
        console.log(chalk.green(`  Noetix UI: running ${detail} — http://127.0.0.1:${uiPort}`));
      } else {
        console.log(chalk.yellow(`  Noetix UI: running ${detail} — not responding on port ${uiPort}`));
      }
    } else {
      console.log(chalk.red(`  Noetix UI: stopped`));
    }

    // Vite dev server check
    const vitePort = state.vitePort || 5174;
    const viteUp = await httpCheck(`http://127.0.0.1:${vitePort}/`);
    if (viteUp) {
      console.log(chalk.green(`  Frontend: running — http://127.0.0.1:${vitePort}`));
    } else {
      console.log(chalk.dim(`  Frontend: not running (dev server on port ${vitePort})`));
    }
  }

  if (hasBackend) {
    const backendPort = state.backendPort || 8001;
    let backendStatus = 'stopped';
    let detail = '';

    const beService = state.backendServiceName || 'noetix-knowledge';
    if (checkSystemd(beService)) {
      backendStatus = 'running';
      detail = '(systemd)';
    } else if (checkDocker('noetix-backend')) {
      backendStatus = 'running';
      detail = '(docker)';
    }

    if (backendStatus === 'running') {
      const backendUrl = mode === 'full' ? `http://127.0.0.1:${backendPort}` : (state.backendUrl || `http://127.0.0.1:${backendPort}`);
      const healthy = await httpCheck(`${backendUrl}/health`);
      if (healthy) {
        console.log(chalk.green(`  Backend:  running ${detail} — ${backendUrl}`));
      } else {
        console.log(chalk.yellow(`  Backend:  running ${detail} — not responding`));
      }
    } else {
      console.log(chalk.red(`  Backend:  stopped`));
    }

    // Neo4j check
    if (checkDocker('noetix-neo4j')) {
      console.log(chalk.green(`  Neo4j:    running — http://127.0.0.1:7474`));
    } else {
      console.log(chalk.dim(`  Neo4j:    not running`));
    }
  }

  // Remote backend check (frontend-only mode)
  if (mode === 'frontend' && state.backendUrl) {
    const healthy = await httpCheck(`${state.backendUrl}/health`);
    if (healthy) {
      console.log(chalk.green(`  Backend:  reachable — ${state.backendUrl}`));
    } else {
      console.log(chalk.red(`  Backend:  unreachable — ${state.backendUrl}`));
    }
  }

  console.log('');
}
