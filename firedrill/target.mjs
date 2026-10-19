import { fileURLToPath } from 'node:url';
import { resolve, join } from 'node:path';
import { ApplicationError, commandMain, invokeCLI, privateDirectory } from './adapter-runtime.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
const cases = new Set(["starter_decision", "starter_payment", "starter_repeat", "busy_portfolio", "busy_subset", "busy_existing_and_due", "ambiguous_late_payment", "ambiguous_window_change", "ambiguous_existing_contact", "boundary_max_revision", "boundary_last_revision", "boundary_unicode_clock"]);
export async function runApplication(input, parameters, signal, invocation) {
  if (Object.keys(input).some(key => !["caseId", "action", "task", "allowWrites", "intent", "today", "renewalIds"].includes(key)) || !cases.has(input.caseId) || !['run', 'inspect'].includes(input.action) ||
      typeof input.task !== 'string' || !input.task.trim() || input.task.length > 8000 || input.task.includes('\0') || typeof input.allowWrites !== 'boolean' ||
      (input.allowWrites ? !input.intent || typeof input.intent !== 'object' || Array.isArray(input.intent) || JSON.stringify(input.intent).length > 8000 : input.intent !== null) ||
      (input.action === 'inspect' && input.allowWrites) || typeof input.today !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(input.today) || !Array.isArray(input.renewalIds) || input.renewalIds.length < 1 || input.renewalIds.length > 128 || !input.renewalIds.every(id => typeof id === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(id))) throw new Error('Supply reviewed explicit task identity/model settings and independent write grant with exact caller intent, or null for read-only.');
  const directory = await privateDirectory(root, invocation), workspace = join(directory, 'workspace');
  const cli = ['uv', 'run', '--locked', '--no-sync'];
  try {
    const initialized = await invokeCLI([...cli, 'python', 'firedrill/case-driver.py', 'init', workspace, input.caseId], { cwd: root, signal });
    const db = initialized.database;
    if (typeof db !== 'string' || !db.startsWith(workspace + '/')) throw new Error('Initialized database is outside the owned workspace.');
    const argv = input.action === 'inspect' ? [...cli, 'python', 'firedrill/case-driver.py', 'inspect', workspace] : [...cli, 'renewal-tick', '--db', db, '--today', input.today, ...input.renewalIds.flatMap(id => ['--renewal-id', id]), '--output', join(directory, 'receipts')];
    if (input.action === 'run') {
      if (input.allowWrites) argv.push('--allow-writes', '--intent-json', JSON.stringify(input.intent));
      if (parameters.model) argv.push('--model', parameters.model);
    }
    return { executionKind: input.action === 'inspect' ? 'application_state_inspection' : 'native_owned_application',
      application: await invokeCLI(argv, { cwd: root, signal }), localState: { directory, workspaceRoot: workspace, databaseFile: db } };
  } catch (error) { if (error instanceof ApplicationError) error.evidence.runDirectory = directory; throw error; }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await commandMain(runApplication);
