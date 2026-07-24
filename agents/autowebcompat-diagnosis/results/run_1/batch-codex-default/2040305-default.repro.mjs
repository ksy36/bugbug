import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const reportedURL = 'https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login';
const configurations = [
  {
    name: 'Firefox',
    launch: {
      browser: 'firefox',
      executablePath: '/home/agent/firefox-stable-5kqocmbp/firefox/firefox',
      headless: true,
    },
  },
  {
    name: 'Chrome',
    launch: {
      browser: 'chrome',
      executablePath: '/home/agent/chrome-stable-k3ooddtx/chrome-linux64/chrome',
      headless: true,
      args: ['--no-sandbox'],
    },
  },
];

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function modalState(page) {
  return page.evaluate(() => {
    const modal = document.querySelector('#login');
    const account = document.querySelector('#loginid');
    return {
      hash: location.hash,
      targetId: document.querySelector(':target')?.id ?? null,
      modalMatchesTarget: modal?.matches(':target') ?? false,
      visibility: modal ? getComputedStyle(modal).visibility : null,
      accountVisibility: account ? getComputedStyle(account).visibility : null,
      hasMemberLoginText: modal?.innerText.includes('Member Login') ?? false,
    };
  });
}

async function run(configuration) {
  const browser = await puppeteer.launch(configuration.launch);
  const page = await browser.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  const httpErrors = [];

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text().split('\n')[0]);
  });
  page.on('pageerror', error => consoleErrors.push(`Uncaught: ${error.message}`));
  page.on('requestfailed', request => {
    failedRequests.push(`${request.url()} (${request.failure()?.errorText ?? 'unknown error'})`);
  });
  page.on('response', response => {
    if (response.status() >= 400) httpErrors.push(`${response.status()} ${response.url()}`);
  });

  try {
    await page.goto(reportedURL, {waitUntil: 'networkidle2', timeout: 60000});
    await delay(1500);
    const onLoad = await modalState(page);

    const memberLogin = await page.$('a[title="會員登入"]');
    if (!memberLogin) throw new Error('Member Login link was not found');
    await Promise.all([
      page.waitForNavigation({waitUntil: 'networkidle2', timeout: 60000}),
      page.evaluate(link => link.click(), memberLogin),
    ]);
    await delay(1500);
    const afterClick = await modalState(page);

    const visible = state =>
      state.hash === '#login' &&
      state.modalMatchesTarget &&
      state.targetId === 'login' &&
      state.visibility === 'visible' &&
      state.accountVisibility === 'visible' &&
      state.hasMemberLoginText;
    const hidden = state =>
      state.hash === '#login' &&
      !state.modalMatchesTarget &&
      state.targetId === null &&
      state.visibility === 'hidden' &&
      state.accountVisibility === 'hidden';

    const passed = configuration.name === 'Firefox'
      ? hidden(onLoad) && hidden(afterClick)
      : visible(onLoad) && visible(afterClick);

    console.log(`${configuration.name}: ${passed ? 'PASS' : 'FAIL'}`);
    console.log(`  page load: ${JSON.stringify(onLoad)}`);
    console.log(`  after Member Login click: ${JSON.stringify(afterClick)}`);
    console.log(`  console errors: ${consoleErrors.length ? consoleErrors.join(' | ') : 'none'}`);
    console.log(`  network failures/HTTP errors: ${[...failedRequests, ...httpErrors].length ? [...failedRequests, ...httpErrors].join(' | ') : 'none'}`);
    return {name: configuration.name, passed, onLoad, afterClick};
  } catch (error) {
    console.log(`${configuration.name}: FAIL`);
    console.log(`  ${error.stack ?? error}`);
    return {name: configuration.name, passed: false};
  } finally {
    await browser.close();
  }
}

const results = [];
for (const configuration of configurations) results.push(await run(configuration));

const firefox = results.find(result => result.name === 'Firefox');
const chrome = results.find(result => result.name === 'Chrome');
const differenceReproduced = Boolean(
  firefox?.passed && chrome?.passed &&
  firefox.onLoad.visibility === 'hidden' && chrome.onLoad.visibility === 'visible' &&
  firefox.afterClick.visibility === 'hidden' && chrome.afterClick.visibility === 'visible'
);

console.log(`DIFFERENCE REPRODUCED: ${differenceReproduced ? 'YES' : 'NO'}`);
if (!differenceReproduced) process.exitCode = 1;
