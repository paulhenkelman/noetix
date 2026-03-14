import { defineConfig } from 'vite';
import fs from 'fs';
import path from 'path';
import { parse } from 'smol-toml';

const projectRoot = path.resolve(import.meta.dirname, '..');
const uiConfig = parse(fs.readFileSync(path.join(projectRoot, 'ui.config'), 'utf-8'));

export default defineConfig({
  root: 'src/frontend',
  server: {
    port: uiConfig.frontend?.vite_port || 5174,
  },
  define: {
    __NOETIX_API_BASE__: JSON.stringify(uiConfig.frontend?.api_base || 'http://127.0.0.1:8788'),
  },
});
