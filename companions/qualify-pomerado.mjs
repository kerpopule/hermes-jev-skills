// No minting or model requests. Run under your resource owner with a separately reviewed runtime.
import {createServer} from 'node:http';
import {once} from 'node:events';
import {cpSync,mkdtempSync,readFileSync,writeFileSync,realpathSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
import {inspectArtifact,launchSpec} from './pomerado.mjs';
const [runtime, node, output]=process.argv.slice(2);
if(!runtime||!node)throw Error('usage: node companions/qualify-pomerado.mjs /absolute/pomerado /absolute/node24 [receipt.json]');
const {Client}=await import(pathToFileURL(join(runtime,'node_modules/@modelcontextprotocol/client/dist/index.mjs')).href);
const {StdioClientTransport}=await import(pathToFileURL(join(runtime,'node_modules/@modelcontextprotocol/client/dist/stdio.mjs')).href);
const root=realpathSync(mkdtempSync(join(tmpdir(),'pomerado-companion-'))),nonce='pomerado-'+randomUUID(),requests=[];
const server=createServer((req,res)=>{requests.push({method:req.method,url:req.url});res.writeHead(200,{'content-type':'text/html'});res.end('<!doctype html><title>Disposable fixture</title><h1>'+nonce+'</h1>');});
server.listen(0,'127.0.0.1');await once(server,'listening');
let client;
try{
 cpSync(fileURLToPath(new URL('../fixtures/pomerado/read-heading',import.meta.url)),root,{recursive:true});
 const d=JSON.parse(readFileSync(join(root,'deployment.json'),'utf8'));d.request.url='http://127.0.0.1:'+server.address().port+'/';writeFileSync(join(root,'deployment.json'),JSON.stringify(d));
 const review=inspectArtifact(root),options={directory:root,node,cli:join(runtime,'dist/typescript/src/standalone/mcp-cli.js'),approved:review.sha256,enabled:true,acknowledgeHostCode:true};
 const spec=launchSpec(options),transport=new StdioClientTransport({command:node,args:[fileURLToPath(new URL('pomerado.mjs',import.meta.url)),'serve','--artifact',root,'--cli',options.cli,'--node',node,'--approved',review.sha256,'--enabled','--acknowledge-host-code'],env:spec.options.env,stderr:'pipe'});
 client=new Client({name:'pomerado-local-qualification',version:'1.0.0'});await client.connect(transport);
 const tools=await client.listTools();assert.ok(tools.tools.some(t=>t.name==='read_fixture'));assert.ok(!tools.tools.some(t=>t.name==='mint'));
 let answer=await client.callTool({name:'read_fixture',arguments:{input:{}}});let body;
 for(let i=0;i<20;i++){assert.ok(!answer.isError,JSON.stringify(answer));body=answer.structuredContent??JSON.parse(answer.content[0].text);if(body.heading||body.status==='completed')break;assert.ok(body.job_id);answer=await client.callTool({name:'get_job',arguments:{job_id:body.job_id,wait_seconds:1}});}
 assert.equal(body.heading??body.output?.heading,nonce);assert.ok(requests.length);assert.ok(requests.every(r=>r.method==='GET'));
 const receipt={passed:true,checks:['actual-companion-stdio-initialize','saved-read-tool-exposed','mint-not-exposed','real-browser-nonce-roundtrip','GET-only-fixture-requests','client-close-in-finally'],nonce,requests,modelsCalled:false,minted:false,artifactSha256:review.sha256,scope:'hand-authored loopback fixture only; no portable security or model-quality claim'};
 if(output)writeFileSync(resolve(output),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt));
}finally{await client?.close();await new Promise(r=>server.close(r));rmSync(root,{recursive:true});}
