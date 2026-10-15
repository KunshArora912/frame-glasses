// Frame phone companion: Web Bluetooth link to the glasses, optional WebSocket
// relay to the laptop so the Python apps run through this phone.
'use strict';

const SERVICE = '7a230001-5475-a6a4-654c-8431f6ad49c4';
const TX = '7a230002-5475-a6a4-654c-8431f6ad49c4';
const RX = '7a230003-5475-a6a4-654c-8431f6ad49c4';
const MTU = 247;                       // Frame negotiates 247; Lua payload 244
const APPS = ['assistant', 'shell', 'todo', 'navigate', 'showcase'];

const $ = id => document.getElementById(id);
const enc = new TextEncoder(), dec = new TextDecoder();

let device = null, server = null, tx = null, rx = null;
let relay = null, relayWanted = false;
let wakeLock = null;
let writeChain = Promise.resolve();    // GATT writes must not overlap

function log(s) {
  const el = $('log');
  const t = new Date().toTimeString().slice(0, 8);
  el.textContent += '[' + t + '] ' + s + '\n';
  el.scrollTop = el.scrollHeight;
}

// ---------- Bluetooth ----------
async function connect() {
  if (!navigator.bluetooth) { log('Web Bluetooth not available in this browser'); return; }
  try {
    device = await navigator.bluetooth.requestDevice({ filters: [{ services: [SERVICE] }] });
    device.addEventListener('gattserverdisconnected', onBleDown);
    log('connecting to ' + device.name + ' ...');
    server = await device.gatt.connect();
    const svc = await server.getPrimaryService(SERVICE);
    tx = await svc.getCharacteristic(TX);
    rx = await svc.getCharacteristic(RX);
    rx.addEventListener('characteristicvaluechanged', onNotify);
    await rx.startNotifications();       // triggers the Android pairing dialog on a fresh bond
    onBleUp();
  } catch (e) {
    log('connect failed: ' + (e.message || e));
    onBleDown();
  }
}

function onBleUp() {
  $('bleDot').classList.add('on');
  $('bleText').textContent = 'Connected: ' + device.name;
  $('connectBtn').hidden = true;
  $('disconnectBtn').hidden = false;
  log('connected to ' + device.name);
  keepAwake(true);
  relaySend({ t: 'ble', state: 'connected', name: device.name, mtu: MTU });
}

function onBleDown() {
  $('bleDot').classList.remove('on');
  $('bleText').textContent = 'Not connected';
  $('connectBtn').hidden = false;
  $('disconnectBtn').hidden = true;
  $('state').textContent = 'glasses asleep or out of range';
  tx = rx = server = null;
  keepAwake(false);
  relaySend({ t: 'ble', state: 'disconnected' });
}

async function disconnect() {
  try { if (device && device.gatt.connected) device.gatt.disconnect(); } catch (e) {}
  onBleDown();
}

// Every packet from the glasses: 0x01-prefixed = data (binary, for the laptop
// app), anything else = printed text (status lines, launch requests, REPL).
let textBuf = '';
function onNotify(ev) {
  const v = ev.target.value;
  const bytes = new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
  relaySend({ t: 'notify', d: b64(bytes) });
  if (bytes[0] === 0x01) return;
  textBuf += dec.decode(bytes);
  let i;
  while ((i = textBuf.indexOf('\n')) >= 0) {
    handleLine(textBuf.slice(0, i).trim());
    textBuf = textBuf.slice(i + 1);
  }
  if (textBuf.length > 400) { handleLine(textBuf.trim()); textBuf = ''; }
}

function handleLine(line) {
  if (!line) return;
  if (line.startsWith('status:')) {            // status:<pct>:<chg>:<fw>  from main.lua
    const parts = line.split(':');
    const pct = Number(parts[1]), chg = parts[2], fw = parts[3];
    $('pct').textContent = pct + '%';
    $('barFill').style.width = pct + '%';
    $('barFill').style.background = pct < 20 ? '#e33' : pct < 50 ? '#f90' : '#3c3';
    $('state').textContent = chg === '1' ? (pct >= 99 ? 'Charged' : 'Charging') : chg === '0' ? 'On battery' : '';
    if (fw) $('fw').textContent = fw;
    $('seen').textContent = 'reported ' + new Date().toTimeString().slice(0, 8);
    return;
  }
  if (line.startsWith('launch:')) {
    const id = line.slice(7);
    log('glasses asked for ' + id + (relay ? ' -> laptop' : ' (laptop not linked)'));
    return;
  }
  log('< ' + line);
}

