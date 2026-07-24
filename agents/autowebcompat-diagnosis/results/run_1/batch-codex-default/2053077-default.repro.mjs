import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

// The originally reported requisition ABLAUS31154104ENUSEXTERNAL is now closed.
// This is a currently active requisition using the same real Abbott application/OTP widget.
const url = 'https://www.jobs.abbott/us/en/apply?jobSeqNo=ABLAUS31154902ENUSEXTERNAL&applyChannel=phenomBot&utm_appsource=career-site-bot&step=1&stepname=applicantAcknowledgment';

const configurations = {
  firefox: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-ypblht77/firefox/firefox',
    headless: true,
  },
  chrome: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-df_5dgc4/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

async function reproduce(name, launchOptions) {
  const browser = await puppeteer.launch(launchOptions);
  const page = await browser.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  const applyResponses = [];

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => consoleErrors.push(error.message));
  page.on('requestfailed', request =>
    failedRequests.push(`${request.failure()?.errorText}: ${request.url()}`));
  page.on('response', response => {
    if (response.url().includes('/applySubmit')) applyResponses.push(response.status());
  });

  try {
    await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 90000});
    await page.waitForSelector('#email', {timeout: 60000});
    await new Promise(resolve => setTimeout(resolve, 3000));
    await page.evaluate(() => document.querySelector('#truste-consent-button')?.click());

    // A fresh address is needed because Abbott treats a reused address as an OTP resend.
    const email = `webcompat2053077${name}${Date.now()}@mail.com`;
    await page.type('#email', email);
    await page.evaluate(() =>
      [...document.querySelectorAll('button')]
        .find(button => button.textContent.trim() === 'Get OTP')?.click());

    await page.waitForFunction(
      () => document.querySelectorAll('.otp-field-input').length === 6,
      {timeout: 30000});

    const otpInputs = await page.$$('.otp-field-input');
    // One digit is enough to check entry without submitting a guessed six-digit OTP.
    await otpInputs[0].type('1');

    const observation = await page.evaluate(() => {
      const inputs = [...document.querySelectorAll('.otp-field-input')];
      return {
        count: inputs.length,
        types: inputs.map(input => input.type),
        values: inputs.map(input => input.value),
        appearances: inputs.map(input => getComputedStyle(input).appearance),
        webkitSpinAppearances: inputs.map(input => getComputedStyle(input, '::-webkit-inner-spin-button').appearance),
        successMessage: document.body.innerText.includes('OTP is sent successfully'),
      };
    });

    // The site's spinner-removal rule targets only ::-webkit-inner-spin-button.
    // Chrome exposes that pseudo-element with appearance:none; Firefox does not,
    // so its native up/down controls remain visible despite appearance:none on the input.
    const nativeNumberControls = observation.appearances.every(value => value === 'none') &&
      observation.webkitSpinAppearances.every(value => value === '');
    const plainDigitBoxes = observation.webkitSpinAppearances.every(value => value === 'none');
    const commonChecks = observation.count === 6 &&
      observation.types.every(type => type === 'number') &&
      observation.values[0] === '1' && observation.successMessage &&
      applyResponses.some(status => status === 200);
    const passed = commonChecks && (name === 'firefox' ? nativeNumberControls : plainDigitBoxes);

    console.log(`${name.toUpperCase()} ${passed ? 'PASS' : 'FAIL'}: ${JSON.stringify(observation)}`);
    console.log(`${name.toUpperCase()} evidence: applySubmit statuses=${applyResponses.join(',')}; consoleErrors=${consoleErrors.length}; failedRequests=${failedRequests.length}`);
    return {passed, observation};
  } catch (error) {
    console.log(`${name.toUpperCase()} FAIL: ${error.stack || error}`);
    return {passed: false, error: String(error)};
  } finally {
    await browser.close();
  }
}

const firefox = await reproduce('firefox', configurations.firefox);
const chrome = await reproduce('chrome', configurations.chrome);
const differenceReproduced = firefox.passed && chrome.passed &&
  firefox.observation.webkitSpinAppearances.join(',') !== chrome.observation.webkitSpinAppearances.join(',');
console.log(`DIFFERENCE ${differenceReproduced ? 'REPRODUCED' : 'NOT REPRODUCED'}: Firefox uses native number controls; Chrome uses plain OTP boxes.`);
if (!differenceReproduced) process.exitCode = 1;
