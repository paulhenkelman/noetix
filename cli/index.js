#!/usr/bin/env node

import { Command } from 'commander';
import { init } from './commands/init.js';
import { start } from './commands/start.js';
import { stop } from './commands/stop.js';
import { status } from './commands/status.js';

const program = new Command();

program
  .name('noetix')
  .description('AI-powered knowledge platform')
  .version('0.1.0');

program
  .command('init')
  .description('Interactive setup — choose full, frontend-only, or backend-only installation')
  .option('-d, --dir <path>', 'Installation directory', '.')
  .option('-m, --mode <mode>', 'Installation mode: full, frontend, backend')
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
