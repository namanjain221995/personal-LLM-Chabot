'use strict';
/**
 * Browser plumbing: one system Chrome for the run, a fresh incognito context
 * per check (so one check's cookies can never make another pass), and the
 * small helpers every suite needs.
 */

const path = require('path');
const puppeteer = require('puppeteer-core');

const { HttpClient, SESSION_COOKIE } = require('./http');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Elements that may hold a plaintext credential on screen. */
const SECRET_SELECTORS = '[data-testid="created-key-secret"], input[type="password"]';

async function launchBrowser(cfg) {
  return puppeteer.launch({
    executablePath: cfg.chrome,
    headless: !cfg.headful,
    // --no-sandbox: the suite runs as an unprivileged user on hosts where
    // Chrome's namespace sandbox is not available, and it only ever loads the
    // application under test on a loopback address.
    args: ['--no-sandbox', '--disable-gpu', '--force-device-scale-factor=1', '--no-first-run', '--disable-extensions'],
    defaultViewport: null,
  });
}

/** The viewport a width stands for: phones and tablets get touch + mobile. */
function viewportFor(width) {
  const mobile = width < 800;
  return {
    width,
    height: width < 400 ? 740 : width < 800 ? 1024 : 900,
    deviceScaleFactor: 1,
    isMobile: mobile,
    hasTouch: mobile,
  };
}

/**
 * Collect what a person with DevTools open would see in red: console errors
 * and uncaught exceptions. Kept as text with the source location, verbatim.
 */
function trackConsole(page, sink) {
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const loc = msg.location();
    const where = loc && loc.url ? ` (${loc.url}${loc.lineNumber != null ? `:${loc.lineNumber}` : ''})` : '';
    sink.push(`console.error: ${msg.text()}${where}`);
  });
  page.on('pageerror', (err) => {
    sink.push(`pageerror: ${err && err.message ? err.message : String(err)}`);
  });
}

/**
 * Per-check session factory. Everything opened through it is screenshotted on
 * failure and closed afterwards, whatever the check did.
 */
function makeSessions(browserRef, cfg, test, result) {
  const contexts = [];
  const pages = [];
  const clients = [];
  let disposed = false;
  const guard = () => {
    if (disposed) throw new Error(`${test.id} is over (it failed or timed out); it may not open sessions or send requests`);
  };

  async function http(role) {
    guard();
    const client = new HttpClient(cfg.base, { guard });
    if (role) {
      const who = cfg[role];
      const password = who.password();
      if (!password) {
        const { SkipError } = require('./harness');
        throw new SkipError(
          `no password for the ${role} account: set E2E_${role.toUpperCase()}_PASSWORD or E2E_${role.toUpperCase()}_PASSWORD_FILE`,
        );
      }
      await client.login(who.email, password);
      clients.push(client);
    }
    return client;
  }

  /**
   * A page in its own incognito context.
   * @param {{role?: 'admin'|'member'|null, width?: number}} [opts]
   *   role signs the context in through the BFF and pins the session cookie,
   *   which is what the login form would have left behind.
   */
  async function page(opts = {}) {
    guard();
    const browser = await browserRef();
    const context = await browser.createBrowserContext();
    contexts.push(context);
    const p = await context.newPage();
    pages.push(p);
    trackConsole(p, result.consoleErrors);
    await p.setViewport(viewportFor(opts.width || 1440));
    p.setDefaultTimeout(30_000);
    p.setDefaultNavigationTimeout(60_000);
    let client = null;
    if (opts.role) {
      client = await http(opts.role);
      const url = new URL(cfg.base);
      await p.setCookie({
        name: SESSION_COOKIE,
        value: client.cookies[SESSION_COOKIE],
        url: url.origin,
        httpOnly: true,
        secure: true,
        sameSite: 'Lax',
      });
    }
    return { page: p, context, client };
  }

  async function screenshotOpenPages(dir) {
    let n = 0;
    for (const p of pages) {
      if (p.isClosed()) continue;
      n += 1;
      // A failure while the one-time key secret is on screen must not write
      // it into a PNG (review, 2026-09-13): blank it before the capture.
      await p
        .evaluate((selector) => {
          for (const el of document.querySelectorAll(selector)) {
            el.style.setProperty('filter', 'blur(24px)', 'important');
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') el.value = '[secret hidden by e2e]';
            else el.textContent = '[secret hidden by e2e]';
          }
        }, SECRET_SELECTORS)
        .catch(() => {});
      const file = path.join(dir, `${test.id.replace(/[^A-Za-z0-9_.-]/g, '_')}-${n}.png`);
      try {
        await p.screenshot({ path: file, fullPage: false });
        result.screenshots.push(path.relative(path.join(dir, '..'), file));
      } catch (err) {
        result.notes.push(`screenshot of page ${n} failed: ${err.message}`);
      }
    }
  }

  async function dispose() {
    disposed = true;
    for (const c of contexts) await c.close().catch(() => {});
    // Sign every HTTP session this check opened back out, so a run does not
    // leave a trail of live sessions on a shared test account.
    for (const client of clients) await client.post('/api/auth/logout', { afterDispose: true }).catch(() => {});
  }

  /** Track a page the check opened itself (e.g. a second tab in a context). */
  function adopt(p) {
    pages.push(p);
    return p;
  }

  return { http, page, adopt, screenshotOpenPages, dispose };
}

