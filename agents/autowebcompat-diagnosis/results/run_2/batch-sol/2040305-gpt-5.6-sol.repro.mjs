import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL = 'https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login';
const browsers = [
  ['Firefox', {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-nm1tux0e/firefox/firefox',
    headless: true,
  }],
  ['Chrome', {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-0kz1hs1i/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  }],
];

async function modalState(page) {
  return page.evaluate(() => {
    const modal = document.querySelector('#login');
    const style = modal && getComputedStyle(modal);
    return {
      hash: location.hash,
      exists: Boolean(modal),
      isTarget: Boolean(modal?.matches(':target')),
      selectedTarget: document.querySelector(':target')?.id ?? null,
      visibility: style?.visibility ?? null,
      opacity: style?.opacity ?? null,
      hasLoginForm: Boolean(modal?.querySelector('#loginid, #loginpassword')),
    };
  });
}

async function run(name, launchOptions) {
  const browser = await puppeteer.launch(launchOptions);
  try {
    const page = await browser.newPage();
    const consoleErrors = [];
    const failedRequests = [];
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text().split('\n')[0]);
    });
    page.on('pageerror', error => consoleErrors.push(error.message));
    page.on('requestfailed', request =>
      failedRequests.push(`${request.url()} (${request.failure()?.errorText ?? 'unknown'})`));

    await page.goto(URL, {waitUntil: 'networkidle2', timeout: 60000});
    await new Promise(resolve => setTimeout(resolve, 2000));
    const onLoad = await modalState(page);

    // Click the real Member Login link in the site's user-login control.
    await Promise.all([
      page.waitForNavigation({waitUntil: 'networkidle2', timeout: 60000}),
      page.evaluate(() => document.querySelector('.user-login a')?.click()),
    ]);
    await new Promise(resolve => setTimeout(resolve, 2000));
    const afterClick = await modalState(page);

    const visible = state => state.exists && state.hasLoginForm &&
      state.isTarget && state.visibility === 'visible' && Number(state.opacity) > 0;
    const expectedVisible = name === 'Chrome';
    const pass = visible(onLoad) === expectedVisible && visible(afterClick) === expectedVisible;
    console.log(`${name}: ${pass ? 'PASS' : 'FAIL'} expected modal ${expectedVisible ? 'visible' : 'missing'} on load and after clicking the real Member Login control`);
    console.log(`${name}: onLoad=${JSON.stringify(onLoad)} afterClick=${JSON.stringify(afterClick)}`);
    console.log(`${name}: consoleErrors=${consoleErrors.length} failedRequests=${JSON.stringify(failedRequests)}`);
    return {pass, visibleOnLoad: visible(onLoad), visibleAfterClick: visible(afterClick)};
  } finally {
    await browser.close();
  }
}

const results = {};
for (const [name, options] of browsers) results[name] = await run(name, options);
const reproduced = results.Firefox.pass && results.Chrome.pass &&
  !results.Firefox.visibleOnLoad && !results.Firefox.visibleAfterClick &&
  results.Chrome.visibleOnLoad && results.Chrome.visibleAfterClick;
console.log(`DIFFERENCE REPRODUCED: ${reproduced ? 'YES' : 'NO'}`);
if (!reproduced) process.exitCode = 1;
