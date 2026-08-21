(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.DailyOpsCollection = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  async function runSequentially(items, worker, onSettled) {
    const queue = Array.from(items);
    const results = [];

    for (let index = 0; index < queue.length; index += 1) {
      const item = queue[index];
      let result;
      try {
        result = { status: "fulfilled", value: await worker(item, index) };
      } catch (reason) {
        result = { status: "rejected", reason };
      }
      results.push(result);
      if (onSettled) {
        onSettled({
          item,
          index,
          completed: index + 1,
          total: queue.length,
          result,
        });
      }
    }

    return results;
  }

  return { runSequentially };
});
