'use strict';
// The detail-panel close control (the bug this suite was born from): select an aircraft,
// dismiss it via ✕ and Esc, verify modal-layering, and confirm the mobile slide-in/out.
module.exports = {
  name: '04 · Detail panel open/close',
  viewport: 'desktop',
  async run(page, t, { sleep, VIEWPORTS }) {
    if ((await page.count('#aircraft-list .aircraft-row')) === 0) { t.ok('skipped — no live aircraft', true); return; }

    const desktop = await page.evaluate(`(()=>{
      const R=[];const A=(n,c)=>R.push([n,!!c]);
      const det=document.getElementById('detail'),content=document.getElementById('detail-content'),empty=document.getElementById('detail-empty');
      const rows=()=>[...document.querySelectorAll('#aircraft-list .aircraft-row')];
      A('initial: content hidden',content.hidden===true);
      rows()[0].click();
      A('select: content visible',content.hidden===false);
      A('select: ✕ button exists',!!document.getElementById('detail-close'));
      A('select: .detail gains .open',det.classList.contains('open'));
      document.getElementById('detail-close').click();
      A('✕: content hidden',content.hidden===true);
      A('✕: placeholder visible',empty.hidden===false);
      A('✕: .detail loses .open',!det.classList.contains('open'));
      rows()[0].click();
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      A('Esc closes the panel',content.hidden===true);
      rows()[0].click();
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'e',bubbles:true}));
      const em=document.getElementById('events-modal');
      A('layering: a modal opened over the selection',em&&em.hidden===false);
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      A('layering: Esc#1 closes modal, keeps selection',em.hidden===true&&content.hidden===false);
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      A('layering: Esc#2 deselects',content.hidden===true);
      return R;
    })()`);
    for (const [n, c] of desktop) t.ok('desktop · ' + n, c);

    // Mobile: the panel is a full-screen overlay that slides via translateX on .detail.open.
    // Assert on the element's OWN transform (deterministic, viewport-independent) and wait for
    // the 0.25s slide to actually settle — rect-vs-innerWidth + fixed sleeps flaked under load
    // (innerWidth read mid-viewport-change, and the panel was measured mid-transition).
    const TX = `(()=>{const t=getComputedStyle(document.getElementById('detail')).transform;if(!t||t==='none')return 0;const m=t.match(/matrix\\(([^)]+)\\)/);return m?Math.round(parseFloat(m[1].split(',')[4])):0;})()`;
    const hasOpen = `document.getElementById('detail').classList.contains('open')`;
    await page.setViewport(VIEWPORTS.mobile); await sleep(300);

    await page.click('#aircraft-list .aircraft-row');
    await page.waitFor(`Math.abs(${TX})<2`, { timeout: 4000 }).catch(() => {});
    t.ok('mobile · select adds .detail.open', await page.evaluate(hasOpen));
    t.ok('mobile · select slides panel ON-screen (translateX≈0)', Math.abs(await page.evaluate(TX)) < 5, 'translateX=' + (await page.evaluate(TX)));

    await page.click('#detail-close');
    await page.waitFor(`(${TX})>50`, { timeout: 4000 }).catch(() => {});
    t.ok('mobile · ✕ removes .detail.open', !(await page.evaluate(hasOpen)));
    t.ok('mobile · ✕ slides panel OFF-screen (translateX≈panel width)', (await page.evaluate(TX)) > 50, 'translateX=' + (await page.evaluate(TX)));
    await page.setViewport(VIEWPORTS.desktop);

    t.eq('no exceptions during detail-panel flow', page.exceptions.length, 0);
  },
};
