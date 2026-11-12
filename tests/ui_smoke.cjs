// Run against the local app. API fixtures prevent changes to the user's courses,
// settings and download folder. Requires Playwright and an installed Chromium.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const output = process.env.UI_OUTPUT || path.join(os.tmpdir(), 'gclassfetch-ui-check');
fs.mkdirSync(output, { recursive: true });
const origin = process.env.APP_URL || 'http://localhost:5328';
const courses = [
  {id: 'c1', name: 'Design and Analysis of Algorithms', section: 'Semester III', alternateLink: 'https://classroom.google.com/c/c1'},
  {id: 'c2', name: 'Linear Algebra', section: 'Semester III'},
  {id: 'c3', name: 'Probability and Random Processes', section: 'A course with a long section name to check narrow screens'},
];
const idle = {running:false, phase:'idle', run_id:'', log:[], issues:[], courses:{}};
const finished = {...idle, phase:'done-warnings', run_id:'test-run', report_ready:true, total_files:4, done_files:3, skipped_files:1, failed_files:1, linked_files:1,
  start_time:Date.now()/1000-5, end_time:Date.now()/1000, done_bytes:8000,
  courses:{c1:{name:courses[0].name, status:'done-warnings', total:4, done:3, failed:1}},
  issues:[{cid:'c1', title:'Lecture notes.pdf', reason:'Google did not allow access. Check your college account.', url:'https://drive.google.com/open?id=test',kind:'attention'},
    {cid:'c1',title:'Lecture recording',reason:'Online resource. No offline copy.',url:'https://youtube.com/watch?v=test',kind:'link'}]};

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const errors = [];
    for (const width of [360, 768, 1440]) {
      const page = await browser.newPage({viewport:{width,height:950}});
      page.on('pageerror', e => errors.push(e.message));
      let status = idle, posted;
      await page.route('**/config', route => route.fulfill({json:{dest:'C:\\School\\Classroom',workers:8,options:{}}}));
      await page.route('**/courses?*', route => route.fulfill({json:courses}));
      await page.route('**/status?*', route => route.fulfill({json:status}));
      await page.route('**/status', route => route.fulfill({json:status}));
      await page.route('**/start/*', route => {posted=route.request().postDataJSON();status=finished;return route.fulfill({json:{status:'ok'}});});
      await page.goto(origin);
      await page.locator('.course-row').first().waitFor();
      assert.equal(await page.locator('.course-row').count(),3);
      assert.equal(await page.locator('#downloadButton').isDisabled(),true);
      await page.locator('#search').fill('no-match');
      await page.locator('#emptySearch').waitFor({state:'visible'});
      await page.locator('#clearSearch').click();
      await page.locator('#search').fill('Algebra');
      await page.locator('#selectAll').check();
      assert.match(await page.locator('#downloadButton').innerText(),/Download 1 course/);
      await page.locator('#search').fill('');
      await page.locator('#selectAll').check();
      assert.match(await page.locator('#downloadButton').innerText(),/Download 3 courses/);
      await page.locator('#downloadButton').click();
      await page.locator('#phaseTitle').filter({hasText:'Finished with items to review'}).waitFor();
      assert.equal(posted.workers,8);
      assert.equal(posted.options.export_docs,true);
      assert.equal(await page.locator('#savedCount').innerText(),'2');
      assert.equal(await page.locator('#existingCount').innerText(),'1');
      assert.equal(await page.locator('#attentionCount').innerText(),'1');
      assert.equal(await page.locator('#reviewList .review-item').count(),1);
      const original = page.locator('#reviewList a');
      await original.focus();
      await page.waitForTimeout(1500);
      assert.equal(await original.evaluate(el=>el===document.activeElement),true,'Polling must preserve review keyboard focus');
      await page.locator('[data-review="link"]').click();
      assert.match(await page.locator('#reviewList').innerText(),/Lecture recording/);
      await page.locator('#settings summary').click();
      await page.locator('#activity summary').click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth),false,`Overflow at ${width}px`);
      await page.screenshot({path:path.join(output,`review-${width}.png`),fullPage:true});
      // Busy state locks the settings and course selection; stopping is visible.
      status = {...finished,running:true,phase:'scanning'};
      await page.locator('#stopButton').waitFor({state:'visible'});
      assert.equal(await page.locator('#destination').isDisabled(),true);
      assert.equal(await page.locator('.course-choice input').first().isDisabled(),true);
      assert.equal(await page.locator('#progress').getAttribute('value'),null);
      await page.close();
      console.log(`UI interactions and layout passed at ${width}px`);
    }
    const page = await browser.newPage();
    await page.route('**/config', route => route.fulfill({json:{dest:'',workers:8,options:{}}}));
    await page.route('**/courses?*', route => route.fulfill({status:500,json:{error:'Google is temporarily unavailable.'}}));
    await page.route('**/status?*', route => route.fulfill({json:idle}));
    await page.goto(origin); await page.getByText(/Google is temporarily unavailable/).waitFor();
    assert.equal(await page.locator('#downloadButton').isDisabled(),true);
    assert.equal(await page.locator('#refreshButton').isEnabled(),true);
    await page.close();
    assert.deepEqual(errors,[]);
    console.log('Course load failure recovery passed; no JavaScript errors.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
