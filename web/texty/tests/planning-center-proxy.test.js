import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.js';
const key='a'.repeat(64),env={BACKEND_URL:'https://backend.example.test/private',BACKEND_BRIDGE_KEY:'synthetic-bridge'};
const request=(path,method='GET',headers={},body)=>new Request('https://console.example.test'+path,{method,headers:{Authorization:'Bearer synthetic-admin',...headers},...(body?{body}:{})});

test('held endpoints preserve actual bearer/private bridge and JSON while stripping cookies',async()=>{
  const saved=globalThis.fetch,calls=[];globalThis.fetch=async(url,options)=>{calls.push({url:String(url),options,body:options.body?await new Response(options.body).text():undefined});return Response.json({execution_enabled:false});};
  try{
    for(const [path,method,body] of [['/api/planning-center/held-previews','POST','{"volunteer_id":7}'],[`/api/planning-center/frequency-reviews/${key}`,'GET',undefined],[`/api/planning-center/frequency-reviews/${key}`,'POST','{"preview_hash":"synthetic-exact-hashes"}']]){
      const response=await worker.fetch(request(path,method,{'Content-Type':'application/json',Cookie:'private-cookie','X-Texty-Bridge':'forged'},body),env);
      assert.equal(response.status,200);assert.equal(response.headers.get('cache-control'),'no-store');const call=calls.at(-1);
      assert.equal(call.url,'https://backend.example.test'+path);assert.equal(call.body,body);assert.equal(call.options.headers.get('authorization'),'Bearer synthetic-admin');
      assert.equal(call.options.headers.get('cookie'),null);assert.equal(call.options.headers.get('x-texty-bridge'),'synthetic-bridge');assert.equal(call.options.redirect,'manual');
    }
  }finally{globalThis.fetch=saved;}
});

test('execute, path/method aliases, queries, missing bearer, non-JSON and cross-origin requests never forward',async()=>{
  const saved=globalThis.fetch;let calls=0;globalThis.fetch=async()=>{calls++;return Response.json({});};
  try{
    for(const path of ['/api/planning-center/execute','/api/planning-center/held-previews/execute','/api/planning-center/held-previews?actor=forged',`/api/planning-center/frequency-reviews/${key}/execute`,'/api/planning-center%2Fexecute','/api/%70lanning-center/execute','/api/planning-center%252Fexecute'])assert.equal((await worker.fetch(request(path,'POST',{'Content-Type':'application/json'},'{}'),env)).status,404);
    assert.equal((await worker.fetch(request('/api/planning-center/held-previews'),env)).status,404);
    assert.equal((await worker.fetch(request(`/api/planning-center/frequency-reviews/${key}`,'DELETE'),env)).status,404);
    assert.equal((await worker.fetch(request('/api/planning-center/held-previews','POST',{Authorization:'','Content-Type':'application/json'},'{}'),env)).status,401);
    assert.equal((await worker.fetch(request('/api/planning-center/held-previews','POST',{'Content-Type':'text/plain'},'{}'),env)).status,415);
    assert.equal((await worker.fetch(request('/api/planning-center/held-previews','POST',{'Content-Type':'application/json',Origin:'https://other.example.test'},'{}'),env)).status,403);
    assert.equal(calls,0);
  }finally{globalThis.fetch=saved;}
});

test('multiply encoded or malformed API namespaces never fall through to the proxy',async()=>{
  const saved=globalThis.fetch;let calls=0;globalThis.fetch=async()=>{calls++;return Response.json({});};
  try{
    for(const path of ['/api/%2570lanning-center/execute','/api/planning%252dcenter/held-previews',
      '/api/%252570lanning-center/execute','/api/planning%25252dcenter/held-previews',
      '/api/planning-center%252Fexecute','/api/%2Fplanning-center/execute',
      '/api/%252Fplanning-center/execute','/api/planning%center/execute',
      '/api/planning-center%/execute','/api//planning-center/execute']){
      for(const method of ['GET','POST','PUT','DELETE']){
        const response=await worker.fetch(request(path,method,{Authorization:'','Content-Type':'application/json'}),env);
        assert.equal(response.status,404,path+' '+method);assert.equal(response.headers.get('cache-control'),'no-store');
      }
    }
    assert.equal(calls,0);
  }finally{globalThis.fetch=saved;}
});

test('canonical API namespaces retain encoded resource IDs and query strings',async()=>{
  const saved=globalThis.fetch,calls=[];globalThis.fetch=async(url)=>{calls.push(String(url));return Response.json({});};
  try{
    const path='/api/setup/contacts/%2B15555550100?source=synthetic%20import';
    assert.equal((await worker.fetch(request(path,'DELETE'),env)).status,200);
    assert.deepEqual(calls,['https://backend.example.test'+path]);
  }finally{globalThis.fetch=saved;}
});
