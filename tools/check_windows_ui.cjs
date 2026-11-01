'use strict';
// Uses the existing workspace Playwright installation. Never starts a paid session.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../../dyhub/node_modules/playwright-core');

(async () => {
  const root = path.resolve(__dirname, '..');
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const response = await page.goto('http://127.0.0.1:8795/stage', { waitUntil: 'load', timeout: 30000 });
    assert.equal(response.status(), 200);
    await page.waitForFunction(() => document.getElementById('s_llm_model').value.length > 0);
    await page.waitForFunction(() => {
      const edge = document.getElementById('rightPanel').getBoundingClientRect().right;
      return ['drawer', 'songs', 'cmds', 'vpanel', 'refs'].every(id =>
        document.getElementById(id).getBoundingClientRect().left >= edge);
    });
    assert.equal(await page.locator('#btnGo').isVisible(), true);
    await page.locator('#btnDrawer').click();
    assert.equal(await page.locator('#s_vidu_key').getAttribute('type'), 'password');
    assert.equal(await page.locator('#s_vidu_key').inputValue(), '');
    assert.equal(await page.locator('#s_voice option').count() > 0, true);

    // Exercise the actual Windows config write while preserving every setting.
    const save = await page.evaluate(async () => {
      const response = await fetch('/api/settings', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      });
      const result = await response.json();
      return { status: response.status, ok: result.ok, viduConfigured: result.settings.vidu.api_key_set };
    });
    assert.equal(save.status, 200);
    assert.equal(save.ok, true);
    const publicContent = await page.evaluate(async () => {
      const response = await fetch('/api/settings');
      return response.text();
    });
    for (const name of ['deepseek.key', 'breezeblue.key']) {
      const key = fs.readFileSync(path.join(root, 'assets', 'local', name), 'utf8').trim();
      assert.equal(publicContent.includes(key), false, 'Server-side credentials must not appear in public settings');
    }
    await page.locator('#btnDrawerX').click();
    await page.waitForFunction(() => document.getElementById('drawer').getBoundingClientRect().left >=
      document.getElementById('rightPanel').getBoundingClientRect().right);
    await page.screenshot({ path: path.join(root, 'logs', 'windows-stage.png'), fullPage: true, animations: 'disabled' });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({
      pageHttp: response.status(), javascriptErrors: errors.length,
      artcSdkLoaded: await page.evaluate(() => typeof AliRtcEngine !== 'undefined'),
      settingsSave: save.ok, viduConfigured: save.viduConfigured,
      screenshot: path.join(root, 'logs', 'windows-stage.png'),
    }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
