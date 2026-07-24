import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const reportedUrl = 'https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login';

const browsers = [
  {
    name: 'Firefox',
    launchOptions: {
      browser: 'firefox',
      executablePath: '/home/agent/firefox-stable-8mjlhgkg/firefox/firefox',
      headless: true,
    },
    expectVisible: false,
  },
  {
    name: 'Chrome',
    launchOptions: {
      browser: 'chrome',
      executablePath: '/home/agent/chrome-stable-nm5pfh94/chrome-linux64/chrome',
      headless: true,
      args: ['--no-sandbox'],
    },
    expectVisible: true,
  },
];

async function inspectLogin(page) {
  return page.evaluate(() => {
    const modal = document.querySelector('#login');
    if (!modal) {
      return {
        exists: false,
        hash: location.hash,
        targetId: document.querySelector(':target')?.id ?? null,
      };
    }

    const style = getComputedStyle(modal);
    return {
      exists: true,
      hash: location.hash,
      targetId: document.querySelector(':target')?.id ?? null,
      matchesTarget: modal.matches(':target'),
      visibility: style.visibility,
      opacity: Number.parseFloat(style.opacity),
      display: style.display,
      visible: style.visibility === 'visible' && Number.parseFloat(style.opacity) > 0,
      memberLoginTextPresent: modal.textContent.includes('Member Login'),
      vueVersion: window.Vue?.version ?? null,
      loginIdCount: document.querySelectorAll('[id="login"]').length,
    };
  });
}

async function activateMemberLogin(page) {
  const navigation = page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 30_000 });
  const clicked = await page.evaluate(() => {
    const link = [...document.querySelectorAll('a')]
      .find(element => element.textContent.trim() === '會員登入');
    if (!link) {
      return false;
    }
    link.click();
    return true;
  });

  if (!clicked) {
    throw new Error('Could not find the Member Login link');
  }

  await navigation;
  await page.waitForSelector('#login', { timeout: 30_000 });
  await new Promise(resolve => setTimeout(resolve, 1_000));
}

async function runBrowser(config) {
  const browser = await puppeteer.launch(config.launchOptions);
  const page = await browser.newPage();
  const consoleErrors = [];
  const failedRequests = [];

  page.on('console', message => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text());
    }
  });
  page.on('requestfailed', request => {
    failedRequests.push(`${request.failure()?.errorText ?? 'failed'} ${request.url()}`);
  });

  try {
    await page.goto(reportedUrl, { waitUntil: 'networkidle2', timeout: 60_000 });
    await page.waitForSelector('#login', { timeout: 30_000 });
    await new Promise(resolve => setTimeout(resolve, 1_000));
    const initial = await inspectLogin(page);

    await activateMemberLogin(page);
    const afterClick = await inspectLogin(page);

    const expectedState = [initial, afterClick].every(result =>
      result.exists &&
      result.memberLoginTextPresent &&
      result.visible === config.expectVisible &&
      result.matchesTarget === config.expectVisible &&
      result.targetId === (config.expectVisible ? 'login' : null)
    );

    const status = expectedState ? 'PASS' : 'FAIL';
    const expectation = config.expectVisible
      ? 'popup visible and #login matches :target'
      : 'reported issue reproduced: popup hidden and no :target element';
    console.log(`${config.name} ${status}: ${expectation}`);
    console.log(`${config.name} initial: ${JSON.stringify(initial)}`);
    console.log(`${config.name} after Member Login activation: ${JSON.stringify(afterClick)}`);
    console.log(`${config.name} console errors: ${consoleErrors.length}; failed requests: ${failedRequests.length}`);
    if (consoleErrors.length) {
      console.log(`${config.name} first console error: ${consoleErrors[0].slice(0, 500)}`);
    }

    return { name: config.name, passed: expectedState, initial, afterClick };
  } catch (error) {
    console.log(`${config.name} FAIL: ${error.stack ?? error}`);
    return { name: config.name, passed: false, error: String(error) };
  } finally {
    await browser.close();
  }
}

const results = [];
for (const config of browsers) {
  results.push(await runBrowser(config));
}

const firefox = results.find(result => result.name === 'Firefox');
const chrome = results.find(result => result.name === 'Chrome');
const differenceReproduced = Boolean(
  firefox?.passed &&
  chrome?.passed &&
  !firefox.initial.visible &&
  chrome.initial.visible &&
  !firefox.afterClick.visible &&
  chrome.afterClick.visible
);

console.log(`FINAL DIFFERENCE REPRODUCED: ${differenceReproduced ? 'YES' : 'NO'}`);
if (!differenceReproduced) {
  process.exitCode = 1;
}
