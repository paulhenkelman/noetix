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
