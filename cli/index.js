#!/usr/bin/env node

import { Command } from 'commander';
import { init } from './commands/init.js';
import { start } from './commands/start.js';
import { stop } from './commands/stop.js';
import { status } from './commands/status.js';

const program = new Command();

program
  .name('noetix')
  .description('Knowledge platform — document management, content processing, and MCP tool services')
  .version('0.2.0');

program
  .command('init')
  .description('Interactive setup — choose full, back-end, or agent installation')
  .option('-d, --dir <path>', 'Installation directory', '.')
  .option('-m, --mode <mode>', 'Installation mode: full, backend, agent (or legacy: frontend)')
  .option('-y, --yes', 'Accept all defaults (non-interactive)')
  .option('--backend-url <url>', 'Backend URL (agent or frontend mode)')
  .option('--backend-deploy <method>', 'Backend deployment: native or docker', 'native')
  .option('-p, --port <port>', 'Noetix UI port')
  .option('--backend-port <port>', 'Backend port')
  .option('--vite-port <port>', 'Frontend dev port')
  .action(init);

program
  .command('start [service]')
  .description('Start services (frontend, backend, or all)')
  .action(start);

program
  .command('stop [service]')
  .description('Stop services (frontend, backend, or all)')
  .action(stop);

program
  .command('status')
  .description('Show running services and health')
  .action(status);

program.parse();
