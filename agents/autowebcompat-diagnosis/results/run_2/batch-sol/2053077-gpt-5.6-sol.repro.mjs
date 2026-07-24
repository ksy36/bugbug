import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const url = 'https://www.jobs.abbott/us/en/apply?jobSeqNo=ABLAUS31152679ENUSEXTERNAL&step=1&stepname=applicantAcknowledgment';

async function reproduce(name, launchOptions) {
  const browser = await puppeteer.launch(launchOptions);
  const page = await browser.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  const postClickResponses = [];
  let clicked = false;

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('requestfailed', request => {
    failedRequests.push(`${request.failure()?.errorText || 'failed'} ${request.url()}`);
  });
  page.on('response', response => {
    if (clicked && /otp|applySubmit|candidate|email/i.test(response.url())) {
      postClickResponses.push(`${response.status()} ${response.url()}`);
    }
  });

  try {
    await page.goto(url, {waitUntil: 'networkidle2', timeout: 120000});
    await page.waitForSelector('#email', {timeout: 90000});

    const consent = await page.$('#truste-consent-button');
    if (consent) await consent.evaluate(element => element.click());

    await page.click('#email');
    await page.type('#email', 'test34@mail.com');
    await page.keyboard.press('Tab');
    await new Promise(resolve => setTimeout(resolve, 500));
    clicked = true;
    await page.click('.otp-send-btn');

    try {
      await page.waitForSelector('.otp-field-input', {timeout: 15000});
    } catch {}

    const otpFields = await page.$$('.otp-field-input');
    if (otpFields.length) {
      await otpFields[0].click();
      await page.keyboard.type('7');
      await new Promise(resolve => setTimeout(resolve, 300));
    }

    const evidence = await page.evaluate(() => ({
      userAgent: navigator.userAgent,
      emailValue: document.querySelector('#email')?.value || '',
      buttonDisabled: document.querySelector('.otp-send-btn')?.disabled ?? null,
      visibleMessage: [...document.querySelectorAll('.error-text,.success-text')]
        .map(element => element.textContent.trim()).filter(Boolean),
      otpFields: [...document.querySelectorAll('.otp-field-input')].map(input => ({
        tag: input.tagName,
        type: input.type,
        value: input.value,
        appearance: getComputedStyle(input).appearance,
        width: input.getBoundingClientRect().width,
        height: input.getBoundingClientRect().height
      }))
    }));

    const issueObserved = evidence.otpFields.length > 0 &&
      (evidence.otpFields[0].value !== '7' || /textfield|menulist|button/i.test(evidence.otpFields[0].appearance));
    console.log(`${name}: ${issueObserved ? 'PASS (reported broken behavior observed)' : 'FAIL (reported broken behavior not observed)'}`);
    console.log(`${name} evidence: ${JSON.stringify({...evidence, postClickResponses, consoleErrors, failedRequests})}`);
    return {name, issueObserved, evidence};
  } catch (error) {
    console.log(`${name}: FAIL (${error.message})`);
    return {name, issueObserved: false, error: error.message};
  } finally {
    await browser.close();
  }
}

const firefox = await reproduce('Firefox', {
  browser: 'firefox',
  executablePath: '/home/agent/firefox-stable-nm1tux0e/firefox/firefox',
  headless: true
});
const chrome = await reproduce('Chrome', {
  browser: 'chrome',
  executablePath: '/home/agent/chrome-stable-0kz1hs1i/chrome-linux64/chrome',
  headless: true,
  args: ['--no-sandbox']
});

const differenceReproduced = firefox.issueObserved && !chrome.issueObserved &&
  firefox.evidence?.otpFields.length > 0 && chrome.evidence?.otpFields.length > 0;
console.log(`Difference reproduced: ${differenceReproduced ? 'YES' : 'NO'}`);
