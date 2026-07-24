// Repro for webcompat bug 2053077 — www.jobs.abbott OTP entry fields appear as
// drop-downs in Firefox.
//
// Root cause: the OTP entry boxes are six <input type="number"> elements
// (id "0-otp".."5-otp", class "otp-field-input") each pinned to width:25px.
// The site's CSS removes the spinner only for Blink/WebKit:
//     .otp-widget input[type='number']::-webkit-inner-spin-button { -webkit-appearance:none }
// while for Firefox it applies `.otp-widget input[type='number']{ -moz-appearance:none }`.
// On a number input, Firefox only drops the spin box for `appearance:textfield`,
// NOT for `appearance:none`, so the native number spin box stays and — inside the
// fixed 25px box — consumes the entire content area. Result: the boxes look like
// up/down "drop-down" spinners and typed digits are not visible.
//
// Observable, engine-intrinsic divergence used as the assertion:
//   #0-otp .clientWidth  ->  Firefox: 0 (spin box fills the box)   Chrome: > 0 (digit area visible)
//
// Runs the REAL reported site in both browsers.
//
// Run: NODE_PATH=/app/node/node_modules node 2053077-default.repro.mjs

import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL =
  'https://www.jobs.abbott/us/en/apply?jobSeqNo=ABLAUS31152679ENUSEXTERNAL&step=1&stepname=applicantAcknowledgment';

const FIREFOX_PATH = '/home/agent/firefox-stable-jthjx0v5/firefox/firefox';
const CHROME_PATH = '/home/agent/chrome-stable-q92ogox_/chrome-linux64/chrome';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function measureOtpField(name, launchOpts) {
  const browser = await puppeteer.launch(launchOpts);
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1366, height: 900 });

    // The OTP boxes only render after a successful "send OTP" backend response.
    // That endpoint is rate limited, so retry with a fresh unique email if needed.
    for (let attempt = 1; attempt <= 5; attempt++) {
      await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForSelector('#email', { timeout: 30000 });
      await sleep(3500); // let the React OTP widget hydrate

      const email = `wc2053077-${name}-${Date.now()}-${attempt}@example.com`;

      // #email is a React-controlled input: use the native value setter + input event.
      await page.evaluate((val) => {
        const el = document.getElementById('email');
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype,
          'value'
        ).set;
        setter.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      }, email);

      // Poll: click "Get OTP" (once enabled) and wait for the fields to render.
      // Re-click if the first click lands before hydration finishes, and bail
      // early on an explicit rate-limit / error message so we can retry.
      let rendered = false;
      let lastMsg = '(no OTP fields, no message)';
      for (let i = 0; i < 12; i++) {
        const state = await page.evaluate(() => {
          if (document.getElementById('0-otp')) return { done: true };
          const f = document.querySelector('form');
          const t = (f && f.innerText) || '';
          const err = t.match(/Request limit reached[^\n]*|Error while[^\n]*/i);
          if (err) return { err: err[0] };
          const b = [...document.querySelectorAll('button')].find(
            (x) => x.textContent.trim() === 'Get OTP'
          );
          if (b && !b.disabled) b.click();
          return { clickedEnabled: !!(b && !b.disabled) };
        });
        if (state.done) {
          rendered = true;
          break;
        }
        if (state.err) {
          lastMsg = state.err;
          break;
        }
        await sleep(2000);
      }

      if (!rendered) {
        console.log(
          `  [${name}] attempt ${attempt}: OTP fields not shown — "${lastMsg.trim()}". Retrying with a new email...`
        );
        await sleep(4000);
        continue;
      }
      await page.waitForSelector('[id="0-otp"]', { timeout: 5000 });

      const m = await page.evaluate(() => {
        const el = document.getElementById('0-otp');
        const cs = getComputedStyle(el);
        return {
          count: document.querySelectorAll('input[id$="-otp"]').length,
          type: el.type,
          autocomplete: el.getAttribute('autocomplete'),
          offsetW: el.offsetWidth,
          clientW: el.clientWidth,
          scrollW: el.scrollWidth,
          appearance: cs.appearance,
          mozAppearance: cs.getPropertyValue('-moz-appearance'),
        };
      });
      return m;
    }
    return null; // never rendered
  } finally {
    await browser.close();
  }
}

async function main() {
  console.log('=== webcompat 2053077 — jobs.abbott OTP fields render as drop-downs in Firefox ===\n');

  console.log('--- Firefox ---');
  const ff = await measureOtpField('ff', {
    browser: 'firefox',
    executablePath: FIREFOX_PATH,
    headless: true,
  });

  console.log('\n--- Chrome ---');
  const cr = await measureOtpField('cr', {
    browser: 'chrome',
    executablePath: CHROME_PATH,
    headless: true,
    args: ['--no-sandbox'],
  });

  console.log('\n=== Results ===');

  if (!ff || !cr) {
    console.log(
      `Could not render the OTP fields in ${!ff ? 'Firefox' : 'Chrome'} (likely OTP-send rate limiting). ` +
        'Re-run in a few minutes.'
    );
    console.log('\nFINAL: Firefox/Chrome difference NOT demonstrated this run.');
    process.exit(1);
  }

  console.log(
    `Firefox: ${ff.count}x <input type="${ff.type}">  offsetWidth=${ff.offsetW}  clientWidth=${ff.clientW}  scrollWidth=${ff.scrollW}  appearance=${ff.appearance}  -moz-appearance=${ff.mozAppearance}`
  );
  console.log(
    `Chrome : ${cr.count}x <input type="${cr.type}">  offsetWidth=${cr.offsetW}  clientWidth=${cr.clientW}  scrollWidth=${cr.scrollW}  appearance=${cr.appearance}`
  );

  // Firefox reproduces the bug: the native number spin box fills the fixed-width
  // box, leaving zero content width (digit not visible / looks like a dropdown).
  const firefoxBroken = ff.clientW === 0;
  // Chrome is fine: spinners removed, real content width available for the digit.
  const chromeOk = cr.clientW > 0;

  console.log('');
  console.log(
    `Firefox: ${
      firefoxBroken
        ? 'FAIL (bug reproduced) — OTP number input clientWidth=0: native spin box fills the 25px box, so it renders as an up/down "drop-down" and typed digits are not visible.'
        : 'PASS (unexpected) — OTP number input has usable content width.'
    }`
  );
  console.log(
    `Chrome : ${
      chromeOk
        ? 'PASS — OTP number input has usable content width (clientWidth>0); spinners removed, digit visible.'
        : 'FAIL (unexpected) — OTP number input has no content width.'
    }`
  );

  const reproduced = firefoxBroken && chromeOk;
  console.log('');
  console.log(
    `FINAL: Firefox/Chrome difference ${
      reproduced ? 'REPRODUCED' : 'NOT reproduced'
    } (Firefox clientWidth=${ff.clientW} vs Chrome clientWidth=${cr.clientW}).`
  );
  process.exit(reproduced ? 0 : 1);
}

main().catch((e) => {
  console.error('Script error:', e);
  process.exit(2);
});
