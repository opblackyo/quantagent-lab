import assert from "node:assert/strict";
import net from "node:net";
import { test } from "node:test";
import { parsePorts, assertPortFree } from "./dev_options.mjs";

test("launcher preserves default ports", () => {
  assert.deepEqual(parsePorts([]), { backendPort: 8000, frontendPort: 3000, customBackend: false, help: false });
});
test("explicit ports and help are parsed without starting services", () => {
  assert.deepEqual(parsePorts(["--backend-port", "8002", "--frontend-port", "3002"]),
    { backendPort: 8002, frontendPort: 3002, customBackend: true, help: false });
  assert.equal(parsePorts(["--help"]).help, true);
});
for (const args of [["--backend-port", "0"], ["--frontend-port", "65536"], ["--backend-port", "abc"],
  ["--backend-port"], ["--other", "8002"], ["--backend-port", "8002", "--backend-port", "8003"],
  ["--backend-port", "3000"]]) {
  test(`reject invalid option ${JSON.stringify(args)}`, () => assert.throws(() => parsePorts(args)));
}
test("occupied loopback port is rejected without touching its server", async () => {
  const owner = net.createServer();
  await new Promise(resolve => owner.listen(0, "127.0.0.1", resolve));
  try {
    await assert.rejects(assertPortFree(owner.address().port));
    assert.equal(owner.listening, true);
  } finally { await new Promise(resolve => owner.close(resolve)); }
});
