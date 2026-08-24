const test = require("node:test");
const assert = require("node:assert/strict");

const { runSequentially } = require("../../static/js/dailyops_collection.js");

test("one failed platform does not stop the remaining six", async () => {
  const platforms = [
    "PINTEREST",
    "QUORA",
    "TIKTOK",
    "SHOPIFY",
    "GOOGLE_SEARCH",
    "GOOGLE_SEARCH_CONSOLE",
    "GOOGLE_ANALYTICS_4",
  ];
  const attempted = [];
  const progress = [];

  const results = await runSequentially(
    platforms,
    async (platform) => {
      attempted.push(platform);
      if (platform === "TIKTOK") throw new Error("temporary failure");
      return `${platform}:ok`;
    },
    ({ completed, total, result }) => {
      progress.push({ completed, total, status: result.status });
    },
  );

  assert.deepEqual(attempted, platforms);
  assert.equal(results.length, 7);
  assert.equal(results.filter((item) => item.status === "rejected").length, 1);
  assert.deepEqual(
    progress.map(({ completed }) => completed),
    [1, 2, 3, 4, 5, 6, 7],
  );
  assert.deepEqual(progress.at(-1), {
    completed: 7,
    total: 7,
    status: "fulfilled",
  });
});

test("platform workers never overlap", async () => {
  let active = 0;
  let maximumActive = 0;

  const results = await runSequentially(
    Array.from({ length: 7 }, (_, index) => index),
    async (index) => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise((resolve) => setTimeout(resolve, index % 2));
      active -= 1;
      return index;
    },
  );

  assert.equal(maximumActive, 1);
  assert.equal(results.length, 7);
  assert.ok(results.every((item) => item.status === "fulfilled"));
});
