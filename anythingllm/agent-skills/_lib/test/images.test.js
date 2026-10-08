const test = require("node:test");
const assert = require("node:assert/strict");

const imageSearch = require("../../image-search/handler").runtime;
const { fakeService } = require("./fakeservice");

const self = { logger: () => {}, super: { handlerProps: { invocation: { uuid: "inv-1", workspace: { slug: "home" }, thread_id: 3 } } } };

test("image-search sends the query to research-runner and gives an Image: line per picture", async () => {
  const image = "[![Red panda](https://h:8445/_webimages/ab.jpg)](https://zoo.example/pandas)";
  const service = await fakeService(() => ({
    ok: true,
    result: { images: [{ image, url: "https://h:8445/_webimages/ab.jpg", page: "https://zoo.example/pandas", source: "zoo.example", width: 480, height: 320 }], skipped: ["a: 404", "b: 404"] },
  }));
  process.env.RESEARCH_SOCKET = service.socket;
  try {
    const reply = await imageSearch.handler.call(self, { query: "red panda", count: "2" });
    assert.equal(reply, `Image: ${image}\nsource: zoo.example (480×320)\n(2 more found couldn't be fetched)`);
    assert.deepEqual(service.requests[0], { op: "images", args: { query: "red panda", url: "", count: 2, alt: "" } });
    await imageSearch.handler.call(self, { url: "https://x.example/a.png", alt: "A" });
    assert.deepEqual(service.requests[1].args, { query: "", url: "https://x.example/a.png", count: null, alt: "A" });
  } finally {
    delete process.env.RESEARCH_SOCKET;
    await service.close();
  }
});
