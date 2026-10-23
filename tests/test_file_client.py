"""Exercise browser transfer behavior with a small DOM/fetch harness."""
import shutil
import subprocess

import pytest
from app.file_routes import CLIENT


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is needed for browser-client behavior checks')
def test_browser_transfer_validation_and_ngrok_bypass():
    runner = r'''
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {createHash, webcrypto} = require('node:crypto');
const client = fs.readFileSync(0, 'utf8');
const sql = '-- Standard MCP full project SQL v1\nCREATE TABLE data_example(id TEXT);\n';
const html = '<!DOCTYPE html><html>ERR_NGROK_6024</html>';
const hash = text => createHash('sha256').update(text).digest('hex');
async function scenario(mode, body, headers, expectedDownload, expectedMessage) {
  let complete;
  const done = new Promise(resolve => {complete=resolve;});
  let message='';
  const status={};
  Object.defineProperty(status,'textContent',{get(){return message;},set(value){message=value;complete();}});
  const links=[];
  const form={hidden:true};
  const calls=[];
  const document={
    body:{dataset:{mode},appendChild(link){links.push(link);}},
    getElementById(id){return {status,upload:form,file:{files:[{name:'project.sql',size:100}]}}[id];},
    createElement(){return {clicked:false,click(){this.clicked=true;}};}
  };
  const context={document,location:{hash:'#private-transfer-token',pathname:'/files/'+mode},history:{replaceState(){}},
    fetch:async (url,options)=>{calls.push({url,options});return {ok:true,headers:new Headers(headers),blob:async()=>new Blob([body]),json:async()=>JSON.parse(body)};},
    URL:{createObjectURL(){return 'blob:local-test';}},crypto:webcrypto};
  vm.runInNewContext(client,context);
  if(mode==='upload') await form.onsubmit({preventDefault(){}});
  const timeout = setTimeout(()=>complete(),1000);
  await done;
  clearTimeout(timeout);
  assert.ok(message.includes(expectedMessage),message);
  assert.equal(links.length,expectedDownload?1:0);
  if(expectedDownload){assert.equal(links[0].download,'finance.sql');assert.ok(links[0].clicked);}
  assert.equal(calls[0].options.headers['ngrok-skip-browser-warning'],'true');
  assert.equal(calls[0].options.headers['X-Transfer-Token'],'private-transfer-token');
}
(async()=>{
 const valid={'Content-Type':'application/sql','Content-Disposition':'attachment; filename="finance.sql"','X-Content-SHA256':hash(sql)};
 await scenario('download',sql,valid,true,'download is ready');
 await scenario('download',html,{'Content-Type':'text/html'},false,'tunnel returned HTML');
 await scenario('download',html,{...valid,'X-Content-SHA256':hash(html)},false,'not a supported project SQL');
 await scenario('download',sql,{...valid,'X-Content-SHA256':'0'.repeat(64)},false,'checksum verification failed');
 await scenario('upload','{"import_id":"example"}',{'Content-Type':'application/json'},false,'Uploaded.');
 await scenario('upload',html,{'Content-Type':'text/html'},false,'unexpected page');
 console.log('6 browser transfer scenarios passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([shutil.which('node'), '-e', runner], input=CLIENT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert '6 browser transfer scenarios passed' in result.stdout
