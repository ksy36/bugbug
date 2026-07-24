import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const url = 'https://drmeth.com/';
const launchOptions = {
  firefox: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-ty_u3t0t/firefox/firefox',
    headless: true,
  },
  chrome: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-k9c6oeka/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

async function reproduce(browserName) {
  const browser = await puppeteer.launch(launchOptions[browserName]);
  const page = await browser.newPage();
  const consoleMessages = [];
  const failedRequests = [];
  page.on('console', message => consoleMessages.push(`${message.type()}: ${message.text()}`));
  page.on('requestfailed', request => failedRequests.push(`${request.url()} (${request.failure()?.errorText})`));

  try {
    await page.setViewport({ width: 1366, height: 768, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('iframe[src*="/game/"]', { timeout: 30000 });
    await page.waitForFunction(() => {
      const frame = document.querySelector('iframe[src*="/game/"]');
      return frame?.contentDocument?.querySelector('#WRAPPER');
    }, { timeout: 30000 });

    const metrics = await page.evaluate(() => {
      const frame = document.querySelector('iframe[src*="/game/"]');
      const rect = frame.getBoundingClientRect();
      const doc = frame.contentDocument;
      const wrapper = doc.querySelector('#WRAPPER').getBoundingClientRect();
      return {
        viewport: [innerWidth, innerHeight],
        frame: { width: rect.width, height: rect.height },
        gameViewport: [frame.contentWindow.innerWidth, frame.contentWindow.innerHeight],
        wrapper: { width: wrapper.width, height: wrapper.height },
        aspectRatio: getComputedStyle(frame).aspectRatio,
        maxHeight: getComputedStyle(frame).maxHeight,
      };
    });

    const firefoxBroken = metrics.frame.width <= 310 && metrics.frame.height <= 180;
    const chromeWorking = metrics.frame.width >= 1000 && metrics.frame.height >= 560;
    const passed = browserName === 'firefox' ? firefoxBroken : chromeWorking;
    console.log(`${browserName.toUpperCase()} ${passed ? 'PASS' : 'FAIL'}: iframe ${metrics.frame.width.toFixed(2)}x${metrics.frame.height.toFixed(2)}, game viewport ${metrics.gameViewport.join('x')}`);
    console.log(`${browserName.toUpperCase()} evidence: aspect-ratio=${metrics.aspectRatio}, max-height=${metrics.maxHeight}, console=${consoleMessages.length}, failedRequests=${failedRequests.length}`);
    if (!passed) console.log(`${browserName.toUpperCase()} observed metrics: ${JSON.stringify(metrics)}`);
    return { passed, metrics, consoleMessages, failedRequests };
  } finally {
    await browser.close();
  }
}

const firefox = await reproduce('firefox');
const chrome = await reproduce('chrome');
const differenceReproduced = firefox.passed && chrome.passed && firefox.metrics.frame.width < chrome.metrics.frame.width / 3;
console.log(`DIFFERENCE REPRODUCED: ${differenceReproduced ? 'YES' : 'NO'}`);
if (!differenceReproduced) process.exitCode = 1;
