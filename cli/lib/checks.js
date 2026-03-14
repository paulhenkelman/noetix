/**
 * Environment checks — codex, docker, node, system tools.
 */

import { execSync, spawnSync } from 'child_process';

export function commandExists(cmd) {
  try {
    execSync(`command -v ${cmd}`, { stdio: 'ignore' });
    return true;
  } catch { return false; }
}

export function getCommandVersion(cmd, flag = '--version') {
  try {
    return execSync(`${cmd} ${flag} 2>&1`, { encoding: 'utf-8' }).trim();
  } catch { return null; }
}

// --- Codex ---

export function isCodexInstalled() {
  return commandExists('codex');
}

export function getCodexVersion() {
  return getCommandVersion('codex');
}

export function isCodexLoggedIn() {
  try {
    const out = execSync('codex login status 2>&1', { encoding: 'utf-8' }).trim();
    return /logged in/i.test(out);
  } catch { return false; }
}

export function installCodex() {
  execSync('npm install -g @openai/codex', { stdio: 'inherit' });
}

export function launchCodexLogin() {
  // codex login opens the browser for OAuth — must be interactive
  spawnSync('codex', ['login'], { stdio: 'inherit' });
}

export function loginCodexWithApiKey(apiKey) {
  spawnSync('codex', ['login', '--with-api-key'], {
    stdio: ['pipe', 'inherit', 'inherit'],
    input: apiKey + '\n',
  });
}

// --- Docker ---

export function isDockerInstalled() {
  return commandExists('docker');
}

export function isDockerRunning() {
  try {
    execSync('docker info', { stdio: 'ignore', timeout: 5000 });
    return true;
  } catch { return false; }
}

export function hasNvidiaDocker() {
  try {
    execSync('docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi', {
      stdio: 'ignore', timeout: 30000
    });
    return true;
  } catch { return false; }
}

export function hasNvidiaGpu() {
  return commandExists('nvidia-smi');
}

// --- System tools ---

export function isNodeVersionOk() {
  const major = parseInt(process.versions.node.split('.')[0], 10);
  return major >= 18;
}

export function isFfmpegInstalled() {
  return commandExists('ffmpeg');
}

export function isPythonInstalled() {
  return commandExists('python3');
}

export function getPythonVersion() {
  return getCommandVersion('python3');
}

// --- Port checks ---

export function isPortInUse(port) {
  try {
    const out = execSync(
      `ss -tlnH sport = :${port} 2>/dev/null || netstat -tln 2>/dev/null | grep ':${port} '`,
      { encoding: 'utf-8', timeout: 3000 }
    ).trim();
    return out.length > 0;
  } catch { return false; }
}

export function findAvailablePort(preferred, step = 10) {
  let port = preferred;
  const max = preferred + step * 20;
  while (port < max) {
    if (!isPortInUse(port)) return port;
    port += step;
  }
  return preferred; // fall back to preferred if nothing found
}

// --- Existing installations ---

export function findNoetixInstallations(searchDir) {
  const found = [];
  try {
    const entries = require('fs').readdirSync(searchDir, { withFileTypes: true });
    for (const e of entries) {
      if (!e.isDirectory()) continue;
      const stateFile = require('path').join(searchDir, e.name, '.noetix-state.json');
      try {
        const state = JSON.parse(require('fs').readFileSync(stateFile, 'utf-8'));
        found.push({ dir: require('path').join(searchDir, e.name), ...state });
      } catch { /* not a noetix install */ }
    }
  } catch { /* dir doesn't exist */ }
  return found;
}
