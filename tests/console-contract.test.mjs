import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { Script } from "node:vm";

const source = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const sample = JSON.parse(source.match(/const VOICE_CALL_EXAMPLE = (.*);/)[1]);

test("the complete console script parses", () => {
  const scripts = [...source.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length);
  for (const [, script] of scripts) new Script(script);
});

test("the real console mock uses the Pipecat request and response contract", async () => {
  const start = source.indexOf('case "/voice_followup/calls": {');
  const end = source.indexOf("\n      default:", start);
  const run = new Function("body", "json", 'switch ("/voice_followup/calls") {' + source.slice(start, end) + '}');
  const json = (body, status = 200) => ({ body, status });
  const result = run(() => sample, json);
  assert.equal(result.status, 200);
  assert.equal(result.body.transport, "pipecat");
  assert.equal(result.body.stream_path, "/voice_followup/stream");
  assert.equal(result.body.livekit_url, "");
  assert.equal(run(() => ({ call_type: "followup", order: {} }), json).status, 422);
});
