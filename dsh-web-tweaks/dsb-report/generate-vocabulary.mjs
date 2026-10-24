import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = path.dirname(fileURLToPath(import.meta.url));
const vocabulary = JSON.parse(fs.readFileSync(path.join(dir, '../../agent/report_vocabulary.json'), 'utf8'));
const file = path.join(dir, 'client.js');
const source = fs.readFileSync(file, 'utf8');
const generated = [
  `    const ACTIONS = ${JSON.stringify(vocabulary.actions)};`,
  `    const RESULTS = ${JSON.stringify(vocabulary.results)};`,
  `    const REASONS = ${JSON.stringify(vocabulary.reasons)};`,
  `    const GATE_CODES = new Set(${JSON.stringify(vocabulary.gate_reason_codes)});`,
].join('\n');
const next = source.replace(/    const ACTIONS = [\s\S]*?    const number = /,
  generated + '\n    const number = ');
if (next === source && !source.includes(generated)) throw new Error('client vocabulary marker missing');
if (process.argv.includes('--check')) {
  if (next !== source) throw new Error('client vocabulary differs from report_vocabulary.json');
} else fs.writeFileSync(file, next);