async function waitForPath(page, pathname, timeoutMs = 30_000) {
  await page.waitForFunction((p) => window.location.pathname === p, { timeout: timeoutMs }, pathname);
}

/** Visible text of the whole page, whitespace-collapsed. */
async function pageText(page) {
  return page.evaluate(() => (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim());
}

/**
 * Click the first VISIBLE element matching `selector` whose text (or
 * aria-label) matches `pattern`. Returns false when none does.
 */
async function clickByText(page, selector, pattern) {
  const source = pattern instanceof RegExp ? pattern.source : String(pattern).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const flags = pattern instanceof RegExp ? pattern.flags : 'i';
  return page.evaluate(
    (sel, src, fl) => {
      const re = new RegExp(src, fl);
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      for (const el of document.querySelectorAll(sel)) {
        const text = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''}`.trim();
        if (re.test(text) && visible(el)) {
          el.click();
          return true;
        }
      }
      return false;
    },
    selector,
    source,
    flags,
  );
}

async function waitForText(page, pattern, timeoutMs = 30_000) {
  const source = pattern instanceof RegExp ? pattern.source : String(pattern).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const flags = pattern instanceof RegExp ? pattern.flags : '';
  await page.waitForFunction(
    (src, fl) => new RegExp(src, fl).test(document.body ? document.body.innerText : ''),
    { timeout: timeoutMs },
    source,
    flags,
  );
}

/**
 * Horizontal overflow at one viewport, measured on the DOCUMENT and on every
 * ELEMENT.
 *
 * WHY ELEMENT BY ELEMENT (2026-09-13): the chat, admin and console shells are
 * `flex h-dvh overflow-hidden` with panes that scroll inside. Their document
 * can never grow wider than the viewport, so a document-only meter read 0 on
 * a page with a 1200px element in its main pane at 360px (the review proved
 * it on /admin/members and /api?tab=keys). The overflow a person suffers there
 * is a pane that scrolls sideways, or content cut off by an `overflow:hidden`
 * ancestor at the screen edge, and neither shows up in scrollWidth.
 *
 * An element counts when it is visible, partly on screen, and its right edge
 * is past the viewport's. Then the nearest ancestor that clips or scrolls
 * horizontally, and itself fits the viewport, decides:
 *   - none                          → past the right edge of the page
 *   - overflow-x hidden/clip        → cut off at the screen edge (unless that
 *                                     ancestor truncates with an ellipsis)
 *   - overflow-x auto/scroll        → a pane that scrolls sideways, UNLESS the
 *                                     pane is a designated horizontal scroller:
 *                                     a pre/code/table/textarea, a tab strip or
 *                                     toolbar role, the wrapper of one table, or
 *                                     a short strip that does not scroll
 *                                     vertically (a chip row, a code block).
 * Wholly off-screen elements (a closed drawer translated away), anything
 * under aria-hidden or inert, and anything inside a screen-reader-only box
 * (clipped to nothing, or 1x1 px with its overflow hidden) are ignored; an
 * absolutely positioned element is
 * only clipped by its containing block and that block's ancestors.
 *
 * The document term is kept too. On a mobile-emulated viewport Chrome zooms
 * OUT to fit wide content, which widens clientWidth along with it, so the
 * edge is the width the viewport was ASKED for (or less, when a classic
 * scrollbar takes some of it).
 */
async function measureOverflow(page, requestedWidth) {
  return page.evaluate((w) => {
    const TOL = 1;
    const doc = document.documentElement;
    const body = document.body;
    const scrollWidth = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0);
    const docOverflowPx = Math.max(scrollWidth - doc.clientWidth, scrollWidth - w, 0);
    const edge = Math.min(w, doc.clientWidth || w);

    const describe = (el) => {
      if (!el) return 'the page';
      const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 4).join('.') : '';
      const testid = el.getAttribute && el.getAttribute('data-testid');
      return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${testid ? `[data-testid="${testid}"]` : ''}${cls ? `.${cls}` : ''}`;
    };

    const designatedScroller = (a) => {
      if (/^(PRE|CODE|TABLE|TEXTAREA)$/.test(a.tagName)) return true;
      if (a.hasAttribute('data-scroll-x')) return true;
      const role = a.getAttribute('role');
      if (role && /^(tablist|toolbar|menubar|grid|table|listbox)$/.test(role)) return true;
      if (a.children.length === 1 && a.children[0].tagName === 'TABLE') return true;
      // A strip whose height follows its content (it does not scroll
      // vertically) and is short: a chip row, a tab bar, a code block. A main
      // pane of an h-dvh shell is neither short nor content-sized.
      return a.clientHeight < 0.6 * window.innerHeight && a.scrollHeight <= a.clientHeight + TOL;
    };

    /**
     * How content past the edge is contained, walking up from `start`
     * (inclusive): null means acceptable. `position` is the content's own
     * position — an absolutely positioned box is clipped only by its
     * containing block and that block's ancestors.
     */
    const containment = (start, position) => {
      if (position === 'fixed') return { how: 'past the right edge of the page', container: null };
      let onlyPositioned = position === 'absolute';
      for (let a = start; a && a !== doc; a = a.parentElement) {
        const as = getComputedStyle(a);
        const positioned = as.position !== 'static' || as.transform !== 'none';
        if (onlyPositioned && !positioned) continue;
        onlyPositioned = false;
        if (as.overflowX !== 'visible') {
          const ar = a.getBoundingClientRect();
          if (ar.right <= edge + TOL) {
            if (as.overflowX === 'auto' || as.overflowX === 'scroll') {
              return designatedScroller(a) ? null : { how: 'scrolls sideways inside', container: a };
            }
            if (as.textOverflow === 'ellipsis') return null;
            return { how: `cut off at the screen edge by overflow-x:${as.overflowX} on`, container: a };
          }
        }
        if (as.position === 'fixed') return { how: 'past the right edge of the page', container: null };
        if (as.position === 'absolute') onlyPositioned = true;
      }
      return { how: 'past the right edge of the page', container: null };
    };

    const flagged = new Set();
    const underFlagged = (node) => {
      for (let p = node; p; p = p.parentElement) if (flagged.has(p)) return true;
      return false;
    };
    /**
     * Visually hidden ON PURPOSE (2026-09-14): the element or an ancestor is
     * the screen-reader-only pattern — an absolutely positioned box clipped to
     * nothing (`clip: rect(0 0 0 0)` or `clip-path: inset(50%)`), or a box of
     * at most 1x1 px that clips its own overflow. Nothing inside such a box
     * can paint, however wide its text runs, but a text RANGE is not clipped,
     * so the range branch below read the console's hidden scope list (569 px
     * of text in a 1x1 `span.sr-only`) as cut off at 1024-1440 px.
     */
    const visuallyHidden = (el) => {
      for (let a = el; a && a !== doc; a = a.parentElement) {
        const as = getComputedStyle(a);
        const outOfFlow = as.position === 'absolute' || as.position === 'fixed';
        if (outOfFlow && as.clip.replace(/[\s,]|px/g, '') === 'rect(0000)') return true;
        if (outOfFlow && as.clipPath.replace(/\s/g, '') === 'inset(50%)') return true;
        if (as.overflowX !== 'visible' && as.overflowY !== 'visible') {
          const ar = a.getBoundingClientRect();
          if (ar.width <= 1 && ar.height <= 1) return true;
        }
      }
      return false;
    };
    const invisible = (el) =>
      Boolean(el.closest('[aria-hidden="true"], [inert]')) ||
      getComputedStyle(el).visibility === 'hidden' ||
      Number(getComputedStyle(el).opacity) === 0 ||
      visuallyHidden(el);

    /**
     * Wholly off-screen AND moved there on purpose: the element, or an
     * ancestor below its nearest clipping/scrolling box, is positioned out of
     * flow or transformed — a closed drawer, an off-canvas menu. An in-flow
     * box pushed past the edge by its siblings is NOT exempt: that is exactly
     * the second column an overflow-hidden shell cuts away.
     */
    const movedAway = (el) => {
      for (let a = el; a && a !== doc; a = a.parentElement) {
        const as = getComputedStyle(a);
        if (as.position === 'absolute' || as.position === 'fixed' || as.transform !== 'none' || as.translate !== 'none') return true;
        if (a !== el && as.overflowX !== 'visible') return false;
      }
      return false;
    };

    const offenders = [];
    let offenderCount = 0;
    let worst = 0;
    const report = (el, r, verdict, what) => {
      flagged.add(el);
      offenderCount += 1;
      worst = Math.max(worst, Math.round(r.right - edge));
      if (offenders.length < 6) {
        offenders.push({
          el: `${what}${describe(el)}`,
          right: Math.round(r.right),
          width: Math.round(r.width),
          how: verdict.how,
          container: verdict.container ? describe(verdict.container) : null,
        });
      }
    };

    // Elements AND text: a long unbroken string spills out of a box whose
    // own rectangle still fits, and only the text's range shows it.
    const range = document.createRange();
    const walker = body ? document.createTreeWalker(body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT) : null;
    for (let node = walker && walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === Node.TEXT_NODE) {
        const parent = node.parentElement;
        if (!parent || !node.nodeValue.trim() || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
        range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        if (r.width === 0 || r.right <= edge + TOL || r.left >= edge - TOL) continue;
        if (underFlagged(parent) || invisible(parent)) continue;
        const verdict = containment(parent, 'static');
        if (verdict) report(parent, r, verdict, 'text in ');
        continue;
      }
      const el = node;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right <= edge + TOL) continue;
      if (underFlagged(el.parentElement) || invisible(el)) continue;
      if (r.left >= edge - TOL && movedAway(el)) continue;
      const verdict = containment(el.parentElement, getComputedStyle(el).position);
      if (verdict) report(el, r, verdict, '');
    }

    return {
      scrollWidth,
      clientWidth: doc.clientWidth,
      innerWidth: window.innerWidth,
      edge,
      docOverflowPx,
      overflowPx: Math.max(docOverflowPx, worst),
      offenderCount,
      offenders,
    };
  }, requestedWidth);
}

/** One line per offender, for failure messages. */
function formatOffenders(m) {
  return m.offenders
    .map((o) => `${o.el} right=${o.right} width=${o.width} (${o.how}${o.container ? ` ${o.container}` : ''})`)
    .join('; ');
}

module.exports = {
  launchBrowser,
  makeSessions,
  viewportFor,
  waitForPath,
  pageText,
  clickByText,
  waitForText,
  measureOverflow,
  formatOffenders,
  SECRET_SELECTORS,
  trackConsole,
  sleep,
};
