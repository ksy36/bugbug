// Reproduction: www.centuryasia.com.tw "Member Login" pop-up fails to load in Firefox.
//
// Root cause captured by this script:
//   The login modal (#login.modal) is shown purely via the CSS :target rule
//       .modal          { visibility: hidden; opacity: 0; }
//       .modal:target   { visibility: visible; opacity: 1; }
//   The page is loaded with the URL fragment "#login", but the #login element is
//   rendered by Vue AFTER the initial navigation. Chrome re-resolves the fragment
//   target when the matching element is later inserted, so #login matches :target
//   and the modal is visible. Firefox resolves the fragment target only at
//   navigation time (when #login did not yet exist), so :target never matches the
//   late-inserted element and the modal stays visibility:hidden / opacity:0.
//
// Observable divergence asserted below:
//   Chrome  -> #login is visible (visibility:visible, opacity:1, matches(':target')=true)
//   Firefox -> #login is hidden  (visibility:hidden,  opacity:0, matches(':target')=false)

import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const puppeteer = require('/app/node/node_modules/puppeteer');

const URL = 'https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login';

const FIREFOX = {
  name: 'Firefox',
  opts: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-oo6huhlg/firefox/firefox',
    headless: true,
  },
};
const CHROME = {
  name: 'Chrome',
  opts: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-zs5riqik/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

// Probe run inside the page: waits for Vue to render #login, then reports how the
// :target-driven modal ended up computed.
function probe() {
  return new Promise((resolve) => {
    const deadline = Date.now() + 15000;
    const tick = () => {
      const el = document.querySelector('#login.modal');
      // Consider the modal "rendered" once Vue has filled in its content.
      const rendered = el && el.querySelector('.modal-content');
      if (rendered) {
        const cs = getComputedStyle(el);
        let targetMatch = false;
        try { targetMatch = el.matches(':target'); } catch (e) {}
        resolve({
          found: true,
          hash: location.hash,
          visibility: cs.visibility,
          opacity: cs.opacity,
          targetMatch,
        });
        return;
      }
      if (Date.now() > deadline) {
        resolve({ found: false, hash: location.hash });
        return;
      }
      setTimeout(tick, 200);
    };
    tick();
  });
}

async function run(browserDef) {
  const browser = await puppeteer.launch(browserDef.opts);
  try {
    const page = await browser.newPage();
    // The site has a flaky/slow subresource; don't wait for network idle.
    try {
      await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
    } catch (e) {
      // Navigation may report a timeout while the document is already usable.
      console.log(`  [${browserDef.name}] navigation note: ${e.message}`);
    }
    const result = await page.evaluate(probe);
    return result;
  } finally {
    await browser.close();
  }
}

function isVisible(r) {
  return !!(r && r.found && r.visibility === 'visible' && parseFloat(r.opacity) > 0);
}

(async () => {
  const ff = await run(FIREFOX);
  console.log('Firefox result:', JSON.stringify(ff));
  const ffVisible = isVisible(ff);
  // Firefox is expected to FAIL to show the popup (the reported bug).
  console.log(`Firefox: Member Login popup visible = ${ffVisible} ` +
    `-> ${ffVisible ? 'FAIL (expected bug NOT reproduced)' : 'PASS (bug reproduced: popup hidden)'}`);

  const ch = await run(CHROME);
  console.log('Chrome result:', JSON.stringify(ch));
  const chVisible = isVisible(ch);
  // Chrome is expected to correctly show the popup.
  console.log(`Chrome:  Member Login popup visible = ${chVisible} ` +
    `-> ${chVisible ? 'PASS (popup shown as expected)' : 'FAIL (popup unexpectedly hidden)'}`);

  const reproduced = ff.found && ch.found && !ffVisible && chVisible;
  console.log('');
  console.log(`DIFFERENCE REPRODUCED: ${reproduced} ` +
    `(Firefox hides the #login :target modal, Chrome shows it)`);

  if (!reproduced) {
    console.error('ERROR: expected Firefox=hidden and Chrome=visible; divergence not demonstrated.');
    process.exit(1);
  }
  process.exit(0);
})().catch((err) => {
  console.error('Unexpected failure:', err);
  process.exit(2);
});
