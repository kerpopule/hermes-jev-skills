#!/usr/bin/env node
/** Optional Pomerado companion. No install, minting, credentials or default changes. */
import {readFileSync, lstatSync, readdirSync, realpathSync} from 'node:fs';
import {resolve, join, isAbsolute, dirname, relative} from 'node:path';
import {createHash} from 'node:crypto';
import {spawn,execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

export const WARNING = 'Pomerado runs reviewed code with your account permissions. It is not a sandbox. A read declaration is not an enforced network or filesystem restriction. No minting, sign-in or write workflows are supported by this companion.';
const fail = message => { throw new Error('pomerado_' + message); };
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const safePath = path => typeof path === 'string' && path.length > 0 && !path.includes('\\') && !isAbsolute(path) && path.split('/').every(p => p && p !== '..' && p !== '.');
export function inspectArtifact(directory) {
  if (!isAbsolute(directory) || realpathSync(directory) !== directory) fail('absolute_real_directory_required');
  const files = []; let size = 0; let entries = 0;
  function walk(base, depth = 0) {
    if (depth > 8) fail('artifact_too_deep');
    const names = readdirSync(base);
    entries += names.length;
    if (entries > 256) fail('artifact_bounds');
    for (const name of names.sort()) {
      const path = join(base, name); const info = lstatSync(path);
      if (info.isSymbolicLink()) fail('symlink_denied');
      if (info.isDirectory()) { walk(path, depth + 1); continue; }
      if (!info.isFile() || info.size > 1024 * 1024 || files.length >= 64) fail('artifact_bounds');
      size += info.size; if (size > 2 * 1024 * 1024) fail('artifact_bounds');
      files.push([relative(directory, path).split('\\').join('/'), hash(readFileSync(path))]);
    }
  }
  walk(directory);
  const read = name => JSON.parse(readFileSync(join(directory, name), 'utf8'));
  const metadata = read('pomerado.json'); const deployment = read('deployment.json');
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(deployment.name)) fail('invalid_name');
  if (deployment.request?.effect !== 'read') fail('read_only_companion');
  if (metadata.signIn || deployment.request.authenticationOrigins?.length || metadata.questions && Object.keys(metadata.questions).length || files.some(([name]) => name === 'auth-fill.json')) fail('authentication_or_input_not_supported');
  const url = new URL(deployment.request.url);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.hash) fail('invalid_url');
  if (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) fail('https_required');
  if (!Array.isArray(metadata.files) || !metadata.files.length || !metadata.files.includes(metadata.entrypoint) || new Set(metadata.files).size !== metadata.files.length) fail('invalid_files');
  const known = new Set(files.map(([name]) => name));
  for (const path of metadata.files) if (!safePath(path) || !known.has(path)) fail('invalid_source_path');
  if (!metadata.inputSchema || !metadata.outputSchema) fail('missing_schema');
  return {name:deployment.name, description:String(deployment.description).slice(0,4096), url:url.href, effect:'read', directory, sha256:hash(JSON.stringify(files)), files, warning:WARNING, inputSchema:metadata.inputSchema};
}
export function launchSpec({directory, cli, node, approved, enabled = false, acknowledgeHostCode = false}, sourceEnv = process.env) {
  if (enabled !== true || acknowledgeHostCode !== true) fail('explicit_opt_in_required');
  const artifact = inspectArtifact(directory);
  if (!/^[a-f0-9]{64}$/.test(approved ?? '') || approved !== artifact.sha256) fail('review_changed');
  for (const path of [node, cli]) if (!isAbsolute(path) || !lstatSync(path).isFile()) fail('absolute_runtime_file_required');
  if (!cli.endsWith('/dist/typescript/src/standalone/mcp-cli.js')) fail('unsupported_runtime_entry');
  const version=execFileSync(node,['--version'],{timeout:5000,env:{PATH:dirname(node)},encoding:'utf8'}).trim();
  if(!/^v24\.(?:2[1-9]|[3-9][0-9])\.[0-9]+$/.test(version))fail('node_24_21_or_newer_24x_required');
  const pkg=JSON.parse(readFileSync(resolve(dirname(cli),'../../../../package.json'),'utf8'));
  if (pkg.name !== 'pomerado' || pkg.license !== 'MIT' || pkg.version !== '0.4.0') fail('unsupported_runtime_package');
  const env = {};
  for (const name of ['PATH','HOME','LANG','LC_ALL','TMPDIR','SYSTEMROOT','WINDIR','PLAYWRIGHT_BROWSERS_PATH']) if (typeof sourceEnv[name] === 'string') env[name] = sourceEnv[name];
  // NODE_OPTIONS, provider keys, proxy credentials and broker tokens never propagate.
  return {command:node, args:[cli,'serve','--artifact',directory], options:{cwd:directory,env,stdio:['pipe','pipe','pipe']}, artifact};
}
function flags(args) {
  const result = {}; const bools = new Set(['enabled','acknowledge-host-code']);
  for (let i=0; i<args.length; i++) {
    const key = args[i].replace(/^--/,'');
    if (!args[i].startsWith('--') || !['artifact','cli','node','approved',...bools].includes(key) || key in result) fail('invalid_arguments');
    result[key] = bools.has(key) ? true : args[++i];
    if (result[key] === undefined) fail('missing_argument');
  }
  return {directory:result.artifact, cli:result.cli, node:result.node, approved:result.approved, enabled:result.enabled, acknowledgeHostCode:result['acknowledge-host-code']};
}
export async function main(args = process.argv.slice(2)) {
  const [mode, ...rest] = args; const options = flags(rest);
  if (mode === 'inspect') { console.log(JSON.stringify(inspectArtifact(options.directory),null,2)); return; }
  if (!['config','serve'].includes(mode)) fail('choose_inspect_config_or_serve');
  const spec = launchSpec(options);
  if (mode === 'config') {
    console.log(JSON.stringify({mcp_servers:{['pomerado_'+spec.artifact.name]:{command:options.node,args:[fileURLToPath(import.meta.url),'serve',...rest],sampling:{enabled:false},timeout:120,connect_timeout:30}}},null,2)); return;
  }
  const child = spawn(spec.command,spec.args,spec.options);
  process.stdin.pipe(child.stdin); child.stdout.pipe(process.stdout); child.stderr.pipe(process.stderr);
  let timer;
  const stop = () => { child.kill('SIGTERM'); timer ??= setTimeout(() => child.kill('SIGKILL'),5000); timer.unref(); };
  process.on('SIGTERM',stop); process.on('SIGINT',stop); process.stdin.on('end',stop);
  child.on('error',() => { process.stderr.write('pomerado_runtime_unavailable\n'); process.exitCode=1; });
  child.on('exit',(code) => { clearTimeout(timer); process.stdin.unpipe(child.stdin); process.stdin.pause(); process.exitCode=code ?? 1; });
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(e => { process.stderr.write(e.message+'\n');process.exitCode=1; });
