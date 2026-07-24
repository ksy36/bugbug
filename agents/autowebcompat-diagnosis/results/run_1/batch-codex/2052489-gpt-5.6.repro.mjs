import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL = 'https://drmeth.com/';
const VIEWPORT = { width: 1365, height: 768 };

const configurations = [
  {
    name: 'Firefox',
    launch: {
      browser: 'firefox',
      executablePath: '/home/agent/firefox-stable-8mjlhgkg/firefox/firefox',
      headless: true,
    },
  },
  {
    name: 'Chrome',
    launch: {
      browser: 'chrome',
      executablePath: '/home/agent/chrome-stable-nm5pfh94/chrome-linux64/chrome',
      headless: true,
      args: ['--no-sandbox'],
    },
  },
];

async function reproduce(configuration) {
  const browser = await puppeteer.launch(configuration.launch);
  const page = await browser.newPage();
  const consoleErrors = [];
  const failedRequests = [];

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('requestfailed', request => {
    failedRequests.push(`${request.url()} (${request.failure()?.errorText ?? 'unknown'})`);
  });

  try {
    await page.setViewport(VIEWPORT);
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 45_000 });
    await page.waitForSelector('iframe[src*="/game/"]', { timeout: 20_000 });

    const gameFrame = page.frames().find(frame => frame.url().includes('/game/'));
    if (!gameFrame) throw new Error('Game iframe did not load');
    await gameFrame.waitForSelector('#WRAPPER', { timeout: 20_000 });
    await new Promise(resolve => setTimeout(resolve, 2_000));

    const metrics = await page.evaluate(() => {
      const iframe = document.querySelector('iframe[src*="/game/"]');
      const container = iframe.parentElement;
      const iframeRect = iframe.getBoundingClientRect();
      const containerRect = container.getBoundingClientRect();
      const style = getComputedStyle(iframe);
      const frameWindow = iframe.contentWindow;
      const wrapper = iframe.contentDocument.querySelector('#WRAPPER');
      const wrapperRect = wrapper.getBoundingClientRect();

      return {
        userAgent: navigator.userAgent,
        viewport: { width: innerWidth, height: innerHeight },
        iframe: {
          width: iframeRect.width,
          height: iframeRect.height,
          aspectRatio: style.aspectRatio,
          maxWidth: style.maxWidth,
          maxHeight: style.maxHeight,
          classes: iframe.className,
        },
        container: {
          width: containerRect.width,
          height: containerRect.height,
          display: getComputedStyle(container).display,
        },
        game: {
          viewportWidth: frameWindow.innerWidth,
          viewportHeight: frameWindow.innerHeight,
          wrapperWidth: wrapperRect.width,
          wrapperHeight: wrapperRect.height,
        },
      };
    });

    const fillRatio = metrics.iframe.width / metrics.container.width;
    return {
      ...metrics,
      fillRatio,
      consoleErrors,
      failedRequestCount: failedRequests.length,
      failedRequests: failedRequests.slice(0, 5),
    };
  } finally {
    await browser.close();
  }
}

const results = {};
let executionFailed = false;

for (const configuration of configurations) {
  try {
    const result = await reproduce(configuration);
    results[configuration.name] = result;
    console.log(`${configuration.name} metrics: ${JSON.stringify(result)}`);
  } catch (error) {
    executionFailed = true;
    console.log(`${configuration.name}: FAIL - ${error.stack ?? error}`);
  }
}

if (!executionFailed) {
  const firefox = results.Firefox;
  const chrome = results.Chrome;
  const firefoxShowsBrokenSizing = firefox.iframe.width <= 400 && firefox.fillRatio < 0.35;
  const chromeShowsExpectedSizing = chrome.iframe.width >= 900 && chrome.fillRatio > 0.75;
  const measurableDifference = chrome.iframe.width >= firefox.iframe.width * 2.5;

  console.log(`Firefox: ${firefoxShowsBrokenSizing ? 'PASS' : 'FAIL'} - iframe width ${firefox.iframe.width.toFixed(2)}px, filling ${(firefox.fillRatio * 100).toFixed(1)}% of its container`);
  console.log(`Chrome: ${chromeShowsExpectedSizing ? 'PASS' : 'FAIL'} - iframe width ${chrome.iframe.width.toFixed(2)}px, filling ${(chrome.fillRatio * 100).toFixed(1)}% of its container`);

  if (firefoxShowsBrokenSizing && chromeShowsExpectedSizing && measurableDifference) {
    console.log('DIFFERENCE REPRODUCED: Firefox keeps the game iframe near its 300px intrinsic width while Chrome expands it to the available 16:9 area.');
  } else {
    executionFailed = true;
    console.log('DIFFERENCE NOT REPRODUCED: measured iframe sizing did not match the reported Firefox/Chrome divergence.');
  }
}

process.exitCode = executionFailed ? 1 : 0;
