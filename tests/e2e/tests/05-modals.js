'use strict';
// Open/close each modal via its trigger, ✕, and Esc; exercise the events + stats tabs and
// the analytics range picker. All ephemeral (opening modals doesn't persist anything).
const MODALS = [
  ['#open-events', '#events-modal', '[data-close-events]'],
  ['#open-stats', '#stats-modal', '[data-close-stats]'],
  ['#open-views', '#views-modal', '[data-close-views]'],
  ['#open-settings', '#settings-modal', '#settings-modal [data-close]'],
];

module.exports = {
  name: '05 · Modals open/close/tabs',
  viewport: 'desktop',
  async run(page, t, { sleep }) {
    for (const [open, modal, close] of MODALS) {
      await page.click(open); await sleep(160);
      t.ok(`${modal} opens`, await page.open(modal));
      await page.click(close); await sleep(140);
      t.ok(`${modal} closes via ✕`, !(await page.open(modal)));
      await page.click(open); await sleep(140);
      await page.press('Escape'); await sleep(140);
      t.ok(`${modal} closes via Esc`, !(await page.open(modal)));
    }

    await page.click('#open-events'); await sleep(160);
    for (const tab of ['emergency', 'military', 'watchlist', 'all']) {
      const ok = await page.evaluate(`(()=>{const b=document.querySelector('[data-events-tab=${JSON.stringify(tab)}]');if(!b)return false;b.click();return true;})()`);
      t.ok(`events tab '${tab}' clickable`, ok);
      await sleep(60);
    }
    await page.press('Escape'); await sleep(120);

    await page.click('#open-stats'); await sleep(180);
    for (const tab of ['today', 'daily', 'coverage', 'leaderboard', 'records', 'bookmarks', 'analytics', 'health']) {
      const ok = await page.evaluate(`(()=>{const b=document.querySelector('[data-stats-tab=${JSON.stringify(tab)}]');if(!b)return false;b.click();return true;})()`);
      t.ok(`stats tab '${tab}' clickable`, ok);
      await sleep(90);
    }
    for (const r of ['24h', '7d', '30d', 'all']) {
      const ok = await page.evaluate(`(()=>{const b=document.querySelector('[data-range=${JSON.stringify(r)}]');if(!b)return false;b.click();return true;})()`);
      t.ok(`analytics range '${r}' clickable`, ok);
      await sleep(120);
    }
    await page.press('Escape'); await sleep(120);

    await page.press('?'); await sleep(160);
    t.ok("keyboard-help opens via '?'", (await page.count('[data-close-help]')) > 0);
    await page.press('Escape'); await sleep(120);

    t.eq('no exceptions during modal cycling', page.exceptions.length, 0);
  },
};