function write(bytes, withResponse) {
  if (withResponse === undefined) withResponse = true;
  if (!tx) return Promise.reject(new Error('not connected'));
  const p = writeChain.then(() => withResponse ? tx.writeValueWithResponse(bytes) : tx.writeValueWithoutResponse(bytes));
  writeChain = p.catch(() => {});
  return p;
}

async function sendLua(s) {
  const bytes = enc.encode(s);
  if (bytes.length > MTU - 3) { log('too long for one packet'); return; }
  log('> ' + s);
  await write(bytes);
}

// ---------- relay to the laptop ----------
function relaySend(m) {
  if (relay && relay.readyState === 1) relay.send(JSON.stringify(m));
}

function relayToggle() {
  if (relay) { relayWanted = false; relay.close(); relay = null; relayUi(false); return; }
  relayWanted = true;
  relayOpen();
}

function relayOpen() {
  const url = $('relayUrl').value.trim();
  if (!url) { log('enter the laptop relay URL first'); relayWanted = false; return; }
  localStorage.setItem('relayUrl', url);
  log('linking to ' + url);
  const ws = new WebSocket(url);
  relay = ws;
  ws.onopen = () => {
    relayUi(true);
    log('laptop linked');
    ws.send(JSON.stringify({ t: 'hello', name: device ? device.name : null, mtu: MTU }));
    if (device && device.gatt.connected) ws.send(JSON.stringify({ t: 'ble', state: 'connected', name: device.name, mtu: MTU }));
  };
  ws.onmessage = async ev => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.t === 'write') {
      try {
        await write(unb64(m.d), m.r !== false);
        ws.send(JSON.stringify({ t: 'wrote', id: m.id }));
      } catch (e) {
        ws.send(JSON.stringify({ t: 'error', id: m.id, msg: String(e.message || e) }));
      }
    }
  };
  ws.onclose = () => {
    relayUi(false);
    if (relay === ws) relay = null;
    if (relayWanted) { log('laptop link dropped; retrying in 5 s'); setTimeout(() => { if (relayWanted && !relay) relayOpen(); }, 5000); }
  };
  ws.onerror = () => {};
}

function relayUi(on) {
  $('relayDot').classList.toggle('on', on);
  $('relayBtn').textContent = on ? 'Unlink' : 'Link';
}

// ---------- helpers ----------
function b64(bytes) { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); }
function unb64(s) { const bin = atob(s); const out = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i); return out; }

async function keepAwake(on) {
  try {
    if (on && !wakeLock && navigator.wakeLock) wakeLock = await navigator.wakeLock.request('screen');
    if (!on && wakeLock) { await wakeLock.release(); wakeLock = null; }
  } catch (e) {}
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && device && device.gatt.connected) keepAwake(true); });

// ---------- wiring ----------
$('connectBtn').onclick = connect;
$('disconnectBtn').onclick = disconnect;
$('relayBtn').onclick = relayToggle;
$('sendBtn').onclick = () => { const s = $('lua').value; if (s) sendLua(s); };
$('lua').addEventListener('keydown', e => { if (e.key === 'Enter') $('sendBtn').click(); });
$('breakBtn').onclick = () => { log('> break'); write(new Uint8Array([0x03])); };
$('resetBtn').onclick = () => { log('> reset'); write(new Uint8Array([0x04])); };
$('clearBtn').onclick = () => { $('log').textContent = ''; };
for (const id of APPS) {
  const b = document.createElement('button');
  b.textContent = id;
  // same request the glasses' menu makes; the laptop's frame_host.py acts on it
  b.onclick = () => { relaySend({ t: 'notify', d: b64(enc.encode('launch:' + id + '\n')) }); log('asked laptop for ' + id); };
  $('apps').appendChild(b);
}
$('relayUrl').value = localStorage.getItem('relayUrl') || ((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/phone');
if (!navigator.bluetooth) $('hint').textContent = 'This browser has no Web Bluetooth. Use Chrome on Android, or Bluefy on iPhone.';
if (!window.isSecureContext) $('hint').textContent += ' Page is not a secure context: Web Bluetooth is disabled here (see phone/README.md).';
