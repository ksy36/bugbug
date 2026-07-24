import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';
import { inflateSync } from 'node:zlib';

const url = 'https://www.jobs.abbott/us/en/apply?jobSeqNo=ABLAUS31152679ENUSEXTERNAL&step=1&stepname=applicantAcknowledgment';


function countTopInteriorInk(png) {
  let offset = 8;
  let width;
  let height;
  let colorType;
  const idat = [];
  while (offset < png.length) {
    const length = png.readUInt32BE(offset);
    const type = png.toString('ascii', offset + 4, offset + 8);
    const data = png.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      colorType = data[9];
    } else if (type === 'IDAT') {
      idat.push(data);
    }
    offset += length + 12;
  }

  const bytesPerPixel = colorType === 6 ? 4 : 3;
  const stride = width * bytesPerPixel;
  const raw = inflateSync(Buffer.concat(idat));
  const pixels = Buffer.alloc(height * stride);
  let rawOffset = 0;

  const paeth = (left, above, upperLeft) => {
    const estimate = left + above - upperLeft;
    const leftDistance = Math.abs(estimate - left);
    const aboveDistance = Math.abs(estimate - above);
    const upperLeftDistance = Math.abs(estimate - upperLeft);
    if (leftDistance <= aboveDistance && leftDistance <= upperLeftDistance) return left;
    if (aboveDistance <= upperLeftDistance) return above;
    return upperLeft;
  };

  for (let y = 0; y < height; y += 1) {
    const filter = raw[rawOffset++];
    for (let x = 0; x < stride; x += 1) {
      let value = raw[rawOffset++];
      const left = x >= bytesPerPixel ? pixels[y * stride + x - bytesPerPixel] : 0;
      const above = y > 0 ? pixels[(y - 1) * stride + x] : 0;
      const upperLeft = y > 0 && x >= bytesPerPixel
        ? pixels[(y - 1) * stride + x - bytesPerPixel]
        : 0;
      if (filter === 1) value = (value + left) & 255;
      if (filter === 2) value = (value + above) & 255;
      if (filter === 3) value = (value + Math.floor((left + above) / 2)) & 255;
      if (filter === 4) value = (value + paeth(left, above, upperLeft)) & 255;
      pixels[y * stride + x] = value;
    }
  }

  let ink = 0;
  for (let y = 5; y < 14; y += 1) {
    for (let x = 4; x < 21; x += 1) {
      const index = (y * width + x) * bytesPerPixel;
      if (Math.min(pixels[index], pixels[index + 1], pixels[index + 2]) < 240) ink += 1;
    }
  }
  return ink;
}

const browsers = [
  {
    name: 'Firefox',
    launch: {
      browser: 'firefox',
      executablePath: '/home/agent/firefox-stable-8mjlhgkg/firefox/firefox',
      headless: true,
    },
    expectTopInteriorInk: true,
  },
  {
    name: 'Chrome',
    launch: {
      browser: 'chrome',
      executablePath: '/home/agent/chrome-stable-nm5pfh94/chrome-linux64/chrome',
      headless: true,
      args: ['--no-sandbox'],
    },
    expectTopInteriorInk: false,
  },
];

async function reproduce(config) {
  const browser = await puppeteer.launch(config.launch);
  const consoleErrors = [];
  const failedRequests = [];

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1365, height: 768 });
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    page.on('requestfailed', request => {
      failedRequests.push(`${request.method()} ${request.url()} (${request.failure()?.errorText})`);
    });

    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#email', { visible: true, timeout: 30000 });

    const email = `compat${Date.now()}${config.name.toLowerCase()}@mail.com`;
    await page.type('#email', email);
    await page.evaluate(() => {
      const button = [...document.querySelectorAll('button')]
        .find(candidate => candidate.textContent.includes('Get OTP'));
      button.click();
    });

    await page.waitForSelector('.otp-field-input', { visible: true, timeout: 30000 });
    await page.waitForFunction(
      () => document.querySelectorAll('.otp-field-input').length === 6,
      { timeout: 30000 },
    );

    const details = await page.evaluate(() => {
      const inputs = [...document.querySelectorAll('.otp-field-input')];
      const first = inputs[0];
      const style = getComputedStyle(first);
      return {
        count: inputs.length,
        type: first.type,
        width: style.width,
        padding: style.padding,
        appearance: style.appearance,
        success: document.querySelector('form').innerText.includes('OTP is sent successfully'),
      };
    });

    const firstInput = await page.$('.otp-field-input');
    const screenshot = await firstInput.screenshot({ encoding: 'binary' });
    const topInteriorInk = countTopInteriorInk(screenshot);
    const hasTopInteriorInk = topInteriorInk > 30;

    const passed = details.count === 6
      && details.type === 'number'
      && details.success
      && hasTopInteriorInk === config.expectTopInteriorInk;

    console.log(
      `${config.name}: ${passed ? 'PASS' : 'FAIL'} - `
      + `OTP inputs=${details.count}, type=${details.type}, width=${details.width}, `
      + `appearance=${details.appearance}, top-interior ink pixels=${topInteriorInk} `
      + `(${hasTopInteriorInk ? 'native stepper arrows visible' : 'no stepper arrows'}), `
      + `console errors=${consoleErrors.length}, failed requests=${failedRequests.length}`,
    );

    return { name: config.name, passed, hasTopInteriorInk, topInteriorInk, details };
  } finally {
    await browser.close();
  }
}

const results = [];
for (const config of browsers) {
  try {
    results.push(await reproduce(config));
  } catch (error) {
    console.log(`${config.name}: FAIL - ${error.stack || error}`);
    results.push({ name: config.name, passed: false, error: String(error) });
  }
}

const firefox = results.find(result => result.name === 'Firefox');
const chrome = results.find(result => result.name === 'Chrome');
const differenceReproduced = Boolean(
  firefox?.passed
  && chrome?.passed
  && firefox.hasTopInteriorInk
  && !chrome.hasTopInteriorInk,
);

console.log(`DIFFERENCE REPRODUCED: ${differenceReproduced ? 'YES' : 'NO'}`);
if (!differenceReproduced) process.exitCode = 1;
