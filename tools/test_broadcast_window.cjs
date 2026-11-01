'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('../../dyhub/node_modules/playwright-core');

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
    const errors = [];
    let paidRequests = 0;
    await context.route('**/api/live/start', route => {
      paidRequests++;
      return route.abort();
    });
    // Isolate the fixture from any real live session running in the workspace.
    await context.route('**/api/live/status?*', route => route.fulfill({json: {
      active: false, phase: 'idle', danmaku_source: {enabled: false}, voice: 'test',
      response: {stage: 'idle'}, transcripts_total: 0, new_transcripts: [],
    }}));
    await context.route('**/api/avatars', route => route.fulfill({json: {items: []}}));
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
    const controller = await context.newPage();
    await controller.goto('http://127.0.0.1:8795/stage', {waitUntil: 'load', timeout: 30000});
    await controller.waitForFunction(() => document.getElementById('s_llm_model').value.length > 0);
    await controller.evaluate(() => {
      const canvas = document.createElement('canvas');
      canvas.width = 1280; canvas.height = 720;
      const draw = () => {
        const brush = canvas.getContext('2d');
        brush.fillStyle = '#0e3546'; brush.fillRect(0, 0, 1280, 720);
        brush.fillStyle = '#7dd3fc'; brush.font = '48px sans-serif';
        brush.fillText('直播画面窗口 · 本地测试', 310, 330);
        brush.font = '24px sans-serif'; brush.fillStyle = '#fff';
        brush.fillText(new Date().toLocaleTimeString(), 570, 390);
        const caption = document.getElementById('capOverlay');
        caption.innerHTML = '<div class="cap dh">这行字幕会同步到直播画面</div>';
        caption.style.cssText = 'display:block;opacity:1;--cap-off:120px;--cap-font:24px';
      };
      draw();
      window.testCanvas = canvas;
      window.testStream = canvas.captureStream(10);
      window.testDrawTimer = setInterval(draw, 100);
      const video = document.getElementById('video');
      video.srcObject = window.testStream; video.muted = true; video.play();
      document.getElementById('avatarStill').style.display = 'none';
      const caption = document.getElementById('capOverlay');
      caption.innerHTML = '<div class="cap dh">这行字幕会同步到直播画面</div>';
      caption.style.cssText = 'display:block;opacity:1;--cap-off:120px;--cap-font:24px';
    });
    const popupPromise = controller.waitForEvent('popup');
    await controller.locator('#btnBroadcastWindow').click();
    const output = await popupPromise;
    await output.waitForLoadState('load');
    await output.setViewportSize({width: 1280, height: 720});
    const outputApiRequests = [];
    output.on('request', request => { if (request.url().includes('/api/')) outputApiRequests.push(request.url()); });
    await output.waitForFunction(() => {
      const video = document.querySelector('#outputMedia video');
      return video && video.readyState >= 2 && video.currentTime > 0.1;
    });
    await controller.waitForFunction(() => window.stageVideoHealth &&
      window.stageVideoHealth.fps > 2, null, {timeout: 12000});
    const playback = await controller.evaluate(() => window.stageVideoHealth);
    assert.equal(playback.width, 1280);
    assert.equal(playback.height, 720);
    assert.ok(playback.fps < 30, 'Report measured frames instead of assuming display refresh rate');
    assert.equal(await output.title(), '大肥鱼直播画面');
    for (const id of ['top', 'bottom', 'rightPanel', 'mask', 'sessLog', 'confirmBar', 'cmdFx']) {
      assert.equal(await output.locator('#' + id).isVisible(), false, id + ' must stay out of the capture');
    }
    await output.waitForFunction(() => document.getElementById('capOverlay').textContent === '这行字幕会同步到直播画面');
    assert.equal(await output.evaluate(() => document.querySelector('#outputMedia video').srcObject ===
      opener.document.getElementById('video').srcObject), true);
    const count = context.pages().length;
    await controller.locator('#btnBroadcastWindow').click();
    assert.equal(context.pages().length, count, 'Opening again must reuse the named output window');
    await output.screenshot({path: path.join(__dirname, '../logs/broadcast-window-check.png'), animations: 'disabled'});
    await output.close();
    assert.equal(await controller.evaluate(() => testStream.getVideoTracks()[0].readyState), 'live',
      'Closing the output window must preserve the controller stream');
    assert.equal(paidRequests, 0);
    assert.deepEqual(outputApiRequests, []);
    assert.deepEqual(errors, []);
    await controller.evaluate(() => {
      clearInterval(testDrawTimer);
      testStream.getTracks().forEach(track => track.stop());
    });
    console.log(JSON.stringify({
      streamMirrored: true, controlsHidden: true, subtitlesMirrored: true,
      reusedWindow: true, sourceSurvivesOutputClose: true, paidRequests, javascriptErrors: errors.length,
      measuredPlaybackFps: playback.fps,
    }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
