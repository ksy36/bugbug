// Repro for webcompat #2052489 — drmeth.com layout broken in Firefox.
//
// Root cause: the game is embedded in
//   <iframe src="/game/index.html" class="aspect-video max-h-full max-w-full">
// with NO explicit width/height attributes or CSS width/height. It is a
// replaced flex item (flex: 0 1 auto) inside a
//   display:flex; align-items:center; justify-content:center
// container, and only has aspect-ratio:16/9 + max-width:100% + max-height:100%.
//
// Chrome stretches this iframe to fill the available flex container
// (e.g. ~full container width). Firefox instead uses the iframe's DEFAULT
// intrinsic size (300px wide, 168.75px tall via the 16/9 ratio), so the whole
// game is squeezed into a tiny ~300px box in the center of the page with all
// its internal elements overlapping and unscaled.
//
// This script drives the REAL site (https://drmeth.com/) in both browsers,
// measures the rendered iframe size, and asserts the divergence.

import puppeteer from '/app/node/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

const URL = 'https://drmeth.com/';
const IFRAME_SEL = 'iframe.aspect-video';
const VIEWPORT = { width: 1280, height: 800 };

// If Firefox's iframe is <= this, the game is collapsed to its intrinsic size.
const COLLAPSED_MAX = 320;
// If Chrome's iframe is >= this, it stretched to fill the container.
const FILLED_MIN = 800;

const LAUNCHERS = {
  firefox: {
    browser: 'firefox',
    executablePath: '/home/agent/firefox-stable-jthjx0v5/firefox/firefox',
    headless: true,
  },
  chrome: {
    browser: 'chrome',
    executablePath: '/home/agent/chrome-stable-q92ogox_/chrome-linux64/chrome',
    headless: true,
    args: ['--no-sandbox'],
  },
};

async function measure(name) {
  const browser = await puppeteer.launch(LAUNCHERS[name]);
  try {
    const page = await browser.newPage();
    await page.setViewport(VIEWPORT);
    await page.goto(URL, { waitUntil: 'load', timeout: 60000 });
    await page.waitForSelector(IFRAME_SEL, { timeout: 30000 });
    // Give layout a moment to settle.
    await new Promise((r) => setTimeout(r, 1500));

    const data = await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        rectW: Math.round(r.width),
        rectH: Math.round(r.height),
        cssW: cs.width,
        cssH: cs.height,
        aspectRatio: cs.aspectRatio,
        maxWidth: cs.maxWidth,
        maxHeight: cs.maxHeight,
        flex: cs.flex,
        parentW: Math.round(el.parentElement.getBoundingClientRect().width),
      };
    }, IFRAME_SEL);

    return data;
  } finally {
    await browser.close();
  }
}

const ff = await measure('firefox');
const ch = await measure('chrome');

console.log('--- drmeth.com game <iframe> measurements ---');
console.log('Firefox:', JSON.stringify(ff));
console.log('Chrome :', JSON.stringify(ch));
console.log('');

if (!ff || !ch) {
  console.log('FAIL: could not locate the game iframe in one of the browsers.');
  console.log('RESULT: difference did NOT reproduce.');
  process.exit(1);
}

const ffCollapsed = ff.rectW <= COLLAPSED_MAX;
const chFilled = ch.rectW >= FILLED_MIN;

console.log(
  `Firefox: iframe width = ${ff.rectW}px (container ${ff.parentW}px) -> ` +
    (ffCollapsed
      ? `PASS: collapsed to intrinsic size (<= ${COLLAPSED_MAX}px), game is tiny/overlapping (BUG reproduced)`
      : `FAIL: iframe did NOT collapse (> ${COLLAPSED_MAX}px)`)
);
console.log(
  `Chrome : iframe width = ${ch.rectW}px (container ${ch.parentW}px) -> ` +
    (chFilled
      ? `PASS: stretched to fill container (>= ${FILLED_MIN}px), game renders correctly`
      : `FAIL: iframe did NOT fill container (< ${FILLED_MIN}px)`)
);
console.log('');

const reproduced = ffCollapsed && chFilled && ch.rectW > ff.rectW * 2;
if (reproduced) {
  console.log(
    `RESULT: difference REPRODUCED — same iframe/CSS, Firefox ${ff.rectW}px vs Chrome ${ch.rectW}px ` +
      `(${(ch.rectW / ff.rectW).toFixed(1)}x larger in Chrome).`
  );
  process.exit(0);
} else {
  console.log('RESULT: difference did NOT reproduce.');
  process.exit(1);
}
