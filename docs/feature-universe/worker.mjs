/** Public reads and narrowly authenticated publishing of sanitized progress only. */
const STATUSES = new Set(['implemented', 'partial', 'planned', 'historical']);
const PROGRESS = new Set(['unchanged', 'changed', 'in_progress', 'blocked', 'verified']);
const MAX_FEED_LENGTH = 1024 * 1024;
const MAX_PUBLISH_BYTES = 512 * 1024;
const API_HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Cache-Control': 'no-store',
  'CDN-Cache-Control': 'no-store',
  'Cloudflare-CDN-Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
};

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function string(value, maximum, { empty = false } = {}) {
  return typeof value === 'string' && value.length <= maximum && (empty || value.trim().length > 0);
}

const CANONICAL = 'jacobthebaer-lab/text-monkey';
const REVISION = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;
// Match the publisher's basic public-data guard, not a general PII classifier.
const PRIVATE = /[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:\+\d[\d ()-]{7,}\d)|(?:\b\d{3}[-. ]\d{3}[-. ]\d{4}\b)|\b(?:\d[ ()-]*){10,}\b|(?:\/Users\/|\/home\/|[A-Za-z]:\\)|\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})|\b(?:bearer\s+|api[_ -]?key\s*[:=]|password\s*[:=]|secret\s*[:=])/i;

function publicText(value, maximum) {
  return string(value, maximum) && !/[\x00-\x1f\x7f<>]/.test(value) &&
    !PRIVATE.test(value) && !/https?:\/\//i.test(value);
}

function timestamp(value) {
  return string(value, 40) && /^\d{4}-\d{2}-\d{2}T/.test(value) &&
    /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)) &&
    Date.parse(value) <= Date.now() + 5 * 60 * 1000;
}

function publicLink(link) {
  if (!object(link) || !publicText(link.title, 100) || !string(link.url, 1000)) return null;
  try {
    const url = new URL(link.url);
    const path = decodeURIComponent(url.pathname);
    if (url.protocol !== 'https:' || url.host !== 'github.com' || url.username || url.password ||
        url.search || url.hash || PRIVATE.test(path) ||
        path.split('/').some(part => part === '.' || part === '..') ||
        !new RegExp('^/' + CANONICAL + '/(?:commit/[a-f0-9]{40,64}|pull/[1-9][0-9]*|blob/[a-f0-9]{40,64}/[A-Za-z0-9_./%+-]+)$').test(url.pathname)) return null;
    return { title: link.title, url: link.url };
  } catch {
    return null;
  }
}

/** Validate the shared v1 contract and project only its public fields. */
export function publicFeed(value) {
  if (!object(value) || value.schemaVersion !== 1 || !timestamp(value.checkedAt) ||
      !timestamp(value.generatedAt) || !object(value.repository) ||
      !string(value.repository.revision, 64) || !REVISION.test(value.repository.revision) || !string(value.repository.branch, 200) ||
      !/^[A-Za-z0-9_./-]+$/.test(value.repository.branch) || PRIVATE.test(value.repository.branch) ||
      !object(value.features) || !object(value.sync) ||
      value.sync.mode !== 'repository-events' || value.sync.intervalMinutes !== 60) return null;

  const latest = Date.now() + 5 * 60 * 1000;
  if (Date.parse(value.checkedAt) > latest || Date.parse(value.generatedAt) > latest) return null;

  const entries = Object.entries(value.features);
  if (entries.length === 0 || entries.length > 2048) return null;
  const features = Object.create(null);
  for (const [id, feature] of entries) {
    if (!/^[a-z0-9][a-z0-9_-]{0,99}$/.test(id) ||
        !object(feature) || !STATUSES.has(feature.status) || !PROGRESS.has(feature.progress) ||
        !publicText(feature.summary, 600) || !timestamp(feature.checkedAt) ||
        !string(feature.sourceRevision, 64) || !REVISION.test(feature.sourceRevision)) return null;
    const clean = {
      status: feature.status,
      progress: feature.progress,
      summary: feature.summary,
      checkedAt: feature.checkedAt,
      sourceRevision: feature.sourceRevision,
    };
    if (feature.links !== undefined) {
      if (!Array.isArray(feature.links) || feature.links.length > 5) return null;
      const links = feature.links.map(publicLink);
      if (links.some(link => link === null)) return null;
      clean.links = links;
    }
    features[id] = clean;
  }
  return {
    schemaVersion: 1,
    checkedAt: value.checkedAt,
    generatedAt: value.generatedAt,
    repository: { revision: value.repository.revision, branch: value.repository.branch },
    features,
    sync: { mode: 'repository-events', intervalMinutes: 60 },
  };
}

function json(request, value, status = 200, extraHeaders = {}) {
  return new Response(request.method === 'HEAD' ? null : JSON.stringify(value), {
    status, headers: { ...API_HEADERS, ...extraHeaders },
  });
}

