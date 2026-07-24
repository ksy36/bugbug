// Reproduction for webcompat report 2053077
// "www.jobs.abbott - OTP entry fields appear as drop-downs"
//
// Root cause: the 6 OTP entry cells are <input type="number" class="otp-field-input">,
// each only ~25px wide, styled with CSS `appearance:none` (+ -webkit-appearance:none)
// to hide the native number spin-buttons.
//   * Chrome honours `appearance:none` and removes the spin-buttons, so the field's
//     content box keeps its width (clientWidth ~= 23) and the typed digit is shown.
//   * Firefox does NOT remove the <input type=number> spin-buttons for `appearance:none`
//     (only `appearance:textfield` / -moz-appearance:textfield does). The spin-box is
//     laid out inside the 25px cell and consumes the ENTIRE content box, so
//     clientWidth === 0. The cell therefore shows only the up/down spinner ("drop-down")
//     and the entered digit has no room to render -> "numbers are not displayed".
//
// Observable, deterministic divergence asserted below:
//   Firefox: every OTP <input type=number appearance:none> has clientWidth === 0
//   Chrome : every OTP <input type=number appearance:none> has clientWidth  >  0

import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const puppeteer = require('/app/node/node_modules/puppeteer');

const URL =
  'https://www.jobs.abbott/us/en/apply?jobSeqNo=ABLAUS31152679ENUSEXTERNAL&step=1&stepname=applicantAcknowledgment';

const FIREFOX = {
  name: 'firefox',
  launch: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-oo6huhlg/firefox/firefox',
    headless: true,
  },
};
const CHROME = {
  name: 'chrome',
  launch: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-zs5riqik/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Unique email each run so a single Get-OTP click per browser does not trip the
// site's per-request rate limiter.
function freshEmail() {
  const n = Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
  return `qa.${n}@example.com`;
}

async function measure(cfg) {
  const browser = await puppeteer.launch(cfg.launch);
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1400, height: 1000 });
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });

    // The email field is present at load, but the SPA needs a moment to wire up.
    await page.waitForSelector('#email', { timeout: 30000 });
    await sleep(2500);

    const email = freshEmail();
    await page.click('#email');
    await page.type('#email', email, { delay: 20 });

    // Click "Get OTP" (button.otp-send-btn).
    await page.waitForSelector('button.otp-send-btn', { timeout: 15000 });
    await page.click('button.otp-send-btn');

    // Wait for the 6 OTP entry cells to be injected.
    await page.waitForSelector('input.otp-field-input', { timeout: 25000 });
    await sleep(1200); // let layout settle

    const fields = await page.evaluate(() => {
      const els = [...document.querySelectorAll('input.otp-field-input')];
      return els.map((i) => ({
        id: i.id,
        type: i.type,
        appearance: getComputedStyle(i).appearance,
        offsetWidth: i.offsetWidth,
        clientWidth: i.clientWidth, // content-box width; 0 in FF when spin-box eats it
      }));
    });

    return { email, fields };
  } finally {
    await browser.close();
  }
}

function analyse(name, data) {
  const { fields } = data;
  const count = fields.length;
  const allNumber = count > 0 && fields.every((f) => f.type === 'number');
  const allAppearanceNone = count > 0 && fields.every((f) => f.appearance === 'none');
  // Digit is hidden when the content box has (essentially) no width.
  const digitHidden = count > 0 && fields.every((f) => f.clientWidth <= 1);
  const digitVisible = count > 0 && fields.every((f) => f.clientWidth > 1);

  console.log(`\n[${name}] triggered OTP with ${data.email}`);
  console.log(`[${name}] OTP cells found: ${count}`);
  console.log(`[${name}] all <input type=number>: ${allNumber}`);
  console.log(`[${name}] all computed appearance:none: ${allAppearanceNone}`);
  console.log(
    `[${name}] per-cell offsetWidth/clientWidth: ` +
      fields.map((f) => `${f.offsetWidth}/${f.clientWidth}`).join('  ')
  );

  return { count, allNumber, digitHidden, digitVisible };
}

async function main() {
  if (process.argv.includes('--chrome-only')) {
    // (debug helper, not used by default)
  }

  console.log('Reproducing webcompat 2053077 against the real site:\n' + URL);

  const ffData = await measure(FIREFOX);
  const crData = await measure(CHROME);

  const ff = analyse('firefox', ffData);
  const cr = analyse('chrome', crData);

  // Firefox reproduces the bug: number spinner hides the digit (clientWidth 0).
  const firefoxBug = ff.count === 6 && ff.allNumber && ff.digitHidden;
  // Chrome works: digit has room to render (clientWidth > 0).
  const chromeOk = cr.count === 6 && cr.allNumber && cr.digitVisible;

  console.log(
    `\nFIREFOX: ${firefoxBug ? 'PASS' : 'FAIL'} - ` +
      (firefoxBug
        ? 'OTP number cells render the native spin-button and the content box has clientWidth 0 (digit not displayed) -> bug reproduced'
        : 'expected clientWidth 0 on all OTP cells but did not observe it')
  );
  console.log(
    `CHROME:  ${chromeOk ? 'PASS' : 'FAIL'} - ` +
      (chromeOk
        ? 'OTP number cells hide the spin-button; content box has width (digit displayed) -> works as expected'
        : 'expected clientWidth > 0 on all OTP cells but did not observe it')
  );

  const reproduced = firefoxBug && chromeOk;
  console.log(
    `\nDIFFERENCE REPRODUCED: ${reproduced ? 'YES' : 'NO'} - Firefox clientWidth=` +
      `${ffData.fields.map((f) => f.clientWidth).join(',')} vs Chrome clientWidth=` +
      `${crData.fields.map((f) => f.clientWidth).join(',')}`
  );

  process.exit(reproduced ? 0 : 1);
}

main().catch((err) => {
  console.error('Reproduction run failed:', err);
  process.exit(2);
});
