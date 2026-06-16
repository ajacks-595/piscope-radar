'use strict';
// Replay mode is ephemeral view-state backed by GET /api/replay/* (no writes). Enter via
// 'p', verify the bar + slider appear, scrub, then return to live.
module.exports = {
  name: '09 · Replay mode (ephemeral)',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    const tl = await page.evaluate(`fetch('/piscope/api/replay/timeline').then(r=>r.json()).catch(()=>null)`);
    t.ok('GET /api/replay/timeline returns data', !!tl, JSON.stringify(tl).slice(0, 90));

    await page.press('p'); await sleep(450);
    const barVisible = await page.visible('#replay-bar');
    t.ok("'p' opens the replay bar", barVisible);
    if (barVisible) {
      const slider = await page.evaluate(`(()=>{const s=document.getElementById('replay-slider');return s?{max:+s.max,val:+s.value}:null;})()`);
      t.ok('replay slider has a range', slider && slider.max >= 0, JSON.stringify(slider));
      await page.evaluate(`(()=>{const s=document.getElementById('replay-slider');if(s){s.value=String(Math.floor((+s.max||0)/2));s.dispatchEvent(new Event('input',{bubbles:true}));}})()`);
      await sleep(450);
      t.ok('replay clock shows a timestamp', (await page.text('#replay-clock')).trim().length > 0);
      await page.click('#replay-live'); await sleep(300);
      await t.hidden('exit replay hides the bar', page, '#replay-bar');
    }
    t.eq('no exceptions during replay', page.exceptions.length, 0);
  },
};
