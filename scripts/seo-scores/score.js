// Rank Math SEO scores for new items (owner, 17 Oct 2026).
// Rank Math works its score out only in a browser (its "Update SEO Scores" tool runs assets/admin/js/analyzer.js in a
// logged-in admin page). This does the same unattended: it reads the analyzer values from aw/v1/seo-score-items (built
// on the site by Rank Math's own Update_Score tool, with aw-seo-extras' rendered body and each item's test list), runs
// the site's own copy of analyzer.js on them, and stores the numbers through aw/v1/seo-scores.
// Only published items with a focus keyword and no stored score, so it runs after the daily focus keywords.
// Run: node score.js   (needs WP_BASE_URL, WP_INGEST_USER, WP_INGEST_APP_PASSWORD)
const { JSDOM } = require('jsdom');

const base = (process.env.WP_BASE_URL || '').replace(/\/+$/, '');
const auth = 'Basic ' + Buffer.from(`${process.env.WP_INGEST_USER}:${process.env.WP_INGEST_APP_PASSWORD}`).toString('base64');
const BATCH = 20;
const MAX_ROUNDS = 50;

async function api(path, init = {}) {
  const res = await fetch(`${base}/wp-json/${path}`, { ...init, headers: { Authorization: auth, 'Content-Type': 'application/json', ...(init.headers || {}) } });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function loadAnalyzer(src, rankMath) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: `${base}/wp-admin/` });
  const w = dom.window;
  Object.assign(global, { window: w, document: w.document, navigator: w.navigator, DOMParser: w.DOMParser, Node: w.Node, HTMLElement: w.HTMLElement });
  w.lodash = global.lodash = require('lodash');
  w.jQuery = global.jQuery = require('jquery')(w);
  w.wp = global.wp = { hooks: require('@wordpress/hooks'), i18n: require('@wordpress/i18n'), autop: require('@wordpress/autop'), wordcount: require('@wordpress/wordcount'), url: require('@wordpress/url') };
  w.rankMath = global.rankMath = { links: {}, ...rankMath }; // links: help-text links only
  new Function('window', 'document', src).call(w, w, w.document);
  return w.rankMathAnalyzer;
}

async function score(RA, rankMath, e) {
  const diac = {};
  lodash.forEach(rankMath.assessor.diacritics || {}, (re, ch) => { diac[ch] = new RegExp(re, 'g'); });
  const strip = (s) => { for (const k in diac) s = s.replace(diac[k], k); return s; };
  const p = new RA.Paper();
  p.setTitle(e.title); p.setDescription(e.description); p.setText(e.content); p.setKeyword(e.keyword); p.setKeywords(e.keywords);
  p.setPermalink(e.url); p.setUrl(e.url); p.setSchema(e.schemas); p.setPostType(e.post_type);
  if (e.thumbnail) p.setThumbnail(e.thumbnail);
  if (e.thumbnailAlt) p.setThumbnailAltText(strip(e.thumbnailAlt));
  p.setContentAI(e.hasContentAi);
  const tests = lodash.difference(e.tests || rankMath.assessor.researchesTests, ['keywordNotUsed']);
  const res = await new RA.Analyzer({ i18n: wp.i18n, analysis: tests }).analyzeSome(tests, p);
  const rm = new RA.ResultManager();
  rm.update(p.getKeyword(), res, true);
  return rm.getScore(p.getKeyword());
}

(async () => {
  if (!base || !process.env.WP_INGEST_USER || !process.env.WP_INGEST_APP_PASSWORD) throw new Error('WP_BASE_URL, WP_INGEST_USER and WP_INGEST_APP_PASSWORD are needed');
  const r = await fetch(`${base}/wp-content/plugins/seo-by-rank-math/assets/admin/js/analyzer.js`);
  if (!r.ok) throw new Error(`analyzer.js: HTTP ${r.status}`);
  const src = await r.text();
  let RA = null;
  const seen = new Set();
  const stored = [];
  const failed = [];
  for (let round = 0; round < MAX_ROUNDS; round++) {
    const batch = await api(`aw/v1/seo-score-items?limit=${BATCH}`);
    const posts = Object.entries(batch.posts || {}).filter(([id]) => !seen.has(id));
    if (!posts.length) break;
    if (!RA) RA = loadAnalyzer(src, batch.rankMath);
    const scores = {};
    for (const [id, e] of posts) {
      seen.add(id);
      try {
        scores[id] = await score(RA, batch.rankMath, e);
      } catch (err) {
        failed.push(`${id}: ${err.message}`);
      }
    }
    if (Object.keys(scores).length) {
      const out = await api('aw/v1/seo-scores', { method: 'POST', body: JSON.stringify({ scores }) });
      for (const id of out.written || []) stored.push(`${id}=${scores[id]}`);
    }
  }
  console.log(`Stored ${stored.length} score(s)${stored.length ? ': ' + stored.join(', ') : ''}`);
  if (failed.length) console.log(`Not scored: ${failed.join('; ')}`);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary) require('fs').appendFileSync(summary, `### Rank Math SEO scores\n\nStored ${stored.length}.${failed.length ? ` Not scored: ${failed.length}.` : ''}\n`);
})().catch((err) => { console.error(err.message || err); process.exit(1); });
