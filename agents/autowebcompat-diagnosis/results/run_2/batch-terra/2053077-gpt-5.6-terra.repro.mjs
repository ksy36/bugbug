import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const url = 'https://www.jobs.abbott/us/en/apply?jobSeqNo=ABLAUS31152679ENUSEXTERNAL&step=1&stepname=applicantAcknowledgment';
const email = 'test34@mail.com'; // The address supplied in the report.

const launches = [
  ['Firefox', { browser: 'firefox', executablePath: '/home/agent/firefox-stable-ty_u3t0t/firefox/firefox', headless: true }],
  ['Chrome', { browser: 'chrome', executablePath: '/home/agent/chrome-stable-k9c6oeka/chrome-linux64/chrome', headless: true, args: ['--no-sandbox'] }],
];

async function reproduce(name, options) {
  const browser = await puppeteer.launch(options);
  const page = await browser.newPage();
  const consoleMessages = [];
  const failedRequests = [];
  page.on('console', m => consoleMessages.push(`${m.type()}: ${m.text()}`));
  page.on('requestfailed', r => failedRequests.push(`${r.method()} ${r.url()} :: ${r.failure()?.errorText}`));
  try {
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
    await page.locator('#email').fill(email);
    await page.locator('button.otp-send-btn').click();
    await new Promise(resolve => setTimeout(resolve, 3500));
    const evidence = await page.evaluate(() => ({
      error: document.body.innerText.includes('Error while resending the OTP.'),
      otpControls: [...document.querySelectorAll('input, select')]
        .filter(e => e.offsetParent && /otp|verification|code/i.test(`${e.id} ${e.name} ${e.className} ${e.outerHTML}`))
        .map(e => ({ tag: e.tagName, type: e.getAttribute('type'), value: e.value, className: e.className })),
      ua: navigator.userAgent,
      numberInputSupported: (() => { const input = document.createElement('input'); input.type = 'number'; return input.type === 'number'; })(),
    }));
    const actionCompleted = await page.$eval('#email', e => e.value === 'test34@mail.com');
    console.log(`${name}: ${actionCompleted ? 'PASS' : 'FAIL'} - entered email and clicked Get OTP; error=${evidence.error}; OTP controls=${JSON.stringify(evidence.otpControls)}`);
    console.log(`${name}: console errors=${JSON.stringify(consoleMessages.filter(x => x.startsWith('error:')).slice(0, 8))}`);
    console.log(`${name}: failed requests=${JSON.stringify(failedRequests.slice(0, 8))}`);
    return evidence;
  } finally {
    await browser.close();
  }
}

const results = new Map();
for (const [name, options] of launches) results.set(name, await reproduce(name, options));
const firefox = results.get('Firefox');
const chrome = results.get('Chrome');
const differenceReproduced = JSON.stringify(firefox.otpControls) !== JSON.stringify(chrome.otpControls) &&
  firefox.otpControls.some(x => x.tag === 'SELECT') && chrome.otpControls.some(x => x.tag === 'INPUT');
console.log(`Difference reproduced: ${differenceReproduced ? 'YES' : 'NO'}`);
if (differenceReproduced) process.exitCode = 0;
