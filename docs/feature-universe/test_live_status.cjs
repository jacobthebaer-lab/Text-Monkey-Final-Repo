"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const live = require("./explorer.js");

const now = Date.parse("2026-10-03T19:00:00Z");
const ids = new Set(["one"]);
const fixture = () => ({
  schemaVersion: 1,
  checkedAt: new Date(now).toISOString(),
  generatedAt: new Date(now).toISOString(),
  repository: { revision: "abc123", branch: "codex/complete-text-monkey" },
  features: {
    one: {
      status: "partial", progress: "changed", summary: "Source changed; verification is still required.",
      checkedAt: new Date(now).toISOString(), sourceRevision: "abc123",
      links: [{ title: "Review change", url: "https://github.com/example/repository/commit/abc123" }],
    },
  },
  sync: { mode: "repository-events", intervalMinutes: 60 },
});
const validate = (input) => live.validate(input, ids, now);

test("validates both supported feed modes and ignores unknown feature IDs", () => {
  const payload = fixture();
  payload.features.futureFeature = { this: "belongs to a later map" };
  const result = validate(payload);
  assert.deepEqual(Object.keys(result.features), ["one"]);
  assert.equal(result.sync.mode, "repository-events");
  payload.sync = { mode: "scheduled-review", intervalMinutes: 5 };
  assert.equal(validate(payload).sync.intervalMinutes, 5);
});

test("rejects bad envelopes and does not treat an empty feed as live", () => {
  for (const change of [
    (p) => { p.schemaVersion = 2; },
    (p) => { p.features = []; },
    (p) => { p.features = {}; },
    (p) => { p.repository = null; },
    (p) => { p.sync.mode = "unsupported"; },
    (p) => { p.sync.intervalMinutes = 0; },
    (p) => { p.sync.intervalMinutes = 600; },
  ]) {
    const payload = fixture(); change(payload);
    assert.throws(() => validate(payload));
  }
});

test("rejects malformed or substantially future timestamps", () => {
  for (const value of ["tomorrow", "2026-10-03T19:00:00", "2026-10-03T20:00:00Z", "2026-99-03T19:00:00Z"]) {
    const payload = fixture(); payload.features.one.checkedAt = value;
    assert.throws(() => validate(payload));
  }
});

test("validates every known feature before accepting the feed", () => {
  for (const change of [
    (p) => { p.features.one.status = "complete"; },
    (p) => { p.features.one.progress = "finished"; },
    (p) => { p.features.one.summary = ""; },
    (p) => { p.features.one.summary = "x".repeat(4001); },
    (p) => { p.features.one.sourceRevision = null; },
    (p) => { p.features.one = { status: "implemented" }; },
  ]) {
    const payload = fixture(); change(payload);
    assert.throws(() => validate(payload));
  }
});

test("rejects script, local-file, relative and credential-bearing evidence links", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///private.txt", "/relative", "https://user:secret@example.com"]) {
    const payload = fixture(); payload.features.one.links[0].url = url;
    assert.throws(() => validate(payload));
  }
});

test("treats malicious-looking text as inert text and escapes HTML/attributes", () => {
  const payload = fixture();
  payload.features.one.summary = '<img src=x onerror="alert(1)"> & injected';
  payload.features.one.links[0].title = "' onclick='alert(1)";
  const result = validate(payload).features.one;
  assert.equal(live.escapeHTML(result.summary), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; injected');
  assert.equal(live.escapeHTML(result.links[0].title), "&#39; onclick=&#39;alert(1)");
});

test("merges statuses without changing descriptions or inferring completion from a source change", () => {
  const base = [{ id: "one", status: "partial", title: "Original", summary: "What it does", details: ["Saved detail"] },
    { id: "two", status: "planned", summary: "Missing from this feed" }];
  const result = live.merge(base, validate(fixture()));
  assert.equal(result[0].status, "partial");
  assert.equal(result[0].live.progress, "changed");
  assert.equal(result[0].summary, "What it does");
  assert.equal(result[0].title, "Original");
  assert.deepEqual(result[0].details, ["Saved detail"]);
  assert.equal(base[0].live, undefined);
  assert.equal(result[1], base[1]);
});

test("accepts older historical verification evidence in a fresh feed", () => {
  const base = live.merge([{ id: "one", status: "planned" }], validate(fixture()));
  const next = fixture();
  next.features.one.checkedAt = new Date(now - 10 * 24 * 60 * 60000).toISOString();
  next.features.one.status = "implemented";
  next.features.one.progress = "verified";
  const updated = live.merge(base, validate(next))[0];
  assert.equal(updated.live.progress, "verified");
  assert.equal(updated.status, "implemented");
  assert.equal(updated.live.checkedAt, next.features.one.checkedAt);
  assert.equal(live.state({ enabled: true, feed: validate(next), error: null, loading: false }, now), "live");
});

test("rejects feed-envelope rollback without conflating it with an evidence date", () => {
  const previous = validate(fixture());
  const oldCheck = fixture(); oldCheck.checkedAt = new Date(now - 1000).toISOString();
  const oldGeneration = fixture(); oldGeneration.generatedAt = new Date(now - 1000).toISOString();
  assert.equal(live.isOlderFeed(validate(oldCheck), previous), true);
  assert.equal(live.isOlderFeed(validate(oldGeneration), previous), true);
  assert.equal(live.isOlderFeed(validate(fixture()), previous), false);
  assert.equal(live.isOlderFeed(validate(fixture()), null), false);
});

test("distinguishes offline, connecting, initial failure and a fresh live review", () => {
  const state = { enabled: true, feed: null, error: null, loading: true };
  assert.equal(live.state(state, now), "connecting");
  assert.equal(live.state({ ...state, enabled: false }, now), "offline");
  assert.equal(live.state({ ...state, loading: false, error: "failed" }, now), "unavailable");
  assert.equal(live.state({ ...state, feed: validate(fixture()), loading: false }, now), "live");
});

test("marks a five-minute review stale exactly at fifteen minutes", () => {
  const payload = fixture(); payload.sync = { mode: "scheduled-review", intervalMinutes: 5 };
  const state = { enabled: true, feed: validate(payload), error: null, loading: false };
  assert.equal(live.state(state, now + 15 * 60000 - 1), "live");
  assert.equal(live.state(state, now + 15 * 60000), "stale");
});

test("allows the hourly fallback and marks it stale exactly at ninety minutes", () => {
  const state = { enabled: true, feed: validate(fixture()), error: null, loading: false };
  assert.equal(live.state(state, now + 90 * 60000 - 1), "live");
  assert.equal(live.state(state, now + 90 * 60000), "stale");
});

test("an error makes retained values stale even when their review was recent", () => {
  const feed = validate(fixture());
  assert.equal(live.state({ enabled: true, feed, error: "network failure", loading: false }, now), "stale");
  assert.equal(feed.features.one.status, "partial");
  assert.equal(feed.features.one.progress, "changed");
});

test("flags missing and unknown inventory entries instead of claiming complete synchronization", () => {
  const payload = fixture();
  payload.features.futureFeature = { status: "new inventory, not rendered by this map" };
  const feed = live.validate(payload, new Set(["one", "two"]), now);
  assert.deepEqual(feed.inventory, { matched: 1, missing: 1, unknown: 1 });
  assert.equal(live.state({ enabled: true, feed, error: null, loading: false }, now), "partial");
  assert.deepEqual(Object.keys(feed.features), ["one"]);
});
