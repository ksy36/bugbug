import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL = 'https://drmeth.com/';
const configs = {
  Firefox: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-4q4yv48o/firefox/firefox',
    headless: true,
  },
  Chrome: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-69yk9_k2/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

async function reproduce(name, launchOptions) {
  const browser = await puppeteer.launch(launchOptions);
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1365, height: 768 });
    const consoleMessages = [];
    const failedRequests = [];
    const httpErrors = [];
    page.on('console', message => consoleMessages.push(`${message.type()}: ${message.text()}`));
    page.on('requestfailed', request =>
      failedRequests.push(`${request.url()} (${request.failure()?.errorText ?? 'unknown'})`));
    page.on('response', response => {
      if (response.status() >= 400) httpErrors.push(`${response.status()} ${response.url()}`);
    });

    await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 });
    await new Promise(resolve => setTimeout(resolve, 5000));

    const metrics = await page.evaluate(() => {
      const iframe = document.querySelector('iframe[x-ref="gameIframe"]');
      if (!iframe) throw new Error('Game iframe was not found');
      const rect = iframe.getBoundingClientRect();
      const style = getComputedStyle(iframe);
      return {
        iframe: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        container: (() => {
          const r = iframe.parentElement.getBoundingClientRect();
          return { width: r.width, height: r.height };
        })(),
        css: {
          width: style.width,
          height: style.height,
          maxWidth: style.maxWidth,
          maxHeight: style.maxHeight,
          aspectRatio: style.aspectRatio,
          flex: style.flex,
        },
        attributes: { width: iframe.getAttribute('width'), height: iframe.getAttribute('height') },
        userAgent: navigator.userAgent,
      };
    });
    const gameFrame = page.frames().find(frame => frame.url().includes('/game/index'));
    metrics.embeddedViewport = gameFrame
      ? await gameFrame.evaluate(() => ({ width: innerWidth, height: innerHeight }))
      : null;
    metrics.consoleMessages = consoleMessages;
    metrics.failedRequests = failedRequests;
    metrics.httpErrors = httpErrors;

    const passed = name === 'Firefox'
      ? metrics.iframe.width <= 320 && metrics.iframe.height <= 180
      : metrics.iframe.width >= 1200 && metrics.iframe.height >= 680;
    console.log(`${passed ? 'PASS' : 'FAIL'} ${name}: iframe ${metrics.iframe.width.toFixed(1)}x${metrics.iframe.height.toFixed(1)}, embedded viewport ${metrics.embeddedViewport?.width}x${metrics.embeddedViewport?.height}`);
    console.log(`${name} evidence: ${JSON.stringify(metrics)}`);
    return { passed, metrics };
  } finally {
    await browser.close();
  }
}

let firefox;
let chrome;
try {
  firefox = await reproduce('Firefox', configs.Firefox);
  chrome = await reproduce('Chrome', configs.Chrome);
  const difference = firefox.passed && chrome.passed && chrome.metrics.iframe.width > firefox.metrics.iframe.width * 3;
  console.log(`${difference ? 'PASS' : 'FAIL'} FINAL: Firefox/Chrome iframe sizing difference ${difference ? 'reproduced' : 'not reproduced'}.`);
  if (!difference) process.exitCode = 1;
} catch (error) {
  console.error(`FAIL FINAL: ${error.stack ?? error}`);
  process.exitCode = 1;
}
