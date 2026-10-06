// Add Podcast: subscribes to a podcast through podcasts-runner (packages/podcasts, add_podcast).
// A skill, not an MCP tool, so that it can refuse a delegated task (_lib/delegated.js).

const { forward, asFlag } = require("../_lib/runner");

const DEFAULT_KEEP = 5; // podcasts.limits.DEFAULT_KEEP

module.exports.runtime = {
  handler: async function ({ url, keep, slug, scrub_ads, transcribe, ad_words, rules }) {
    let kept = DEFAULT_KEEP;
    if (keep != null && keep !== "") kept = /^\d+$/.test(String(keep)) ? Number(keep) : keep; // a number, or "all"
    return forward(this, {
      service: "podcasts",
      env: "PODCASTS_SOCKET",
      op: "add_podcast",
      args: {
        url,
        keep: kept,
        slug: slug ?? "",
        scrub_ads: asFlag(scrub_ads),
        transcribe: asFlag(transcribe),
        ad_words: ad_words || null,
        rules: rules ?? null,
      },
    });
  },
};
