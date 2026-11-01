#!/usr/bin/env node
'use strict';

// Offline regression for the real stage code. No SDK download, browser,
// credentials, HTTP requests, or cloud session is needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const stage = fs.readFileSync(path.join(__dirname, '..', 'stage', 'index.html'), 'utf8');

function between(startMarker, endMarker) {
  const start = stage.indexOf(startMarker);
  const end = start < 0 ? -1 : stage.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0 && end > start, `Stage function boundary changed: ${startMarker}`);
  return stage.slice(start, end);
}

const joinCode = between('async function joinArtc(', '\nasync function joinTrtc(');
const leaveCode = between('function leaveRtc(){', '\nfunction startTimers(');
const resetStart = stage.indexOf('function resetUi(){');
const resetEnd = stage.indexOf('\n}\n', resetStart);
assert.ok(resetStart >= 0 && resetEnd > resetStart, 'Stage resetUi boundary changed');
const resetCode = stage.slice(resetStart, resetEnd + 3);

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

function fixture({ waitForJoin = false } = {}) {
  const calls = [];
  const handlers = new Map();
  const timers = [];
  const nodes = new Map();
  const entered = deferred();
  const completed = deferred();
  const state = {
    rtcGeneration: 1, engine: null, rtcKind: '', joined: false,
    busy: true, liveId: 'offline-live', timerTimer: null,
  };
  function $(id) {
    if (!nodes.has(id)) nodes.set(id, { id, style: {}, textContent: '', srcObject: null });
    return nodes.get(id);
  }
  const engine = {
    on(name, callback) {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(callback);
    },
    emit(name, ...args) {
      for (const callback of handlers.get(name) || []) callback(...args);
    },
    setChannelProfile(value) { calls.push(['profile', value]); },
    async publishLocalAudioStream(value) { calls.push(['publishAudio', value]); },
    async publishLocalVideoStream(value) { calls.push(['publishVideo', value]); },
    setDefaultSubscribeAllRemoteAudioStreams(value) { calls.push(['subscribeAudio', value]); },
    setDefaultSubscribeAllRemoteVideoStreams(value) { calls.push(['subscribeVideo', value]); },
    async joinChannel() {
      calls.push(['join']);
      entered.resolve();
      if (waitForJoin) await completed.promise;
    },
    setRemoteViewConfig(view, uid, track) { calls.push(['view', view.id, uid, track]); },
    async leaveChannel() { calls.push(['leave']); },
    destroy() { calls.push(['destroy']); },
  };
  const context = {
    state, $,
    window: {
      AliRtcEngine: {
        async isSupported() { return { support: true }; },
        setLogLevel() {},
        getInstance() { return engine; },
      },
    },
    setTimeout(callback, delay) { timers.push({ callback, delay }); return timers.length; },
    clearInterval() {},
    trAdd() {},
    maskId() { return 'offline-channel'; },
    toast() {},
    tryPlay() { calls.push(['play']); },
    startTimers() { calls.push(['startTimers']); },
    showAvatarStill(visible) { $('avatarStill').style.display = visible ? '' : 'none'; },
    console: { warn() {} },
    fetch() { throw new Error('Network is forbidden in this regression test'); },
  };
  vm.createContext(context);
  vm.runInContext(`${joinCode}\n${leaveCode}\n${resetCode}`, context, {
    filename: 'actual-stage-rtc-functions.js',
  });
  return {
    context, state, calls, engine, entered, completed, $,
    rtc: { provider: 'artc', channel_id: 'offline-channel', user_id: 'viewer', token: 'fixture' },
    flushTimers() {
      while (timers.length) timers.shift().callback();
    },
  };
}

test('viewer disables local microphone and camera publication before joining', async () => {
  const f = fixture();
  await f.context.joinArtc(f.rtc, 1);
  const joinIndex = f.calls.findIndex(call => call[0] === 'join');
  assert.ok(joinIndex >= 0, 'The actual joinArtc must invoke joinChannel');
  for (const method of ['publishAudio', 'publishVideo']) {
    const index = f.calls.findIndex(call => call[0] === method);
    assert.ok(index >= 0 && index < joinIndex, `${method} must run before joinChannel`);
    assert.equal(f.calls[index][1], false, `${method} must disable local publication`);
  }
  assert.ok(f.calls.some(call => call[0] === 'subscribeAudio' && call[1] === true));
  assert.ok(f.calls.some(call => call[0] === 'subscribeVideo' && call[1] === true));
  assert.equal(f.state.joined, true);
});

test('remote renderer stays bound to its camera track instead of being overwritten by screen', async () => {
  const f = fixture();
  await f.context.joinArtc(f.rtc, 1);
  f.engine.emit('remoteUserOnLineNotify', 'renderer');
  f.flushTimers();
  f.engine.emit('videoSubscribeStateChanged', 'renderer', 2, 3);
  const views = f.calls.filter(call => call[0] === 'view');
  assert.ok(views.length >= 1, 'Remote online/subscription callbacks must bind the real video element');
  assert.ok(views.every(call => call[1] === 'video' && call[2] === 'renderer'));
  assert.ok(views.every(call => call[3] === 1), 'Track 2 is screen share and must not replace the camera binding');
});

test('reset during a pending join prevents late join completion and callbacks from reviving the UI', async () => {
  const f = fixture({ waitForJoin: true });
  const pendingJoin = f.context.joinArtc(f.rtc, 1);
  await f.entered.promise;
  // Queue genuine stage callbacks before reset, then fire them after reset.
  f.engine.emit('remoteUserOnLineNotify', 'renderer');
  f.context.resetUi();
  assert.equal(f.state.rtcGeneration, 2);
  assert.equal(f.$('mask').style.display, '');
  assert.equal(f.$('avatarStill').style.display, '');
  f.completed.resolve();
  await pendingJoin;
  f.flushTimers();
  f.engine.emit('videoSubscribeStateChanged', 'renderer', 2, 3);
  assert.equal(f.state.engine, null);
  assert.equal(f.state.joined, false);
  assert.equal(f.$('mask').style.display, '', 'A stale join must not hide the ended-session mask');
  assert.equal(f.$('avatarStill').style.display, '', 'A stale join must not hide the idle image');
  assert.equal(f.calls.filter(call => call[0] === 'view').length, 0);
  assert.equal(f.calls.filter(call => call[0] === 'startTimers').length, 0);
  assert.ok(f.calls.some(call => call[0] === 'leave'), 'The real reset path must leave RTC');
});
