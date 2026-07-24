import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL = 'https://drmeth.com/';
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

const results = {};
for (const [name, launchOptions] of browsers) {
  let browser;
  try {
    browser = await puppeteer.launch(launchOptions);
    const page = await browser.newPage();
    await page.setViewport({width: 1280, height: 900, deviceScaleFactor: 1});
    const consoleMessages = [];
    const failedRequests = [];
    const httpErrors = [];
    page.on('console', message => consoleMessages.push(`${message.type()}: ${message.text()}`));
    page.on('requestfailed', request => failedRequests.push(`${request.failure()?.errorText}: ${request.url()}`));
    page.on('response', response => {
      if (response.status() >= 400) httpErrors.push(`${response.status()}: ${response.url()}`);
    });

    await page.goto(URL, {waitUntil: 'networkidle2', timeout: 120000});
    await page.waitForSelector('iframe[src="/game/index.html"]', {timeout: 30000});
    await new Promise(resolve => setTimeout(resolve, 3000));

    const layout = await page.evaluate(() => {
      const iframe = document.querySelector('iframe[src="/game/index.html"]');
      const rect = iframe.getBoundingClientRect();
      const style = getComputedStyle(iframe);
      const parentStyle = getComputedStyle(iframe.parentElement);
      return {
        iframeWidth: Math.round(rect.width),
        iframeHeight: Math.round(rect.height),
        computedMaxWidth: style.maxWidth,
        computedMaxHeight: style.maxHeight,
        computedAspectRatio: style.aspectRatio,
        parentWidth: Math.round(iframe.parentElement.getBoundingClientRect().width),
        parentHeight: Math.round(iframe.parentElement.getBoundingClientRect().height),
        parentDisplay: parentStyle.display,
        parentAlignItems: parentStyle.alignItems,
        userAgent: navigator.userAgent,
      };
    });

    const expected = name === 'Firefox'
      ? layout.iframeWidth <= 400 && layout.iframeHeight <= 250
      : layout.iframeWidth >= 1000 && layout.iframeHeight >= 600;
    results[name] = {expected, layout};
    console.log(`${expected ? 'PASS' : 'FAIL'} ${name}: game iframe is ${layout.iframeWidth}x${layout.iframeHeight}; parent is ${layout.parentWidth}x${layout.parentHeight}; aspect-ratio=${layout.computedAspectRatio}; max-size=${layout.computedMaxWidth} x ${layout.computedMaxHeight}`);
    console.log(`${name} console messages: ${consoleMessages.length}; failed requests: ${failedRequests.length}; HTTP errors: ${httpErrors.length}`);
  } catch (error) {
    results[name] = {expected: false, error: String(error)};
    console.log(`FAIL ${name}: ${error}`);
  } finally {
    if (browser) await browser.close();
  }
}

const reproduced = results.Firefox?.expected && results.Chrome?.expected &&
  results.Firefox.layout.iframeWidth < results.Chrome.layout.iframeWidth / 2;
console.log(`${reproduced ? 'PASS' : 'FAIL'} final: Firefox/Chrome iframe sizing difference ${reproduced ? 'reproduced' : 'not reproduced'}.`);
if (!reproduced) process.exitCode = 1;
