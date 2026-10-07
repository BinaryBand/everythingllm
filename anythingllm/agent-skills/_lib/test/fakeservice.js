// A fake host service for the skill tests, on a socket of its own: answers each request
// with respond(op, args) and keeps the requests. (No tests here; node --test runs it as an
// empty file.)

const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

async function fakeService(respond) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "svc-sock-"));
  const socket = path.join(dir, "runner.sock");
  const requests = [];
  const server = net.createServer((conn) => {
    let buffer = "";
    conn.on("data", (chunk) => {
      buffer += chunk;
      if (!buffer.includes("\n")) return;
      const msg = JSON.parse(buffer.split("\n")[0]);
      requests.push(msg);
      conn.end(JSON.stringify(respond(msg.op, msg.args)) + "\n");
    });
  });
  await new Promise((r) => server.listen(socket, r));
  return { socket, requests, close: () => new Promise((r) => server.close(r)) };
}

module.exports = { fakeService };
