'use strict';
const $ = id => document.getElementById(id);
let supported = false;
let fullRequired = true;
const show = (id, value) => { $(id).textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); };
async function request(path, options = {}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(10000)});
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch { data = {detail: text}; }
  return {ok: response.ok, status: response.status, data};
}
$('mode').addEventListener('change', () => {
  if ($('mode').value === 'split' && !supported) { $('mode').value = 'legacy'; show('status', 'API ยังไม่รองรับชื่อแยก'); }
  const split = $('mode').value === 'split';
  $('legacy').hidden = split;
  $('split').hidden = !split;
  $('full').required = !split;
  $('first').required = split;
  $('last').required = split;
});
async function initialize() {
  try {
    const result = await request('/openapi.json');
    if (!result.ok) throw new Error(`OpenAPI HTTP ${result.status}`);
    const schema = result.data.components.schemas.UserCreate;
    if (!schema || !schema.properties) throw new Error('ไม่พบ UserCreate schema');
    supported = Boolean(schema.properties.first_name && schema.properties.last_name);
    fullRequired = (schema.required || []).includes('full_name');
    $('mode').options[1].disabled = !supported;
    $('mode').options[1].textContent = supported ? 'ชื่อ / นามสกุล (first_name / last_name)' : 'ชื่อ / นามสกุล — รอ API รองรับ';
    show('schema', supported ? 'API รองรับชื่อ / นามสกุลแยก' : 'API ปัจจุบันรองรับ full_name เท่านั้น');
    $('submit').disabled = false;
    const health = await request('/health');
    show('status', health.ok ? 'พร้อมทดสอบ · API และฐานข้อมูลเชื่อมต่อสำเร็จ' : `ระบบยังไม่พร้อม · HTTP ${health.status}`);
  } catch (error) { show('status', `ตรวจ API ไม่สำเร็จ: ${error.message} กรุณารีเฟรชหน้า`); }
}
$('submit').addEventListener('click', () => $('user-form').requestSubmit());
$('user-form').addEventListener('submit', async event => {
  event.preventDefault();
  const password = $('password').value;
  if (new TextEncoder().encode(password).length > 72) { show('status', 'รหัสผ่านยาวเกิน 72 ไบต์ UTF-8'); return; }
  const payload = {email: $('email').value.trim(), username: $('username').value.trim(), password};
  if ($('mode').value === 'split') {
    if (!supported) return;
    payload.first_name = $('first').value.trim();
    payload.last_name = $('last').value.trim();
    if (fullRequired) payload.full_name = `${payload.first_name} ${payload.last_name}`;
  } else { payload.full_name = $('full').value.trim(); }
  show('payload', {...payload, password: '[hidden]'});
  show('response', 'กำลังรอผล…');
  $('submit').disabled = true;
  try {
    const result = await request('/api/v1/users', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    show('response', result.data);
    show('status', result.ok ? `สร้างผู้ใช้สำเร็จ · HTTP ${result.status}` : `สร้างผู้ใช้ไม่สำเร็จ · HTTP ${result.status}`);
    if (result.ok) $('password').value = '';
  } catch (error) { show('status', `ไม่ได้รับคำตอบ: ${error.message} โปรดตรวจรายชื่อก่อนส่งซ้ำ เพราะคำขออาจสำเร็จแล้ว`); }
  finally { $('submit').disabled = false; }
});
$('refresh').addEventListener('click', async () => {
  $('refresh').disabled = true;
  try {
    const result = await request('/api/v1/users?page=1&page_size=10');
    if (!result.ok) throw new Error(`HTTP ${result.status}`);
    $('users').replaceChildren();
    for (const user of result.data.items) {
      const li = document.createElement('li');
      li.textContent = `#${user.id} · ${[user.first_name,user.last_name].filter(Boolean).join(' ') || user.full_name || user.username} · ${user.email}`;
      $('users').append(li);
    }
    show('status', `ผู้ใช้ทั้งหมด ${result.data.total} คน · แสดงหน้าแรกสูงสุด 10 คน`);
  } catch (error) { show('status', `โหลดรายชื่อไม่สำเร็จ: ${error.message}`); }
  finally { $('refresh').disabled = false; }
});
initialize();
