'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('../../dyhub/node_modules/playwright-core');
(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:900}});
    const rows = [{i:1,role:'gift',text:'🎁 本地测试 送出 小心心 ×2',event_key:'test-combo'}];
    let paidRequests = 0;
    await context.route('**/api/live/start', route => {paidRequests++;return route.abort();});
    await context.route('**/api/avatars', route => route.fulfill({json:{items:[]}}));
    await context.route('**/api/live/status?*', route => {
      const since = Number(new URL(route.request().url()).searchParams.get('since'));
      return route.fulfill({json:{active:false,phase:'idle',danmaku_source:{enabled:false},voice:'test',
        response:{stage:'idle'},transcripts_total:rows.length,new_transcripts:rows.filter(row=>row.i>since)}});
    });
    const page = await context.newPage();
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto('http://127.0.0.1:8795/stage',{waitUntil:'load'});
    await page.waitForFunction(()=>document.querySelector('#trList .gift')?.textContent.includes('×2'));
    rows.push({i:2,role:'gift',text:'🎁 本地测试 送出 小心心 ×5',event_key:'test-combo'});
    await page.waitForFunction(()=>document.querySelector('#trList .gift')?.textContent.includes('×5'));
    assert.equal(await page.locator('#trList .gift').count(),1);
    assert.equal(await page.locator('#trList .gift').textContent(),'🎁 本地测试 送出 小心心 ×5');
    assert.equal(paidRequests,0);
    assert.deepEqual(errors,[]);
    await page.screenshot({path:path.join(__dirname,'../logs/gift-events-ui-check.png'),animations:'disabled'});
    console.log(JSON.stringify({giftVisibleInEvents:true,comboUpdatesSameRow:true,paidRequests,javascriptErrors:errors.length}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