async function authorized(request, secret) {
  // Hash to equal-sized buffers, then use Cloudflare's native constant-time
  // comparison. Never compare the bearer secret with JavaScript equality.
  const supplied = request.headers.get('Authorization') || '';
  const encode = new TextEncoder();
  const [actual, expected] = await Promise.all([
    crypto.subtle.digest('SHA-256', encode.encode(supplied)),
    crypto.subtle.digest('SHA-256', encode.encode(`Bearer ${secret}`)),
  ]);
  return crypto.subtle.timingSafeEqual(actual, expected);
}

async function limitedBody(request) {
  const length = Number(request.headers.get('Content-Length'));
  if (Number.isFinite(length) && length > MAX_PUBLISH_BYTES) throw new RangeError('body_too_large');
  if (!request.body) return '';
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_PUBLISH_BYTES) {
      await reader.cancel().catch(() => {});
      throw new RangeError('body_too_large');
    }
    chunks.push(value);
  }
  const all = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { all.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder('utf-8', { fatal: true }).decode(all);
}

function regresses(next, current) {
  if (Date.parse(next.checkedAt) < Date.parse(current.checkedAt) ||
      Date.parse(next.generatedAt) < Date.parse(current.generatedAt) ||
      (next.repository.revision !== current.repository.revision &&
        Date.parse(next.checkedAt) <= Date.parse(current.checkedAt))) return true;
  // Feature timestamps preserve the last meaningful review, which may be
  // older than a previous automatic source check. The GitHub pipeline verifies
  // review applicability and ancestry; only feed publication is monotonic here.
  return false;
}

async function publish(request, env) {
  if (request.method !== 'POST') {
    return json(request, { error: 'method_not_allowed' }, 405, { Allow: 'POST' });
  }
  if (!string(env.STATUS_PUBLISH_KEY, 512) || env.STATUS_PUBLISH_KEY.length < 32 ||
      !env.FEATURE_STATUS?.get || !env.FEATURE_STATUS?.put) {
    return json(request, { error: 'publish_unavailable' }, 503);
  }
  try {
    if (!await authorized(request, env.STATUS_PUBLISH_KEY)) {
      return json(request, { error: 'unauthorized' }, 401);
    }
  } catch {
    return json(request, { error: 'publish_unavailable' }, 503);
  }
  const origin = request.headers.get('Origin');
  if (origin && origin !== new URL(request.url).origin) {
    return json(request, { error: 'origin_not_allowed' }, 403);
  }
  if (request.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    return json(request, { error: 'json_required' }, 415);
  }
  let next;
  try {
    next = publicFeed(JSON.parse(await limitedBody(request)));
  } catch (error) {
    return json(request, { error: error instanceof RangeError ? 'body_too_large' : 'invalid_feed' },
      error instanceof RangeError ? 413 : 400);
  }
  if (!next) return json(request, { error: 'invalid_feed' }, 400);
  try {
    const raw = await env.FEATURE_STATUS.get('feature-status', { type: 'text', cacheTtl: 60 });
    if (raw !== null) {
      const current = typeof raw === 'string' && raw.length <= MAX_FEED_LENGTH && publicFeed(JSON.parse(raw));
      if (!current) return json(request, { error: 'publish_unavailable' }, 503);
      if (regresses(next, current)) return json(request, { error: 'stale_feed' }, 409);
      if (JSON.stringify(current) === JSON.stringify(next)) {
        return json(request, { ok: true, unchanged: true, checkedAt: next.checkedAt });
      }
    }
    // KV has no compare-and-set. The publisher must serialize jobs; this guard
    // rejects regression visible in this snapshot, not all distributed races.
    await env.FEATURE_STATUS.put('feature-status', JSON.stringify(next));
    return json(request, { ok: true, checkedAt: next.checkedAt, revision: next.repository.revision });
  } catch {
    return json(request, { error: 'publish_unavailable' }, 503);
  }
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === '/api/status/publish') return publish(request, env);
    if (!['GET', 'HEAD'].includes(request.method)) {
      return json(request, { error: 'method_not_allowed' }, 405, { Allow: 'GET, HEAD' });
    }
    if (pathname === '/api/status') {
      try {
        // KV can propagate updates after this cache window. HTTP no-store does
        // not turn KV into a strongly consistent store; timestamps stay intact.
        const raw = await env.FEATURE_STATUS?.get('feature-status', { type: 'text', cacheTtl: 60 });
        if (typeof raw !== 'string' || raw.length > MAX_FEED_LENGTH) {
          return json(request, { error: 'status_unavailable' }, 503);
        }
        const feed = publicFeed(JSON.parse(raw));
        if (!feed) return json(request, { error: 'status_unavailable' }, 503);
        return json(request, feed);
      } catch {
        // Never expose KV errors, binding details, stored source data or secrets.
        return json(request, { error: 'status_unavailable' }, 503);
      }
    }
    if (pathname === '/api' || pathname.startsWith('/api/')) {
      return json(request, { error: 'not_found' }, 404);
    }
    try {
      if (!env.ASSETS) return json(request, { error: 'assets_unavailable' }, 503);
      return await env.ASSETS.fetch(request);
    } catch {
      return json(request, { error: 'assets_unavailable' }, 503);
    }
  },
};
