const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage();
    const errors = [], actions = [];
    let rejectSave = false;
    page.on('pageerror', e => errors.push(e.message));
    const state = {available:true, configured:true, paused:true, token:'fixture', daily_limit:1000, interval_minutes:2, next_send:0, selected_senders:['one'], accounts:[{id:'one', email:'sender@example.com', configured:true}], suppressed:[], checks:{}, messages:[{id:1,company:'Acme',recipient:'jane@example.com',status:'pending',priority:1}], schedule:{enabled:false,start:'09:00',end:'11:00',weekdays_only:true,not_before:0,fallback_timezone:'UTC'}};
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if(url.pathname === '/') return route.fulfill({contentType:'text/html',body:fs.readFileSync('index.html','utf8')});
      if(url.pathname === '/api/mail') {
        if(req.method() === 'POST') {
          const payload = req.postDataJSON(); actions.push(payload);
          if(rejectSave) return route.fulfill({status:400,json:{error:'Invalid time zone'}});
          assert.equal(payload.action,'set_schedule');
          state.schedule = payload.schedule;
          return route.fulfill({json:{schedule:state.schedule}});
        }
        return route.fulfill({json:state});
      }
      return route.fulfill({json:[]});
    });
    await page.goto('http://localhost:8765/');
    await page.waitForFunction(() => !!mailState);
    assert.equal(await page.locator('#scheduleStart').isDisabled(),true);
    assert.equal(await page.locator('#saveSchedule').isDisabled(),true);
    assert.equal(await page.locator('#startMail').innerText(),'Start sending now');
    await page.check('#scheduleEnabled');
    await page.waitForFunction(() => mailState.schedule.enabled && !scheduleSaving);
    assert.equal(actions.length,1); // Switch itself persists the mode.
    assert.equal(await page.locator('#scheduleStart').isDisabled(),false);
    assert.equal(await page.locator('#startMail').innerText(),'Start scheduled sending');
    await page.fill('#scheduleStart','08:00');
    await page.click('#saveSchedule');
    await page.waitForFunction(() => mailState.schedule.start === '08:00' && !scheduleSaving);
    await page.uncheck('#scheduleEnabled');
    await page.waitForFunction(() => !mailState.schedule.enabled && !scheduleSaving);
    assert.equal(state.schedule.start,'08:00'); // Turning off retains the window.
    assert.equal(await page.locator('#scheduleStart').isDisabled(),true);
    assert.match(await page.locator('#scheduleStatus').innerText(),/Scheduling OFF/);
    await page.reload();
    await page.waitForFunction(() => !!mailState);
    assert.equal(await page.locator('#scheduleEnabled').isChecked(),false);
    await page.check('#scheduleEnabled');
    await page.waitForFunction(() => mailState.schedule.enabled && !scheduleSaving);
    await page.click('#disableSchedule');
    await page.waitForFunction(() => !mailState.schedule.enabled && !scheduleSaving);
    rejectSave = true;
    await page.check('#scheduleEnabled');
    await page.waitForFunction(() => document.querySelector('#scheduleStatus').textContent.includes('Could not change scheduling'));
    assert.equal(await page.locator('#scheduleEnabled').isChecked(),false);
    assert.equal(await page.locator('#scheduleStart').isDisabled(),true);
    assert.equal(state.paused,true); // Changing modes never starts sending.
    assert.deepEqual(errors,[]);
    console.log('Schedule UI passed: immediate on/off, window updates, persistence, and failure recovery. No mail sent.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exit(1);});
