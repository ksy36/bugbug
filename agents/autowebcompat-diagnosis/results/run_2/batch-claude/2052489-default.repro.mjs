// Reproduction for webcompat bug 2052489
// Site: https://drmeth.com/  (Dr. Meth - Idle Clicker)
//
// Reported symptom: In Firefox the page layout is broken — the game is
// crammed into a tiny overlapping box in the center of the page, while it
// renders correctly (full size) in Chrome.
//
// Root cause observed: the game is embedded in an <iframe> that carries only
// CSS classes `aspect-video max-h-full max-w-full` (aspect-ratio: 16/9 with
// max-width/max-height: 100%) and NO explicit width/height attribute, inline
// size, or CSS width. It sits in a flex container that uses items-center /
// justify-center, so flex does not stretch it on either axis.
//   - Chrome sizes this block-level replaced element to fill the container's
//     inline size (~viewport width) and derives the height from 16/9.
//   - Firefox uses the iframe's DEFAULT intrinsic replaced size (width 300px),
//     deriving height 300/(16/9) ≈ 168.75px -> the game renders at ~300px.
//
// This script drives the REAL site in both browsers, measures the game
// iframe width relative to its container, and asserts the divergence.

import { createRequire } from 'module';
const require = createRequire('/app/node/node_modules/');
const puppeteer = require('puppeteer');

const URL = 'https://drmeth.com/';
const VIEWPORT = { width: 1366, height: 768 };

const FIREFOX = {
  browser: 'firefox',
  executablePath: '/home/agent/firefox-stable-oo6huhlg/firefox/firefox',
  headless: true,
};
const CHROME = {
  browser: 'chrome',
  executablePath: '/home/agent/chrome-stable-zs5riqik/chrome-linux64/chrome',
  headless: true,
  args: ['--no-sandbox'],
};

// If the iframe width is less than this fraction of its container width, the
// game has collapsed to the intrinsic default size => broken layout.
const COLLAPSE_FRACTION = 0.5;

async function measure(launchOpts, label) {
  const browser = await puppeteer.launch(launchOpts);
  try {
    const page = await browser.newPage();
    await page.setViewport(VIEWPORT);
    await page.goto(URL, { waitUntil: 'load', timeout: 60000 });
    await page.waitForSelector('iframe', { timeout: 30000 });
    // Give layout a moment to settle.
    await new Promise((r) => setTimeout(r, 1500));

    const data = await page.evaluate(() => {
      const f = document.querySelector('iframe');
      if (!f) return null;
      const cs = getComputedStyle(f);
      const r = f.getBoundingClientRect();
      const parent = f.parentElement;
      const pr = parent.getBoundingClientRect();
      return {
        iframeW: Math.round(r.width),
        iframeH: Math.round(r.height),
        cssWidth: cs.width,
        aspectRatio: cs.aspectRatio,
        hasWidthAttr: f.hasAttribute('width'),
        inlineStyle: f.getAttribute('style'),
        containerW: Math.round(pr.width),
        src: f.getAttribute('src'),
      };
    });

    if (!data) throw new Error('game iframe not found');

    const fraction = data.iframeW / data.containerW;
    const collapsed = fraction < COLLAPSE_FRACTION;

    console.log(`\n[${label}]`);
    console.log(`  iframe src        : ${data.src}`);
    console.log(`  width attr / style: ${data.hasWidthAttr ? 'present' : 'none'} / ${data.inlineStyle ?? 'none'}`);
    console.log(`  computed CSS width: ${data.cssWidth}  (aspect-ratio: ${data.aspectRatio})`);
    console.log(`  iframe box        : ${data.iframeW} x ${data.iframeH}`);
    console.log(`  container width   : ${data.containerW}`);
    console.log(`  iframe/container  : ${(fraction * 100).toFixed(1)}%`);
    console.log(`  layout collapsed  : ${collapsed ? 'YES (broken)' : 'no (correct)'}`);

    return { ...data, fraction, collapsed };
  } finally {
    await browser.close();
  }
}

const ff = await measure(FIREFOX, 'FIREFOX');
const cr = await measure(CHROME, 'CHROME');

// Expected reproduction: Firefox collapses the iframe to intrinsic size while
// Chrome fills the container.
const ffPass = ff.collapsed === true;   // Firefox exhibits the broken layout
const crPass = cr.collapsed === false;  // Chrome renders correctly

console.log('\n==== RESULTS ====');
console.log(`FIREFOX: ${ffPass ? 'PASS' : 'FAIL'} - game iframe ${ffPass ? 'collapsed to intrinsic size (broken layout reproduced)' : 'did NOT collapse'} (${ff.iframeW}px, ${(ff.fraction * 100).toFixed(1)}% of container)`);
console.log(`CHROME : ${crPass ? 'PASS' : 'FAIL'} - game iframe ${crPass ? 'filled container (correct layout)' : 'unexpectedly collapsed'} (${cr.iframeW}px, ${(cr.fraction * 100).toFixed(1)}% of container)`);

const reproduced = ffPass && crPass;
console.log(`\nDIFFERENCE REPRODUCED: ${reproduced ? 'YES' : 'NO'} - Firefox sizes the width-less aspect-ratio iframe to its 300px intrinsic default; Chrome stretches it to fill the container.`);

process.exit(reproduced ? 0 : 1);
