import net from "node:net";

export function parsePorts(args) {
  const result = { backendPort: 8000, frontendPort: 3000, customBackend: false, help: false };
  const names = { "--backend-port": "backendPort", "--frontend-port": "frontendPort" };
  const seen = new Set();
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === "--help") { result.help = true; continue; }
    if (!names[flag] || seen.has(flag)) throw new Error("Unknown or duplicate launcher option.");
    const value = args[++index];
    if (!/^\d+$/.test(value || "") || Number(value) < 1 || Number(value) > 65535)
      throw new Error("Ports must be integers from 1 through 65535.");
    seen.add(flag);
    result[names[flag]] = Number(value);
    if (flag === "--backend-port") result.customBackend = true;
  }
  if (result.backendPort === result.frontendPort) throw new Error("Frontend and backend ports must differ.");
  return result;
}

export function assertPortFree(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", () => reject(new Error(`Loopback port ${port} is unavailable; choose an explicit alternate port.`)));
    server.listen({ host: "127.0.0.1", port, exclusive: true }, () => server.close(resolve));
  });
}
