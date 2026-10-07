const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage({viewport:{width:1100,height:900}}), errors=[];
    page.on('pageerror', e => errors.push(e.message));
    const state = {available:true,configured:true,paused:true,token:'fixture',daily_limit:20,interval_minutes:2,next_send:0,selected_senders:['primary'],accounts:[{id:'primary',email:'sender@acme.com',configured:true}],checks:{},suppressed:[],messages:Array.from({length:25},(_,i)=>({id:i+1,company:'Historical '+i,recipient:`old${i}@past.com`,source:i%2?'valid':'guess',status:'sent',sender:'sender@acme.com',priority:0}))};
    const companies=[{name:'Acme',slug:'acme',batch:'Winter 2024',domain:'acme.com',website:'https://acme.com',founders:[{name:'Jane Doe',title:'CEO',emails_found:['jane@acme.com']},{name:'Joe Smith',title:'CTO',email_guesses:['joe@acme.com']}]}, {name:'Missing',slug:'missing',batch:'Winter 2024',domain:'missing.com',website:'https://missing.com',founders:[{name:'Bad Guess',apify_email:'bad@missing.com',apify_status:'invalid'}]}];
    const dialogs=[];
    page.on('dialog',d=>{dialogs.push(d.message()); d.accept();});
    companies.push({name:'Historical 0',slug:'past',domain:'past.com',founders:[{name:'Old Contact',email_guesses:['old0@past.com']}]});
    companies[0].founders.push({name:'Previously Sent',email_guesses:['old1@past.com']});
    let verifierCalls=0, discoveryCalls=0, domainCalls=0, sampleCalls=0, importCalls=0;
    let sampleEmails=[];
    let historyReads=0, historyUnavailable=false, extraDirectoryRows=[];
    let holdCatalogJob=false,catalogRun=0,catalogJobState={running:false,completed:0,total:0,failed:[],new_slugs:[]};
    const catalogKnown=new Set(companies.map(c=>c.slug));
    const verifiedRow = email => ({email,status:'valid',smtp_valid:true,is_catch_all:false,is_disposable:false,syntax_valid:true,domain_exists:true,mx_found:true,risk_type:'none'});
    await page.route('**/*', async route => {
      const req=route.request(),u=new URL(req.url()); let data;
      if(u.pathname.startsWith('/vendor/tabulator/')) return route.fulfill({contentType:u.pathname.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync('.'+u.pathname,'utf8')});
      if(u.pathname==='/') return route.fulfill({contentType:'text/html',body:fs.readFileSync('index.html','utf8')});
      if(u.pathname==='/api/yc') data=u.searchParams.get('action')==='batches'?[{batch:'Winter 2024',count:2}]:['companies','catalog'].includes(u.searchParams.get('action'))?[...companies,...extraDirectoryRows]:companies;
      else if(u.pathname==='/api/catalog') {
        if(req.method()==='POST') {assert.equal(req.postDataJSON().action,'refresh');assert.equal(req.headers()['x-outreach-token'],'fixture');const newSlugs=extraDirectoryRows.filter(c=>!catalogKnown.has(c.slug)).map(c=>c.slug);newSlugs.forEach(s=>catalogKnown.add(s));catalogJobState={running:holdCatalogJob,run_id:String(++catalogRun),completed:holdCatalogJob?0:1,total:1,failed:[],new_slugs:newSlugs,current_batch:'Winter 2024'};}
        data=catalogJobState;
      }
      else if(u.pathname==='/api/mail') {
        if(req.method()==='POST') {
          const body=req.postDataJSON();
          if(body.action==='enqueue') {assert.equal(body.messages.length,3); assert.equal(new Set(body.messages.map(m=>m.recipient)).size,3); const joe=body.messages.find(m=>m.recipient==='joe@acme.com'); assert.match(joe.body,/Hey Joe/); assert.equal(joe.variants.length,2); assert.match(joe.variants[1].body,/Hello Joe/); assert.equal(joe.variants[1].subject,'Another role at Acme'); state.messages.push(...body.messages.map((m,i)=>({...m,id:100+i,priority:1,status:'held'}))); data={added:3,skipped:0};}
          else if(body.action==='import_checks') {importCalls++; assert.equal(body.provider,'bounceverify'); for(const m of state.messages.filter(m=>m.status==='held')) {state.checks[m.recipient]={verdict:'deliverable',checked:Date.now()/1000,run_id:'Verify1'}; m.status='pending';} data={imported:3};}
          else if(body.action==='bounce') {const m=state.messages.find(m=>m.recipient===body.email); m.status='bounced'; state.suppressed.push(body.email); data={bounced:1};}
          else data={ok:true};
        } else {historyReads++; if(historyUnavailable) return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'History temporarily unavailable'})}); data=state;}
      } else if(u.pathname.includes('/acts/code_crafter')) {discoveryCalls++; assert.equal(req.postDataJSON().fetch_count,100); assert.deepEqual(req.postDataJSON().email_status,['validated']); assert.ok(!req.postDataJSON().company_domain.includes('past.com')); data={data:{id:'Find1',status:'SUCCEEDED',defaultDatasetId:'Contacts1'}};}
      else if(u.pathname.includes('/datasets/Contacts1')) data=[{company_domain:'https://www.missing.com/',first_name:'Real',last_name:'Founder',job_title:'Founder',email:'real@missing.com'}, {company_domain:'outsider.com',full_name:'Wrong Company',email:'wrong@outsider.com'}];
      else if(u.pathname.includes('/acts/vulnv')) {domainCalls++; assert.deepEqual(req.postDataJSON().emails.sort(),['jane@acme.com','joe@acme.com','real@missing.com']); data={data:{id:'Domain1',status:'SUCCEEDED',defaultDatasetId:'Domains1'}};}
      else if(u.pathname.includes('/datasets/Domains1')) data=['jane@acme.com','joe@acme.com','real@missing.com'].map(email=>({input_email:email,status:'valid',is_valid:true}));
      else if(u.pathname.includes('/acts/bounceverify')) {
        const input=req.postDataJSON(); assert.equal(input.timeoutSecs,120); assert.equal(input.skipInvalidSyntax,true);
        if(input.emails.includes('sender@acme.com')) {sampleCalls++; sampleEmails=input.emails; data={data:{id:'Sample1',status:'SUCCEEDED',defaultDatasetId:'SampleChecks1'}};}
        else {verifierCalls++; assert.deepEqual(input.emails.sort(),['jane@acme.com','joe@acme.com','real@missing.com']); data={data:{id:'Verify1',status:'SUCCEEDED',finishedAt:new Date().toISOString(),defaultDatasetId:'Checks1'}};}
      }
      else if(u.pathname.includes('/datasets/SampleChecks1')) data=sampleEmails.map(verifiedRow);
      else if(u.pathname.includes('/datasets/Checks1')) data=['jane@acme.com','joe@acme.com','real@missing.com'].map(verifiedRow);
      else return route.fulfill({status:404,body:'Unexpected request'});
      return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto('http://localhost:8765/');
    await page.locator('#load').click();
    await page.waitForFunction(()=>document.querySelectorAll('#list details').length===3);
    await page.locator('[data-k="body"]').fill('Hey {first_name}, I built a relevant project for {company}.');
    await page.locator('#rotateTemplates').check();
    await page.locator('[data-k="subject_b"]').fill('Another role at {company}');
    await page.locator('[data-k="body_b"]').fill('Hello {first_name}, this is my second reviewed draft for {company}.');
    await page.locator('#token').fill('fixture');
    await page.locator('#freeDiscovery summary').click();
    const downloadEvent=page.waitForEvent('download');
    await page.locator('#prepareDiscovery').click();
    const searchDownload=await downloadEvent;
    const preparedInput=JSON.parse(fs.readFileSync(await searchDownload.path(),'utf8'));
    assert.equal(preparedInput.fetch_count,100);
    assert.ok(!preparedInput.company_domain.includes('past.com'));
    assert.equal(discoveryCalls,0);
    await page.locator('#discover').click();
    await page.waitForFunction(()=>document.getElementById('discoverStatus').textContent.includes('Found 1'));
    const manualRows=[{company_domain:'missing.com',full_name:'Real Founder',email:'real@missing.com'}, {company_domain:'past.com',full_name:'New Person',email:'new@past.com'}, {company_domain:'outsider.com',full_name:'Outsider',email:'other@outsider.com'}];
    await page.locator('#discoveryFile').setInputFiles({name:'results.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(manualRows))});
    await page.waitForFunction(()=>document.getElementById('discoverStatus').textContent.includes('Imported 0'));
    assert.equal(await page.evaluate(()=>DATA.flatMap(c=>c.extra_contacts||[]).length),1);
    assert.equal(discoveryCalls,1);
    assert.equal(state.messages.length,25); // Import did not queue or send.
    await page.locator('#discoveryFile').setInputFiles({name:'invalid.json',mimeType:'application/json',buffer:Buffer.from('[null]')});
    await page.waitForFunction(()=>document.getElementById('discoverStatus').textContent.includes('contact objects'));
    // Actual provider shape: null company metadata and companies whose profile
    // page is not loaded yet must still match the full selected batch.
    await page.evaluate(()=>{
      window.discoveryFixtureBackup={all:ALL,data:DATA};
      ALL=[{name:'Cosmic Robotics',slug:'cosmic-robotics',website:'www.cosmicrobotics.com/'},{name:'Markov',slug:'markov',website:'https://www.markovstudios.com/'}];DATA=[];
    });
    const providerRows=[
      {full_name:'Lewis C',email:'lewis@cosmicrobotics.com',company_domain:'cosmicrobotics.com',job_title:'Co-founder, CTO'},
      {first_name:'Jai',last_name:'Mandal',email:'jai@markovstudios.com',company_domain:null,company_website:null,job_title:'Co-founder'},
      {full_name:'Melvin Chen',email:'melvin.chen@careaihq.com',company_domain:'careaihq.com'},
      {full_name:'James Emerick',email:'james@cosmicrobotics.com',company_website:'https://www.cosmicrobotics.com'},
      {first_name:'Dev',last_name:'Mandal',email:'dev@markovstudios.com',company_domain:null}
    ];
    await page.locator('#discoveryFile').setInputFiles({name:'provider-results.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(providerRows))});
    await page.waitForFunction(()=>document.getElementById('discoverStatus').textContent.includes('Imported 4 of 5'));
    assert.match(await page.locator('#discoveryReport').innerText(),/Company not in the loaded batch/);
    assert.equal(await page.evaluate(()=>DATA.length),2);
    assert.equal(await page.evaluate(()=>DATA.flatMap(c=>contacts(c)).every(f=>best(f).kind==='unknown')),true);
    assert.equal(state.messages.length,25);
    await page.locator('#discoveryFile').setInputFiles({name:'provider-results.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(providerRows))});
    await page.waitForFunction(()=>document.getElementById('discoverStatus').textContent.includes('Imported 0 of 5'));
    assert.match(await page.locator('#discoveryReport').innerText(),/Already present/);
    await page.evaluate(()=>{ALL=window.discoveryFixtureBackup.all;DATA=window.discoveryFixtureBackup.data;delete window.discoveryFixtureBackup;save();render();progress();});


    await page.locator('#reviewMail').click();
    assert.equal(await page.locator('.review-message').count(),3);
    assert.equal(await page.locator('.choose-mail:checked').count(),3);
    assert.equal(await page.locator('.body-mail-b').count(),3);
    assert.equal(await page.locator('.recipient-mail[value="bad@missing.com"]').count(),0);
    // Updating the template must not try to render review details as company rows.
    await page.locator('.review-message').first().evaluate(d=>{d.open=true;});
    await page.locator('[data-k="subject"]').fill('A role at {company}');
    await page.locator('#queueMail').click();
    await page.waitForFunction(()=>document.getElementById('mailNextStep').textContent.includes('need mailbox verification'));
    assert.equal(await page.locator('#startMail').isDisabled(),true);
    // A reloaded company name can differ in capitalization/spacing. Even its
    // never-contacted alternate must be excluded when the company is complete.
    await page.evaluate(()=>{DATA[2].name='  HISTORICAL 0  '; DATA[2].founders.push({name:'Another Person',email_guesses:['another@past.com']});});
    const readsBeforeDomain=historyReads;
    await page.locator('#checkDomains').click();
    await page.waitForFunction(()=>document.getElementById('domainStatus').textContent.includes('3/3'));
    assert.match(await page.locator('#list').innerText(),/checked \(domain\)/);
    assert.equal(state.messages.filter(m=>m.status==='held').length,3);
    assert.equal(await page.locator('#startMail').isDisabled(),true);
    assert.ok(historyReads>readsBeforeDomain);
    assert.ok(dialogs.some(s=>s.includes('3 new, unsent addresses') && s.includes('3 already contacted addresses skipped')));
    await page.locator('#testMailboxes').click();
    await page.waitForFunction(()=>document.getElementById('testStatus').textContent.includes('Sample complete'));
    assert.equal(importCalls,0);
    assert.equal(state.messages.filter(m=>m.status==='held').length,3);
    assert.equal(await page.locator('#startMail').isDisabled(),true);
    await page.locator('#enrich').click();
    await page.waitForFunction(()=>document.getElementById('enrichStatus').textContent.includes('3/3'));
    assert.equal(await page.locator('#startMail').isEnabled(),true);
    await page.locator('#dailyLimit').fill('1000');
    assert.equal(await page.locator('#startMail').isEnabled(),true);
    assert.equal(await page.locator('#dailyLimit').getAttribute('max'),'1000');
    await page.locator('[data-tab="sent"]').click();
    assert.equal(await page.locator('#mailHistory tbody tr').count(),20);
    assert.equal(await page.locator('#mailHistory .quality-dot').count(),20);
    await page.locator('[data-history-page="1"]').click();
    assert.equal(await page.locator('#mailHistory tbody tr').count(),5);
    await page.locator('#historySearch').fill('old24');
    assert.equal(await page.locator('#mailHistory tbody tr').count(),1);
    historyUnavailable=true;
    await page.locator('#checkDomains').click();
    await page.waitForFunction(()=>document.getElementById('domainStatus').textContent.includes('no Apify run was started'));
    assert.equal(domainCalls,1);
    historyUnavailable=false;
    await page.locator('#historySearch').fill('');
    await page.locator('#blockEmail').fill('old24@past.com');
    await page.locator('#bounceMail').click();
    await page.waitForFunction(()=>document.getElementById('mailHistory').textContent.includes('bounced'));
    assert.equal(await page.locator('#mailHistory tbody tr').count(),1);
    await page.locator('#testMailboxes').click();
    await page.waitForFunction(()=>document.getElementById('testStatus').textContent.includes('known bounced address was marked deliverable'));
    assert.equal(importCalls,1);
    assert.ok(state.suppressed.includes('old24@past.com'));
    await page.locator('#reimportMailChecks').click();
    await page.waitForFunction(()=>document.getElementById('enrichStatus').textContent.includes('Refreshed 1 completed'));
    assert.equal(verifierCalls,1);
    state.messages.push({id:300,company:'Catch All',recipient:'maybe@catch.com',source:'site',status:'held',priority:1});
    state.checks['maybe@catch.com']={verdict:'catch-all',checked:Date.now()/1000};
    await page.evaluate(()=>refreshMail());
    await page.locator('[data-tab="queue"]').click();
    assert.match(await page.locator('#mailHistory').innerText(),/Held · catch-all/);
    assert.match(await page.locator('#mailHistory').innerText(),/mailbox is not confirmed/);
    await page.locator('#sourceExplorer').click();
    await page.waitForFunction(()=>document.querySelector('#explorerStatus').textContent.includes('Synced 1 batches'));
    assert.equal(await page.evaluate(()=>explorerAutoStarted),true);
    const contactedRow=page.locator('#explorerTable .tabulator-row').filter({hasText:'Historical 0'});
    assert.match(await contactedRow.innerText(),/Already sent · 1/);
    state.messages[0].company='  HISTORICAL 0  ';
    state.messages[0].status='bounced';
    await page.evaluate(()=>refreshMail());
    await page.waitForFunction(()=>document.querySelector('#explorerTable').textContent.includes('Bounced · 1'));
    assert.match(await contactedRow.innerText(),/Bounced · 1/);
    assert.ok(!(await contactedRow.innerText()).includes('Already sent'));
    await page.locator('.explorer-data-options summary').click();
    await page.locator('#explorerLoad').click();
    await page.waitForFunction(()=>document.querySelector('#explorerCount').textContent.includes('3 of 3'));
    await page.locator('#explorerCatalog').click();
    await page.waitForFunction(()=>document.querySelector('#explorerStatus').textContent.includes('Loaded 3 saved company records'));
    extraDirectoryRows=[{name:'New Startup',slug:'new-startup',batch:'Winter 2024',website:'https://new-startup.com',launched_at:1}];
    await page.locator('#explorerSync').click();
    await page.waitForFunction(()=>document.querySelector('#explorerStatus').textContent.includes('Synced 1 batches'));
    await page.waitForFunction(()=>document.querySelector('#explorerCount').textContent.includes('4 of 4'));
    assert.match(await page.locator('#explorerCount').innerText(),/4 of 4/);
    assert.match(await page.locator('#explorerStatus').innerText(),/1 new companies/);
    assert.match(await page.locator('#explorerTable .tabulator-row').first().innerText(),/New Startup.*New this refresh/s);
    holdCatalogJob=true;
    await page.locator('#explorerSync').click();
    await page.waitForFunction(()=>document.querySelector('#explorerStatus').textContent.includes('Background refresh'));
    await page.locator('#sourceYC').click();
    assert.equal(await page.locator('#ycSource').isVisible(),true);
    await page.locator('#sourceImport').click();
    assert.equal(await page.locator('#importSource').isVisible(),true);
    await page.locator('#sourceExplorer').click();
    assert.equal(catalogRun,3); // Reopening attaches to the same job.
    catalogJobState.running=false;catalogJobState.completed=1;holdCatalogJob=false;
    await page.evaluate(()=>pollCatalogJob());
    await page.locator('#explorerOutreachFilter').selectOption('fresh');
    assert.match(await page.locator('#explorerCount').innerText(),/1 of 4/);
    await page.locator('#explorerSelectFresh').click();
    assert.deepEqual(await page.evaluate(()=>[...explorerSelected]),['new-startup']);
    await page.locator('#explorerDeselect').click();
    await page.locator('#explorerOutreachFilter').selectOption('');
    await page.locator('#explorerSearch').fill('Acme');
    assert.match(await page.locator('#explorerCount').innerText(),/1 of 4/);
    await page.locator('#explorerSelect').click();
    assert.match(await page.locator('#explorerCount').innerText(),/1 selected/);
    await page.locator('#explorerEnrich').click();
    await page.waitForFunction(()=>document.querySelector('#explorerStatus').textContent.includes('Fetched 1 profiles'));
    await page.locator('#explorerBatchFilter').selectOption('Winter 2024');
    await page.locator('#explorerTable .tabulator-row button').first().click();
    assert.equal(await page.locator('#explorerDetail').isVisible(),true);
    assert.equal(await page.locator('#explorerDetailTitle').innerText(),'Acme');
    assert.match(await page.locator('#explorerTable').innerText(),/Fetched ✓/);
    assert.match(await page.locator('#explorerDetailBody').innerText(),/Your outreach to this company/);
    assert.match(await page.locator('#explorerDetailBody').innerText(),/In queue/);
    assert.match(await page.locator('#explorerDetailBody').innerText(),/Published founder emails/);
    assert.match(await page.locator('#explorerDetailBody').innerText(),/Unverified guesses/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#explorerDetail').isVisible(),false);
    await page.emulateMedia({colorScheme:'dark'});
    await page.locator('#explorerSearch').fill('');
    await page.setViewportSize({width:1280,height:1080});
    await page.screenshot({path:'/private/tmp/yc-explorer-redesign.png'});
    await page.locator('#explorerSearch').fill('Acme');
    await page.setViewportSize({width:390,height:844});
    await page.locator('#explorerTable .tabulator-row button').first().click();
    const profileBounds=await page.locator('#explorerDetail').boundingBox();
    assert.ok(profileBounds.x>=0 && profileBounds.x+profileBounds.width<=390);
    await page.screenshot({path:'/private/tmp/yc-explorer-profile-mobile.png'});
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:1280,height:1080});
    const csv=await page.evaluate(()=>explorerCSV(explorerFiltered()));
    assert.match(csv,/published_emails/);
    assert.match(csv,/Acme/);
    assert.ok(!csv.includes('Historical 0'));
    assert.equal(await page.evaluate(()=>csvCell('=HYPERLINK("bad")')), '"\'=HYPERLINK(""bad"")"');
    const queueBeforeTransfer=state.messages.length;
    await page.locator('#explorerSend').click();
    assert.equal(await page.locator('#outreachHandoff').isVisible(),true);
    assert.match(await page.locator('#outreachHandoffText').innerText(),/Nothing has been queued or sent/);
    assert.equal(await page.locator('#ycSource').isVisible(),true);
    assert.match(await page.locator('#list').innerText(),/Acme/);
    assert.equal(state.messages.length,queueBeforeTransfer);
    await page.locator('#sourceImport').click();
    assert.equal(await page.locator('#ycSource').isVisible(),false);
    await page.locator('#contactFile').setInputFiles({name:'contacts.csv',mimeType:'text/csv',buffer:Buffer.from('Email,Company,Name,Website\r\nnew@sample.com,"Sample, Inc",New Person,sample.com\r\nnew@sample.com,Duplicate,Duplicate,\r\ninvalid,Bad,Invalid,\r\nother@sample.com,"Sample, Inc",Other Person,sample.com')});
    await page.waitForFunction(()=>document.querySelector('#importStatus').textContent.includes('2 unique addresses'));
    assert.match(await page.locator('#importStatus').innerText(),/1 invalid rows skipped · 1 duplicates skipped/);
    await page.locator('#importLoad').click();
    await page.locator('#reviewMail').click();
    assert.equal(await page.locator('.review-message').count(),2);
    assert.match(await page.locator('.body-mail').first().inputValue(),/Hey New/);
    assert.equal(await page.evaluate(()=>parseContactFile('Email\tName\nhi@test.com\t"Multi\nLine"','contacts.tsv')[0].Name),'Multi\nLine');
    await page.locator('#sourceYC').click();
    assert.equal(await page.locator('#ycSource').isVisible(),true);
    assert.match(await page.locator('#list').innerText(),/Acme/);
    await page.setViewportSize({width:390,height:844});
    await page.locator('#token').scrollIntoViewIfNeeded();
    await page.screenshot({path:'/private/tmp/yc-outreach-mail-mobile.png'});
    assert.deepEqual(errors,[]);
    assert.equal(discoveryCalls,1); assert.equal(verifierCalls,1); assert.equal(domainCalls,1); assert.equal(sampleCalls,2); assert.equal(importCalls,2);
    console.log('Browser checks passed: discovery, separate greetings, verification gate, history pagination/colors, bounce view. No mail sent.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
