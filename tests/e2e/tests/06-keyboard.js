'use strict';
// Keyboard shortcuts. ONLY the ephemeral ones are driven here. f/a/w/n (follow/audio/
// weather/day-night) persist to settings, so they're deliberately excluded — see TEST_PLAN.md.
module.exports = {
  name: '06 · Keyboard shortcuts (ephemeral)',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    await page.press('/'); await sleep(90);
    t.ok("'/' focuses the search box", await page.evaluate(`document.activeElement&&document.activeElement.id==='filter-search'`));
    await page.evaluate(`document.activeElement&&document.activeElement.blur&&document.activeElement.blur()`);

    for (const [key, modal] of [['e', '#events-modal'], ['s', '#stats-modal'], ['v', '#views-modal']]) {
      await page.press(key); await sleep(160);
      t.ok(`'${key}' opens ${modal}`, await page.open(modal));
      await page.press('Escape'); await sleep(130);
      t.ok(`Esc closes ${modal}`, !(await page.open(modal)));
    }

    await page.press('z'); await sleep(200);   // fit-to-data
    await page.press('r'); await sleep(200);   // recenter-on-receiver
    t.ok('z (fit) + r (recenter) run without throwing', page.exceptions.length === 0);

    await page.press('?'); await sleep(160);
    t.ok("'?' opens keyboard help", (await page.count('[data-close-help]')) > 0);
    await page.press('Escape'); await sleep(120);

    t.eq('no exceptions during shortcut run', page.exceptions.length, 0);
  },
};
