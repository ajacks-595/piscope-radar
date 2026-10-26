'use strict';
/*
 * Tiny assertion + reporting layer for the PiScope browser E2E suite.
 *
 * Each test module exports: { name, viewport?, destructive?, async run(page, t) }
 *   - viewport:   'desktop' (1280x900) | 'mobile' (390x844) | omitted (leave as-is)
 *   - destructive: true  => skipped unless run.js is passed --include-destructive
 *   - run(page, t): drive the page via the cdp.js Page API and record checks on `t`.
 *
 * `t` (a Checks collector) methods all record a {name, pass, detail} row:
 *   t.ok(name, cond, detail)      truthy assertion
 *   t.eq(name, actual, expected)  strict-equal
 *   t.ge / t.gt(name, a, b)       numeric compare
 *   t.has(name, haystack, needle) substring / array-includes
 *   await t.visible(name, page, sel)     element is visible
 *   await t.hidden(name, page, sel)      element absent or display:none
 */

class Checks {
  constructor(moduleName) { this.moduleName = moduleName; this.rows = []; }
  _push(name, pass, detail) { this.rows.push({ name, pass: !!pass, detail: String(detail === undefined ? '' : detail) }); return !!pass; }
  ok(name, cond, detail) { return this._push(name, cond, detail); }
  eq(name, a, b) { return this._push(name, a === b, `got ${JSON.stringify(a)} want ${JSON.stringify(b)}`); }
  ge(name, a, b) { return this._push(name, a >= b, `${a} >= ${b}`); }
  gt(name, a, b) { return this._push(name, a > b, `${a} > ${b}`); }
  has(name, hay, needle) {
    const ok = Array.isArray(hay) ? hay.includes(needle) : String(hay).includes(needle);
    return this._push(name, ok, `looking for ${JSON.stringify(needle)}`);
  }
  async visible(name, page, sel) { return this._push(name, await page.visible(sel), `${sel} visible`); }
  async hidden(name, page, sel) {
    const vis = await page.visible(sel);
    return this._push(name, !vis, `${sel} hidden`);
  }
  get passed() { return this.rows.filter((r) => r.pass).length; }
  get failed() { return this.rows.filter((r) => !r.pass).length; }
}

function report(moduleResults, pageLogs) {
  const RESET = '\x1b[0m', GREEN = '\x1b[32m', RED = '\x1b[31m', DIM = '\x1b[2m', BOLD = '\x1b[1m';
  let totPass = 0, totFail = 0;
  const lines = [];
  for (const mr of moduleResults) {
    if (mr.skipped) { lines.push(`${DIM}— ${mr.name} (skipped: ${mr.skipped})${RESET}`); continue; }
    if (mr.error) { lines.push(`${RED}✗ ${mr.name} — MODULE ERROR: ${mr.error}${RESET}`); totFail++; continue; }
    const c = mr.checks;
    totPass += c.passed; totFail += c.failed;
    const head = c.failed === 0 ? `${GREEN}✓` : `${RED}✗`;
    lines.push(`${head} ${BOLD}${mr.name}${RESET} ${DIM}(${c.passed}/${c.rows.length})${RESET}`);
    for (const r of c.rows) {
      if (r.pass) lines.push(`    ${GREEN}·${RESET} ${r.name}`);
      else lines.push(`    ${RED}✗ ${r.name}${RESET} ${DIM}${r.detail}${RESET}`);
    }
  }
  lines.push('');
  const ce = pageLogs.consoleErrors.length, ex = pageLogs.exceptions.length, le = pageLogs.logErrors.length;
  lines.push(`${BOLD}console.error: ${ce} | uncaught exceptions: ${ex} | browser log errors: ${le}${RESET}`);
  for (const e of pageLogs.exceptions) lines.push(`    ${RED}EXCEPTION:${RESET} ${e}`);
  for (const e of pageLogs.consoleErrors) lines.push(`    ${DIM}console.error:${RESET} ${e}`);
  // browser log errors (e.g. failed photo fetches) are informational — list but don't fail on them.
  for (const e of pageLogs.logErrors.slice(0, 12)) lines.push(`    ${DIM}log:${RESET} ${e}`);
  lines.push('');
  const verdict = (totFail === 0 && ex === 0) ? `${GREEN}${BOLD}PASS${RESET}` : `${RED}${BOLD}FAIL${RESET}`;
  lines.push(`${verdict}  ${totPass} passed, ${totFail} failed across ${moduleResults.filter((m) => !m.skipped).length} modules`);
  return { text: lines.join('\n'), totPass, totFail, exceptions: ex };
}

module.exports = { Checks, report };
