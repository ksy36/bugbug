// Reproduction for webcompat report 2040305
// Site: https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login
//
// Reported bug: the "Member Login" pop-up (a `<div id="login" class="modal">`)
// fails to appear in Firefox but works in Chrome.
//
// Root cause established during investigation: the modal is shown purely via CSS
//   .modal          { opacity: 0; visibility: hidden; }
//   .modal:target   { opacity: 1; visibility: visible; }
// The page is loaded with the fragment "#login". The `#login` element is
// (re)created by the page's Vue 2.5.17 app after the initial fragment navigation.
// Chrome re-evaluates `:target` when the matching element appears, so `#login`
// matches `:target` and the modal is visible. Firefox does NOT retroactively mark
// the dynamically-created element as `:target`, so `.modal:target` never applies
// and the modal stays hidden (opacity:0 / visibility:hidden).
//
// This script drives the REAL site in both Firefox and Chrome, waits for the Vue
// app to render the `#login` modal, then asserts the modal's *visible* state
// (computed opacity/visibility) and whether `#login` matches `:target`.
//
// Run: NODE_PATH=/app/node/node_modules node 2040305-default.repro.mjs

import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL = 'https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login';

const FIREFOX = {
  name: 'Firefox',
  opts: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-jthjx0v5/firefox/firefox',
    headless: true,
  },
};

const CHROME = {
  name: 'Chrome',
  opts: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-q92ogox_/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

// Probe the #login modal's *effective* visibility state.
function probe() {
  const el = document.querySelector('#login');
  if (!el) return { present: false };
  const cs = getComputedStyle(el);
  let isTarget = false;
  try { isTarget = el.matches(':target'); } catch (e) { /* ignore */ }
  const visible = cs.visibility === 'visible' && parseFloat(cs.opacity) > 0.5;
  return {
    present: true,
    isTarget,
    visibility: cs.visibility,
    opacity: cs.opacity,
    visible,
    hash: location.hash,
  };
}

async function run(browserDef) {
  const browser = await puppeteer.launch(browserDef.opts);
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1366, height: 900 });

    try {
      await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } catch (e) {
      // Some ad/analytics requests keep the connection busy; DOMContentLoaded
      // may still fire late. Continue and rely on the polling below.
      console.log(`  [${browserDef.name}] goto note: ${e.message}`);
    }

    // Wait for the Vue app to (re)create the #login modal.
    await page.waitForSelector('#login', { timeout: 30000 }).catch(() => {});

    // Poll a few seconds to let Vue finish rendering and any :target /
    // animation settle, then read the final effective state.
    let state = null;
    for (let i = 0; i < 20; i++) {
      state = await page.evaluate(probe);
      if (state.present && state.visible) break; // reached the "working" state early
      await new Promise((r) => setTimeout(r, 500));
    }

    console.log(`  [${browserDef.name}] #login present=${state.present} ` +
      `visible=${state.visible} (visibility=${state.visibility}, opacity=${state.opacity}) ` +
      `matches(:target)=${state.isTarget} hash=${state.hash}`);

    return state;
  } finally {
    await browser.close();
  }
}

const results = {};
for (const def of [FIREFOX, CHROME]) {
  console.log(`\n=== ${def.name} ===`);
  const state = await run(def);
  const shown = !!(state && state.present && state.visible);
  results[def.name] = shown;

  if (def.name === 'Firefox') {
    // Bug reproduces when the modal does NOT show in Firefox.
    console.log(`  ${def.name}: ${shown ? 'FAIL (modal shown - bug NOT reproduced)'
      : 'PASS (Member Login modal did NOT appear - bug reproduced)'}`);
  } else {
    // Chrome is the reference: modal is expected to show.
    console.log(`  ${def.name}: ${shown ? 'PASS (Member Login modal appeared as expected)'
      : 'FAIL (modal did NOT appear - unexpected)'}`);
  }
}

const firefoxBroken = results.Firefox === false;
const chromeWorks = results.Chrome === true;
const reproduced = firefoxBroken && chromeWorks;

console.log('\n=== SUMMARY ===');
console.log(`Firefox modal shown: ${results.Firefox}`);
console.log(`Chrome  modal shown: ${results.Chrome}`);
console.log(
  reproduced
    ? 'RESULT: DIFFERENCE REPRODUCED - Member Login pop-up appears in Chrome but not in Firefox.'
    : 'RESULT: difference NOT reproduced.'
);

process.exit(reproduced ? 0 : 1);
