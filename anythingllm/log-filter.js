// Preloaded into AnythingLLM's node processes (NODE_OPTIONS=--require, set in
// host/quadlet/anythingllm.container.in). AnythingLLM logs every MCP message
// and tool result in full at info level, with no setting to turn it off, so page text,
// emails and files the agent read would land in the journal. Those lines keep their
// prefix but lose their payload (a failed tool call keeps its first ERROR_KEEP
// characters, so the error stays visible), and any other line is cut at MAX_LINE.
//
// Scheduled jobs run in node processes Bree forks, which write to the container's stdout
// themselves. A fork inherits execArgv (spawned processes don't), so this adds itself
// there for them, while NODE_OPTIONS is dropped so spawned MCP servers, whose stdout is
// their protocol stream, never load it. Chunks may be Buffers, and objects are logged
// across several lines: so chunks are decoded first, and the payload runs to the end of
// the chunk, not of the line.
const PAYLOAD = /( - Transport message:| MCP server: \S+ completed successfully) ([\s\S]+?)(\n?)$/;
const IS_ERROR = /"isError":\s*true|isError: true/;
const ERROR_KEEP = 300;
const MAX_LINE = 2000;

/** What to log instead of `text`. Pure, for the tests. */
function filter(text) {
  text = text.replace(PAYLOAD, (_, head, body, newline) => {
    const kept = IS_ERROR.test(body) ? body.slice(0, ERROR_KEEP) : "";
    const omitted = body.length - kept.length;
    return `${head}${kept && ` ${kept}`}${omitted ? ` [${omitted} chars omitted]` : ""}${newline}`;
  });
  if (text.length > MAX_LINE) text = `${text.slice(0, MAX_LINE)} [${text.length - MAX_LINE} chars omitted]\n`;
  return text;
}

module.exports = { filter };

// Only when preloaded (no main module yet), so a test can require this file.
if (!require.main) {
  delete process.env.NODE_OPTIONS;
  process.execArgv.push(`--require=${__filename}`);

  const write = process.stdout.write;
  process.stdout.write = function (chunk, ...rest) {
    if (chunk instanceof Uint8Array) {
      chunk = Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength).toString("utf8");
      // An encoding meant for the Buffer would misread the string.
      if (typeof rest[0] === "string") rest[0] = "utf8";
    }
    if (typeof chunk === "string") chunk = filter(chunk);
    return write.call(this, chunk, ...rest);
  };
}
