import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const url = 'https://www.centuryasia.com.tw/login.html?obj=l&obj1=i&ver=I+DfKKUJFcA=#login';
const browsers = [
  {
    name: 'Firefox',
    options: {
      browser: 'firefox',
      executablePath: '/home/agent/firefox-stable-ty_u3t0t/firefox/firefox',
      headless: true,
    },
  },
  {
    name: 'Chrome',
    options: {
      browser: 'chrome',
      executablePath: '/home/agent/chrome-stable-k9c6oeka/chrome-linux64/chrome',
      headless: true,
      args: ['--no-sandbox'],
    },
  },
];

async function run({ name, options }) {
  const browser = await puppeteer.launch(options);
  try {
    const page = await browser.newPage();
    const consoleMessages = [];
    const failedRequests = [];
    page.on('console', m => consoleMessages.push(`${m.type()}: ${m.text()}`));
    page.on('requestfailed', r => failedRequests.push(`${r.url()} (${r.failure()?.errorText})`));
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('#login');
    await new Promise(resolve => setTimeout(resolve, 1500));

    const initial = await page.evaluate(() => {
      const login = document.querySelector('#login');
      const css = getComputedStyle(login);
      return {
        hash: location.hash,
        targetId: document.querySelector(':target')?.id ?? null,
        matchesTarget: login.matches(':target'),
        visibility: css.visibility,
        opacity: css.opacity,
        memberLoginLink: document.querySelector('a[title="會員登入"]')?.getAttribute('href') ?? null,
      };
    });

    // Reproduce the report's click on the visible Member Login navigation link.
    // It navigates to the site's no-fragment login URL; the initial #login state
    // above is the state in which the cross-browser difference occurs.
    const memberLink = await page.$('a[title="會員登入"]');
    let clickNavigation = null;
    if (memberLink) {
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 10000 }).catch(() => null),
        // The page-wide modal overlay can cover this link in Chrome, so invoke
        // the link's native click method while retaining the site's real URL.
        page.evaluate(() => document.querySelector('a[title="會員登入"]')?.click()),
      ]);
      clickNavigation = await page.evaluate(() => location.href);
    }

    const visible = initial.visibility === 'visible' && Number(initial.opacity) > 0.9;
    console.log(`${name}: ${visible ? 'PASS' : 'FAIL'} — #login visibility=${initial.visibility}, opacity=${initial.opacity}, :target=${initial.targetId}, matches=${initial.matchesTarget}`);
    console.log(`${name}: Member Login click navigated to ${clickNavigation}`);
    if (consoleMessages.length) console.log(`${name}: console ${consoleMessages.slice(0, 3).map(m => m.replace(/\s+/g, ' ').slice(0, 240)).join(' | ')}`);
    if (failedRequests.length) console.log(`${name}: failed requests ${failedRequests.slice(0, 3).join(' | ')}`);
    return { name, visible, initial };
  } finally {
    await browser.close();
  }
}

const results = [];
for (const browser of browsers) results.push(await run(browser));
const firefox = results.find(r => r.name === 'Firefox');
const chrome = results.find(r => r.name === 'Chrome');
const reproduced = firefox && chrome && !firefox.visible && chrome.visible &&
  !firefox.initial.matchesTarget && chrome.initial.matchesTarget;
console.log(`Difference reproduced: ${reproduced ? 'YES' : 'NO'}`);
if (!reproduced) process.exitCode = 1;
